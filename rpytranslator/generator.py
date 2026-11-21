# -*- coding: utf-8 -*-
"""
翻译文件生成器：按源文件生成 tl/<语言>/ 下的 .rpy 翻译文件。

按 Ren'Py 的 translate 对话块和 old/new 字符串格式输出：

    # TODO: Translation updated at 2026-08-30 14:30:00

    # script.rpy:19
    translate schinese start_915cb944:

        # "It's only when I hear…"
        "当我听到……"

    translate schinese strings:

        # options.rpy:15
        old "The Question"
        new "The Question"
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from pathlib import Path

from .extract import DialogueUnit, StringUnit, encode_say_string
from .tl_files import (
    TLEntry, TLFile, TranslationMergeError, editable_output_path, encode_string,
    index_tl_directory, unambiguous_translations, write_if_unchanged,
)

_HEADER = "# TODO: Translation updated at {stamp}"


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rel_path(filename: str, game_dir: Path) -> str:
    """把源文件路径转成相对 game 目录的 posix 路径。

    兼容三种输入：
    - 绝对路径（D:/Wild Harmonies/game/days/day_6.rpy）
    - 含 game/ 前缀的路径
    - 已是相对 game 目录的路径（days/route_ulrich/day_6.rpy，反编译/规范化后）
    """
    p = Path(filename)
    if p.is_absolute():
        try:
            return p.relative_to(game_dir).as_posix()
        except ValueError:
            pass
        parts = p.parts
        if "game" in parts:
            idx = parts.index("game")
            return Path(*parts[idx + 1:]).as_posix()
        return p.name
    return p.as_posix()


def _dialogue_body(unit: DialogueUnit, translated: str) -> str:
    """单个对话翻译块（不含 translate 头）。"""
    return "    # " + unit.say_code(unit.what) + "\n    " + unit.say_code(translated)


def _string_body(text: str, translated: str) -> str:
    return "    old " + encode_string(text) + "\n    new " + encode_string(translated)


def build_file_script(
    dialogues: list[DialogueUnit],
    strings: list[StringUnit],
    language: str,
    dialogue_translations: dict[str, str],   # identifier -> 译文
    string_translations: dict[str, str],     # 原文 -> 译文
    game_dir: Path | None = None,
    updated_at: str | None = None,
) -> str:
    """生成单个源文件对应的翻译脚本。"""
    blocks: list[str] = []
    if updated_at is None:
        updated_at = _stamp()
    if dialogues:
        seen_ids: set[str] = set()
        for d in dialogues:
            # 同一 translate identifier 只输出一次，避免 Ren'Py 编译 tl 时
            # 因重复定义报错导致整个文件的翻译失效（游戏显示英文原文）。
            if d.identifier in seen_ids:
                continue
            seen_ids.add(d.identifier)
            tr = dialogue_translations.get(d.identifier)
            if tr is None:
                continue
            ref = _rel_path(d.filename, game_dir) if game_dir else Path(d.filename).name
            blocks.append(
                f"# {ref}:{d.line}\n"
                f"translate {language} {d.identifier}:\n\n"
                f"{_dialogue_body(d, tr)}"
            )
    if strings:
        body: list[str] = []
        seen: set[str] = set()
        for s in strings:
            if s.text in seen:
                continue
            seen.add(s.text)
            tr = string_translations.get(s.text)
            if tr is None:
                continue
            ref = _rel_path(s.filename, game_dir) if game_dir else Path(s.filename).name
            body.append(f"    # {ref}:{s.line}\n{_string_body(s.text, tr)}")
        if body:
            blocks.append("translate " + language + " strings:\n\n" + "\n\n".join(body))
    if not blocks:
        return ""
    return _HEADER.format(stamp=updated_at) + "\n\n" + "\n\n".join(blocks) + "\n"


def group_by_source_file(
    dialogues: list[DialogueUnit], strings: list[StringUnit]
) -> dict[str, tuple[list[DialogueUnit], list[StringUnit]]]:
    """按源文件分组（对话与字符串同文件合并）。"""
    grouped: dict[str, tuple[list[DialogueUnit], list[StringUnit]]] = defaultdict(
        lambda: ([], []))
    for d in dialogues:
        grouped[d.filename][0].append(d)
    for s in strings:
        grouped[s.filename][1].append(s)
    return dict(grouped)


def _owners(docs: dict[Path, TLFile]) -> dict[tuple[bool, str], tuple[TLFile, TLEntry]]:
    owners = {}
    for doc in docs.values():
        for entry in doc.entries:
            key = (entry.identifier is None,
                   entry.original if entry.identifier is None else entry.identifier)
            if key in owners:
                first, other = owners[key]
                raise TranslationMergeError(
                    f"已有译文重复定义 {key[1]!r}：{first.origin or first.path}:{other.block_start + 1} "
                    f"和 {doc.origin or doc.path}:{entry.block_start + 1}，请先检查这两处")
            owners[key] = (doc, entry)
    return owners


def parse_existing_translations(
    tl_dir: Path, language: str, *, originals: dict[str, str] | None = None,
    docs: dict[Path, TLFile] | None = None,
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """None denotes an occupied multi-statement or dynamic translation."""
    dialogues, strings = {}, {}
    if docs is None:
        docs = index_tl_directory(tl_dir, language)
    # Archived source alone is not registered by Ren'Py. Feed its unambiguous
    # static translations to the planner so a loose definition can reuse them.
    references = unambiguous_translations(
        ((entry.identifier is None, entry.identifier or entry.original), (entry.original, entry.translation))
        for doc in docs.values() for entry in doc.reference_entries if entry.translation)
    for (is_string, key), pair in references.items():
        if pair is not None:
            (strings if is_string else dialogues)[key] = pair[1]
            if not is_string and originals is not None and pair[0] is not None:
                originals[key] = pair[0]
    for (is_string, key), (doc, entry) in _owners(docs).items():
        value = entry.translation
        if value == "" and (doc.read_only or entry.span is None):
            value = None  # an empty compiled/dynamic rendering cannot be filled in place
        (strings if is_string else dialogues)[key] = value
        if not is_string and originals is not None:
            if entry.original is not None:
                originals[key] = entry.original
            else:
                originals.pop(key, None)
    return dialogues, strings


def write_translation_files(
    dialogues: list[DialogueUnit],
    strings: list[StringUnit],
    language: str,
    dialogue_translations: dict[str, str],
    string_translations: dict[str, str],
    game_dir: Path,
    tl_root: Path | None = None,
    updated_at: str | None = None,
    progress_cb=None,
    *,
    retry_texts: set[str] | None = None,
    changed_ids: set[str] | None = None,
    accepted_originals: set[tuple[bool, str]] | None = None,
) -> list[Path]:
    """Add missing definitions or repair requested literals in place in their owning files."""
    if tl_root is None:
        tl_root = game_dir / "tl"
    out_dir = tl_root / language
    docs = index_tl_directory(out_dir, language)
    owners = _owners(docs)
    seen = set()
    changes: dict[Path, list[str]] = {}
    retry_texts = retry_texts or set()
    changed_ids = changed_ids or set()
    accepted_originals = accepted_originals or set()

    def needs_definition(is_string, key, source, value):
        lookup = (is_string, key)
        if value is None or lookup in seen:
            return False
        seen.add(lookup)
        if lookup not in owners:
            return True
        doc, entry = owners[lookup]
        changed = not is_string and key in changed_ids and source != entry.original
        fallback = value == source and lookup not in accepted_originals
        if changed and (entry.original_span is None or not value.strip() or fallback):
            return False
        if entry.translation is None or (entry.translation == value and not changed):
            return False
        if entry.translation and (not changed and (source not in retry_texts or fallback)):
            return False
        if doc.read_only or entry.span is None:
            raise TranslationMergeError(f"重译 {key!r} 需要对应的可编辑 .rpy 源文件：{doc.origin or doc.path}")
        if doc.path not in changes:
            changes[doc.path] = doc.lines.copy()
        lines = changes[doc.path]
        if changed:
            start, end = entry.original_span
            line = lines[entry.original_line]
            lines[entry.original_line] = line[:start] + encode_say_string(source) + line[end:]
        if entry.translation != value:
            lines[entry.line] = entry.with_translation(lines[entry.line], value)
        return False

    for src, (ds, ss) in sorted(group_by_source_file(dialogues, strings).items()):
        ds = [u for u in ds if needs_definition(False, u.identifier, u.what,
                                                dialogue_translations.get(u.identifier))]
        ss = [u for u in ss if needs_definition(True, u.text, u.text,
                                                string_translations.get(u.text))]
        if not (ds or ss):
            continue
        rel = Path(_rel_path(src, game_dir)).with_suffix(".rpy")
        if rel.is_absolute() or rel.drive or ".." in rel.parts:
            raise TranslationMergeError(f"源脚本路径不能写入翻译目录：{src}")
        out_file = editable_output_path(out_dir / rel, docs)
        script = build_file_script(ds, ss, language, dialogue_translations, string_translations,
                                   game_dir=game_dir, updated_at=updated_at)
        doc = docs.get(out_file)
        if out_file not in changes:
            changes[out_file] = doc.lines.copy() if doc else []
        lines = changes[out_file]
        newline = doc.newline if doc else "\n"
        if lines:
            lines.append(newline if lines[-1].endswith(("\r", "\n")) else newline * 2)
        lines.append(script.replace("\n", newline))

    written = []
    for idx, (out_file, lines) in enumerate(changes.items(), 1):
        write_if_unchanged(out_file, "".join(lines), docs.get(out_file))
        written.append(out_file)
        if progress_cb:
            progress_cb(idx, len(changes), str(out_file))
    return written
