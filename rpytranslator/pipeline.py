"""Shared workflow for GUI and CLI translation, repair and runtime verification."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import engine
from .audit import AuditItem, AuditSummary, classify, plan_retry_queue, reusable_dialogue, run_audit
from .connection import ollama_gpu_status, prepare_local_model
from .extract import DialogueUnit
from .generator import parse_existing_translations, write_translation_files
from .glossary import read_name_groups, required_game_names, save_game_terms
from .patcher import PatchResult, apply_all
from .reports import (
    _escape_report_text, import_report_texts, parse_external_report, read_retry_queue, write_retry_entries,
)
from .sources import extract_game_scripts
from .storage import write_text_atomic
from .tl_files import index_tl_directory, unambiguous_translations
from .translator import (PauseRequested, TranslationClient, TranslationConfig, TranslationError,
                         protect_text, target_language_name, unprotected_text, valid_unchanged_text)
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


@dataclass
class _CheckpointTable:
    values: dict[str, str] = field(default_factory=dict)
    confirmed_unchanged: set[str] = field(default_factory=set)

    def complete(self, source):
        value = self.values.get(source)
        return bool(isinstance(value, str) and value.strip()
                    and (value != source or source in self.confirmed_unchanged))

    def record(self, source, value, confirmed=False):
        if isinstance(value, str) and value.strip():
            self.values[source] = value
        else:
            self.values.pop(source, None)
        if value == source and confirmed:
            self.confirmed_unchanged.add(source)
        else:
            self.confirmed_unchanged.discard(source)


_CHECKPOINT_GROUPS = ("dialogues", "strings")


def _load_progress(path, groups, names, target, log):
    tables = (_CheckpointTable(), _CheckpointTable())
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("断点格式无效")
        if not all(isinstance(data.get(key, {}), dict) for key in _CHECKPOINT_GROUPS):
            raise ValueError("断点词表格式无效")
    except FileNotFoundError:
        return tables
    except (ValueError, OSError) as exc:
        log(f"无法读取旧断点，将按游戏中已有译文继续：{exc}")
        return tables
    # Old checkpoints cannot distinguish a successful identical answer from a
    # failed request returning its input. Changed cached model output must still
    # preserve every original tag, substitution, newline and protected name.
    saved = data.get("confirmed_unchanged")
    for key, group, table in zip(_CHECKPOINT_GROUPS, groups, tables):
        for s in group:
            t = data.get(key, {}).get(s)
            if not isinstance(t, str) or not t.strip():
                continue
            if t != s:
                if list(protect_text(s)[1].values()) != list(protect_text(t)[1].values()):
                    continue
                check = AuditItem("", "", 0, original=s, translation=t, is_string=key == "strings")
                if classify(check, target, names) is not None:
                    continue
            table.values[s] = t
        marked = saved.get(key) if isinstance(saved, dict) else None
        if isinstance(marked, list):
            table.confirmed_unchanged = {s for s in marked if isinstance(s, str)
                                         and table.values.get(s) == s and valid_unchanged_text(
                                             s, target, names, is_ui=key == "strings")}
    return tables


def _digest(identifier):
    match = _DIGEST.search(identifier)
    return match[1] if match else None


def _plan_dialogues(dialogues, existing, retry_ids, changed_ids, language, protected):
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
            candidates = (by_digest.get(_digest(key)), by_text.get(d.what))
            value = next((v for v in candidates if v and reusable_dialogue(d.what, v, language, protected)), None)
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
    return TranslationClient(config, prepare=prepare, log=log)


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

    def on_item(table, is_ui):
        protected = set(names) | set(getattr(client, "detected_names", ()))
        reported = set()

        def record(source, translation):
            nonlocal writes
            confirmed = False
            if source == translation:
                protected.update(getattr(client, "detected_names", ()))
                confirmed = not _needs_retry(source, target, protected, client, is_ui=is_ui)
            table.record(source, translation, confirmed)
            reported.add(source)
            writes += 1
            if writes % 50 == 0:
                save()
        return record, reported

    def needs_request(text, table, protected):
        if table.complete(text):
            return False
        if not any(c.isalpha() for c in unprotected_text(text, protected)):
            table.record(text, text, confirmed=True)
            return False
        return True

    for attempt in range(MAX_RETRY_ROUNDS + 1):
        protected = set(names) | set(getattr(client, "detected_names", ()))
        todo = [[t for t in group if needs_request(t, table, protected)]
                for group, table in zip(groups, tables)]
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
        for texts, table, is_ui in zip(todo, tables, (False, True)):
            if not texts:
                continue
            record, reported = on_item(table, is_ui)
            values = client.translate_texts(
                texts, target=target, names=names, offset=offset, total=total,
                progress_cb=progress, error_cb=lambda m: log("ERR|" + m),
                item_cb=record)
            if len(values) != len(texts):
                raise TranslationError("模型返回条数与待译条数不一致，已保留断点")
            for source, value in zip(texts, values):
                # Injected clients may return results without invoking item_cb.
                if source not in reported or table.values.get(source) != value:
                    record(source, value)
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
        characters = list(required_game_names(u.text for u in extracted.strings if u.context == "character"))
        if extracted.skipped:
            characters = list(dict.fromkeys(characters + stored.get("characters", [])))
        detected = stored.get("detected", [])
        explicit = [s.strip() for s in (extra_terms or []) if s.strip()]
        names = list(dict.fromkeys(characters + detected + explicit))
        protected = set(names)
        # Character displays use the same Ren'Py string table. A role like
        # ``You`` or the title in ``Captain Jo`` still needs a translation.
        strings = [s for s in extracted.strings if s.text not in protected]
        docs = index_tl_directory(out, language)
        originals = {}
        existing_d, existing_s = parse_existing_translations(out, language, originals=originals, docs=docs)
        retry = plan_retry_queue(out, language, queue, dialogues, strings, protected,
                                 required_terms=required_game_names(characters, explicit), docs=docs)
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
        d_output, pending_d = _plan_dialogues(dialogues, existing_d, retry.dialogues, changed_ids, language, names)
        # None is a registered, game-controlled expression. An empty literal is
        # a translation template to fill, and an absent key is a new definition.
        pending_s = [s for s in strings if s.text not in existing_s or
                     (existing_s[s.text] is not None and (not existing_s[s.text] or s.text in retry.strings))]
        s_output = {s.text: existing_s[s.text] for s in strings if existing_s.get(s.text)}
        groups = (list(dict.fromkeys(d.what for d in pending_d)),
                  list(dict.fromkeys(s.text for s in pending_s)))
        valid = set(groups[0]) | set(groups[1])
        target = target_language_name(language)
        tables = _load_progress(cache, groups, names, target, log)
        if client is None:
            client = _make_client(config or TranslationConfig(), log)
        if pause_event is not None:
            client.pause_event = pause_event
        log(f"本次待补 {len(groups[0])} 种对话、{len(groups[1])} 种字符串，"
            f"断点中已完成 {sum(t.complete(s) for t in tables for s in t.values)} 条")

        def save_terms():
            save_game_terms(game, language, characters, [*detected, *client.detected_names], stored)

        def save():
            save_terms()
            data = {k: t.values for k, t in zip(_CHECKPOINT_GROUPS, tables)}
            data["confirmed_unchanged"] = {k: sorted(t.confirmed_unchanged)
                                           for k, t in zip(_CHECKPOINT_GROUPS, tables)}
            data["protected_terms"] = list(dict.fromkeys([*names, *client.detected_names]))
            write_text_atomic(cache, json.dumps(data, ensure_ascii=False))

        try:
            _translate_pending(groups, tables, client, target, names, save, log, pause_event)
        except (PauseRequested, TranslationError) as exc:
            save()
            if isinstance(exc, TranslationError):
                raise
            waiting = [u for units, table in zip((pending_d, pending_s), tables)
                       for u in units if not table.complete(_source(u))]
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
                value = table.values.get(text)
                if table.complete(text):
                    output[key] = value
                    if value == text:
                        accepted_originals.add((not is_dialogue, key))
                    if is_dialogue and key in changed_ids:
                        updated_ids.add(key)
                else:
                    # A failed repair must not replace the user's previous rendering.
                    output.setdefault(key, text)
                    pending_queue[text] = f"{unit.filename}:{unit.line}"

        result.output_files = write_translation_files(
            dialogues, strings, language, d_output, s_output, game_dir=game,
            retry_texts=set(old_queue), changed_ids=updated_ids, accepted_originals=accepted_originals)
        write_retry_entries(queue, pending_queue, old_queue, manual_texts=retry.manual_texts)
        try:
            _postprocess(result, game, language, client, sorted(protected), apply_font_patch,
                         apply_language_ui, apply_verification, log)
        finally:
            save_terms()
        protected.update(getattr(client, "detected_names", ()))
        result.audit = run_audit(game, language, log=log, protected_terms=explicit)
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
    except PauseRequested as exc:
        result.paused = True
        result.message = str(exc) or "运行时补译已暂停，重新开始时会继续处理缺失报告"
    except (OSError, ValueError, RuntimeError, TranslationError) as exc:
        result.errors.append(str(exc))
        result.message = f"汉化未完成：{exc}"
    return result
