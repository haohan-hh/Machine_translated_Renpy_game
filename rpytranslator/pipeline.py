"""Shared workflow for GUI and CLI translation, repair and runtime verification."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import engine
from .audit import AuditSummary, plan_retry_queue, run_audit
from .connection import ollama_gpu_status, prepare_local_model
from .extract import DialogueUnit
from .generator import parse_existing_translations, write_translation_files
from .glossary import read_name_groups, save_game_terms
from .patcher import PatchResult, apply_all
from .reports import (
    _escape_report_text, import_report_texts, parse_external_report, read_retry_queue, write_retry_entries,
)
from .sources import extract_game_scripts
from .storage import write_text_atomic
from .tl_files import index_tl_directory, unambiguous_translations
from .translator import (
    PauseRequested, TranslationClient, TranslationConfig, TranslationError, target_language_name,
    unprotected_text, valid_unchanged_text,
)
from .verify import (
    build_missing_fix, install_language_switch_probe, runtime_report_path, write_verification_patch,
)

DEFAULT_LANGUAGE = "schinese"
MAX_RETRY_ROUNDS = 5
_DIGEST = re.compile(r"(?:^|_)([0-9a-f]{8})(?:_\d+)?$")


@dataclass
class PipelineResult:
    ok: bool = False
    game_dir: Path | None = None
    languages: list[str] = field(default_factory=list)
    has_chinese: bool = False
    dialogue_count: int = 0
    string_count: int = 0
    translated_count: int = 0
    skipped_count: int = 0
    output_files: list[Path] = field(default_factory=list)
    output_dir: Path | None = None
    post_patches: list[PatchResult] = field(default_factory=list)
    removed_rpyc: int = 0
    removed_dup_blocks: int = 0
    paused: bool = False
    audit: AuditSummary | None = None
    message: str = ""
    errors: list[str] = field(default_factory=list)


def _load_progress(path: Path, texts: set[str], names: list[str], log):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("断点格式无效")
        tables = [data.get(k, {}) for k in ("dialogues", "strings")]
        if not all(isinstance(table, dict) for table in tables):
            raise ValueError("断点词表格式无效")
    except FileNotFoundError:
        return {}, {}
    except (ValueError, OSError) as exc:
        log(f"无法读取旧断点，将按游戏中已有译文继续：{exc}")
        return {}, {}
    previous = data.get("protected_terms", [])
    if not isinstance(previous, list):
        previous = []
    added_names = set(names).difference(n for n in previous if isinstance(n, str))
    return tuple({s: t for s, t in table.items()
                  if s in texts and isinstance(t, str) and t.strip()
                  and not any(n in s and n not in t for n in added_names)}
                 for table in tables)


def _digest(identifier):
    match = _DIGEST.search(identifier)
    return match[1] if match else None


def _plan_dialogues(dialogues, existing, retry_ids, changed_ids):
    by_digest = unambiguous_translations((_digest(key), value) for key, value in existing.items()
                                         if key not in changed_ids)
    by_text = unambiguous_translations((d.what, existing.get(d.identifier)) for d in dialogues
                                       if d.identifier not in changed_ids)
    resolved = {}
    pending = []
    for d in dialogues:
        key = d.identifier
        if key in existing:
            # None represents a registered multi-statement block. It is owned
            # by the game and has no single translation to reuse elsewhere.
            value = existing[key]
            if value is None:
                continue
        else:
            value = by_digest.get(_digest(key)) or by_text.get(d.what)
        if value:
            resolved[key] = value
        if not value or key in retry_ids or key in changed_ids:
            pending.append(d)
    return resolved, pending


def _make_client(config, log):
    def prepare():
        ready, detail = prepare_local_model(config.base_url, config.model,
                                            api_key=config.api_key, log=log)
        if not ready:
            raise TranslationError(detail)
    return TranslationClient(config, prepare=prepare)


def _needs_retry(text, target, protected, client, *, is_ui=False) -> bool:
    return text in getattr(client, "failed_texts", ()) or not valid_unchanged_text(
        text, target, protected, is_ui=is_ui)


def _source(unit):
    return unit.what if isinstance(unit, DialogueUnit) else unit.text


def _translate_pending(groups, tables, client, target, names, save, log, pause_event):
    """Fill the two checkpoint tables without scheduling covered dialogue IDs."""
    def progress(done, total):
        log("PROGRESS|%d" % (done * 100 // max(1, total)))

    writes = 0

    def on_item(table):
        def record(source, translation):
            nonlocal writes
            table[source] = translation
            writes += 1
            if writes % 50 == 0:
                save()
        return record

    def needs_request(text, table, round_number, protected, is_ui):
        if not any(c.isalpha() for c in unprotected_text(text, protected)):
            table[text] = text
            return False
        if table.get(text) and table[text] != text:
            return False
        return round_number == 0 or _needs_retry(text, target, protected, client, is_ui=is_ui)

    for attempt in range(MAX_RETRY_ROUNDS + 1):
        protected = set(names) | set(getattr(client, "detected_names", ()))
        todo = [[t for t in group if needs_request(t, table, attempt, protected, is_ui)]
                for group, table, is_ui in zip(groups, tables, (False, True))]
        total = sum(map(len, todo))
        if not total:
            return
        if pause_event is not None and pause_event.is_set():
            raise PauseRequested()
        if attempt:
            log(f"补译第 {attempt}/{MAX_RETRY_ROUNDS} 轮，剩余 {total} 条")
            if pause_event is None:
                time.sleep(2)
            elif pause_event.wait(2):
                raise PauseRequested()
        offset = 0
        for texts, table in zip(todo, tables):
            if not texts:
                continue
            values = client.translate_texts(
                texts, target=target, names=names, offset=offset, total=total,
                progress_cb=progress, error_cb=lambda m: log("ERR|" + m),
                item_cb=on_item(table))
            if len(values) != len(texts):
                raise TranslationError("模型返回条数与待译条数不一致，已保留断点")
            table.update(zip(texts, values))
            offset += len(texts)
        save()


def _postprocess(result, game, language, client, names, font, ui, verify, log):
    if font or ui:
        result.post_patches.extend(apply_all(
            game, language=language, with_font=font, with_language_ui=ui))
    if verify:
        result.post_patches.append(write_verification_patch(game, language, log_cb=log))
        if ui:
            result.post_patches.append(install_language_switch_probe(game, language, log_cb=log))
    if runtime_report_path(game, language).is_file():
        fix = build_missing_fix(game, language, client=client, names=names, log_cb=log)
        result.post_patches.append(fix)
        result.output_files.extend(fix.files)
    for patch in result.post_patches:
        if patch.message:
            log(patch.message)
        if not patch.ok:
            result.errors.append(patch.message)


def run_pipeline(
    game_path: str | Path, config: TranslationConfig | None = None,
    language: str = DEFAULT_LANGUAGE, client: TranslationClient | None = None,
    progress_cb: Callable[[str], None] | None = None, apply_font_patch: bool = True,
    apply_language_ui: bool = True, apply_verification: bool = True,
    extra_terms: list[str] | None = None, pause_event=None,
) -> PipelineResult:
    result = PipelineResult()
    log = progress_cb or (lambda text: None)
    language = language.strip()
    if not language.isidentifier() or language.lower() == "none":
        result.message = f"目标语言标识无效：{language!r}"
        return result
    started = time.monotonic()
    log("正在扫描游戏目录…")
    try:
        info = engine.scan_game(game_path)
    except (OSError, ValueError) as exc:
        result.message = f"无法扫描游戏目录：{exc}"
        return result
    game = result.game_dir = info.game_dir
    result.languages, result.has_chinese = info.languages, info.has_chinese
    if game is None:
        result.message = "；".join(info.notes) or "未找到 Ren'Py 游戏"
        return result

    tl = game / "tl"
    out = result.output_dir = tl / language
    cache = tl / f".{language}.汉化进度.json"
    marker = tl / f".{language}.暂停标记.txt"
    queue = tl / f"{language}.未翻译报告.txt"
    try:
        extracted = extract_game_scripts(info, log)
        for warning in extracted.skipped:
            log(warning)
        if extracted.skipped:
            result.errors.extend(extracted.skipped)
        dialogues = extracted.dialogues
        stored = read_name_groups(game, language)
        characters = list(dict.fromkeys(u.text for u in extracted.strings if u.context == "character"))
        if extracted.skipped:
            characters = list(dict.fromkeys(characters + stored.get("characters", [])))
        detected = stored.get("detected", [])
        names = list(dict.fromkeys(characters + detected + [s.strip() for s in (extra_terms or []) if s.strip()]))
        protected = set(names)
        strings = [s for s in extracted.strings if s.context != "character" and s.text not in protected]
        docs = index_tl_directory(out, language)
        originals = {}
        existing_d, existing_s = parse_existing_translations(out, language, originals=originals, docs=docs)
        retry = plan_retry_queue(out, language, queue, dialogues, strings, protected, docs=docs)
        # A shipped translation can contain definitions (e.g. engine strings)
        # with no corresponding literal in the extracted game scripts.
        game_ids = {d.identifier for d in dialogues}
        game_strings = {s.text for s in strings}
        dialogues = dialogues + [d for key, d in retry.dialogues.items() if key not in game_ids]
        strings = strings + [s for key, s in retry.strings.items() if key not in game_strings]
        result.dialogue_count, result.string_count = len(dialogues), len(strings)
        if not dialogues and not strings:
            result.message = "未提取到可翻译的文本" + ("\n" + "\n".join(result.errors) if result.errors else "")
            return result
        log(f"提取到 {len(dialogues)} 条对话、{len(strings)} 条字符串，保护 {len(names)} 个名称")

        old_queue = retry.entries
        changed_ids = {d.identifier for d in dialogues
                       if d.identifier in originals and originals[d.identifier] != d.what}
        d_output, pending_d = _plan_dialogues(dialogues, existing_d, retry.dialogues, changed_ids)
        # None is a registered, game-controlled expression. An empty literal is
        # a translation template to fill, and an absent key is a new definition.
        pending_s = [s for s in strings if s.text not in existing_s or
                     (existing_s[s.text] is not None and (not existing_s[s.text] or s.text in retry.strings))]
        s_output = {s.text: existing_s[s.text] for s in strings if existing_s.get(s.text)}
        groups = (list(dict.fromkeys(d.what for d in pending_d)),
                  list(dict.fromkeys(s.text for s in pending_s)))
        valid = set(groups[0]) | set(groups[1])
        tables = _load_progress(cache, valid, names, log)
        if client is None:
            client = _make_client(config or TranslationConfig(), log)
        if pause_event is not None:
            client.pause_event = pause_event
        log(f"本次待补 {len(groups[0])} 种对话、{len(groups[1])} 种字符串，"
            f"断点中已有 {sum(len(t) for t in tables)} 条记录")

        def save_terms():
            save_game_terms(game, language, characters, [*detected, *client.detected_names], stored)

        def save():
            save_terms()
            data = {"dialogues": tables[0], "strings": tables[1],
                    "protected_terms": list(dict.fromkeys([*names, *client.detected_names]))}
            write_text_atomic(cache, json.dumps(data, ensure_ascii=False))

        target = target_language_name(language)
        try:
            _translate_pending(groups, tables, client, target, names, save, log, pause_event)
        except (PauseRequested, TranslationError) as exc:
            save()
            if isinstance(exc, TranslationError):
                raise
            waiting = [u for units, table in zip((pending_d, pending_s), tables)
                       for u in units if not table.get(_source(u)) or table[_source(u)] == _source(u)]
            write_text_atomic(marker, "\n".join(
                f"{u.filename}:{u.line}  {_escape_report_text(_source(u))}"
                for u in waiting))
            result.paused = True
            result.message = f"已暂停，剩余 {len(waiting)} 条，断点已保存到 {cache}"
            return result

        protected.update(getattr(client, "detected_names", ()))
        pending_queue = ({text: loc for text, loc in old_queue.items() if text not in valid}
                         if extracted.skipped else {})
        updated_ids = set()
        accepted_originals = set()
        for units, table, output in zip((pending_d, pending_s), tables, (d_output, s_output)):
            for unit in units:
                is_dialogue = isinstance(unit, DialogueUnit)
                if is_dialogue:
                    key, text = unit.identifier, unit.what
                else:
                    key = text = unit.text
                value = table.get(text)
                accepted_original = value == text and not _needs_retry(
                    text, target, protected, client, is_ui=not is_dialogue)
                if value and value.strip() and (value != text or accepted_original):
                    output[key] = value
                    if accepted_original:
                        accepted_originals.add((not is_dialogue, key))
                    if is_dialogue and key in changed_ids:
                        updated_ids.add(key)
                else:
                    # A failed repair must not replace the user's previous rendering.
                    output.setdefault(key, text)
                    if _needs_retry(text, target, protected, client, is_ui=not is_dialogue):
                        pending_queue[text] = f"{unit.filename}:{unit.line}"

        result.output_files = write_translation_files(
            dialogues, strings, language, d_output, s_output, game_dir=game,
            retry_texts=set(old_queue), changed_ids=updated_ids, accepted_originals=accepted_originals)
        write_retry_entries(queue, pending_queue, old_queue, manual_texts=retry.manual_texts)
        _postprocess(result, game, language, client, sorted(protected), apply_font_patch,
                     apply_language_ui, apply_verification, log)
        save_terms()
        protected.update(getattr(client, "detected_names", ()))
        result.audit = run_audit(game, language, log=log, protected_terms=protected)
        result.output_files = list(dict.fromkeys([*result.output_files, *result.audit.fixed_files]))
        if result.audit.read_only_count:
            result.errors.append(f"审校发现 {result.audit.read_only_count} 条只读译文需要对应的源码，"
                                 f"见 {result.audit.report_path}")
        remaining = set(read_retry_queue(queue))
        result.skipped_count = len(remaining)
        result.translated_count = sum(d.what not in remaining for d in dialogues)
        result.translated_count += sum(s.text not in remaining for s in strings)
        if remaining:
            result.errors.append(f"仍有 {len(remaining)} 条原文待补译，见 {queue}")
        cache.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        result.ok = not result.errors
        heading = ("汉化完成" if result.output_files else "已有译文检查完成") if result.ok else "汉化尚未全部完成"
        result.message = (
            f"{heading}，本次写入 {len(result.output_files)} 个译文文件，"
            f"耗时 {time.monotonic() - started:.0f} 秒。\n译文目录：{out}"
        )
        if result.errors:
            result.message += "\n" + "\n".join(result.errors)
        log(f"模型请求 {client.request_count} 次，其中失败 {client.error_count} 次")
        if client.request_count and isinstance(client, TranslationClient):
            cfg = client.config
            gpu = ollama_gpu_status(cfg.base_url, cfg.model, api_key=cfg.api_key)
            if gpu:
                log(gpu)
        if result.ok:
            log("PROGRESS|100")
    except PauseRequested:
        result.paused = True
        result.message = "运行时补译已暂停，重新开始时会继续处理缺失报告"
    except (OSError, ValueError, RuntimeError, TranslationError) as exc:
        result.errors.append(str(exc))
        result.message = f"汉化未完成：{exc}"
    return result
