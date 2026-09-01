# -*- coding: utf-8 -*-
"""
翻译文件生成器：按源文件生成 tl/<语言>/ 下的 .rpy 翻译文件。

输出格式与 Ren'Py 官方 TranslationGenerator 逐字节一致：

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
import re
from collections import defaultdict
from pathlib import Path

from .extract import DialogueUnit, StringUnit, encode_say_string

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
    lines: list[str] = []
    who = unit.who
    # 原句注释：who 可选
    comment = encode_say_string(unit.what)
    if who:
        comment = who + " " + comment
    lines.append("    # " + comment)
    # 译文行
    new_comment = encode_say_string(translated)
    if who:
        new_comment = who + " " + new_comment
    lines.append("    " + new_comment)
    return "\n".join(lines)


def _string_body(text: str, translated: str) -> str:
    return "    old " + encode_say_string(text) + "\n    new " + encode_say_string(translated)


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
) -> list[Path]:
    """生成全部 tl/<语言>/ 翻译文件，返回写出文件路径列表。"""
    if tl_root is None:
        tl_root = game_dir / "tl"
    out_dir = tl_root / language
    out_dir.mkdir(parents=True, exist_ok=True)

    grouped = group_by_source_file(dialogues, strings)
    written: list[Path] = []
    # Ren'Py 的字符串翻译全局唯一：同一字符串（如界面文本 "Language"）出现在
    # 多个源文件（screens.rpy、languages.rpy）时，只能在一个 tl 文件中定义一次，
    # 否则运行时报 "A translation for ... already exists"。
    # 按文件排序顺序，把字符串分配给第一个出现的文件，其余文件跳过。
    seen_strings: set[str] = set()
    for idx, (src, (ds, ss)) in enumerate(sorted(grouped.items()), 1):
        ss_dedup: list[StringUnit] = []
        for s in ss:
            if s.text not in seen_strings:
                seen_strings.add(s.text)
                ss_dedup.append(s)
        script = build_file_script(
            ds, ss_dedup, language, dialogue_translations, string_translations,
            game_dir=game_dir, updated_at=updated_at,
        )
        if not script:
            continue
        # 目标路径与源文件相对 game 目录的结构一致（保留子目录），
        # 避免 days/route_aelfric/day_6.rpy 与 days/route_ulrich/day_6.rpy
        # 等不同目录的同名文件互相覆盖。
        rel = _rel_path(src, game_dir) if game_dir else Path(src).name
        out_file = out_dir / rel
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(script, encoding="utf-8-sig")
        written.append(out_file)
        if progress_cb:
            progress_cb(idx, len(grouped), str(out_file))
    return written
