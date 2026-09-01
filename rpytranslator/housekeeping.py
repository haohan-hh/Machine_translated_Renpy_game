# -*- coding: utf-8 -*-
"""
汉化后自动化处理（每次汉化完成后自动执行）：

1. 删除“有对应 .rpy/.rpym 源文件”的 .rpyc/.rpymc
   Ren'Py 运行时优先加载 .rpyc；若 .rpyc 早于本次汉化（没有对应翻译的
   identifier），或与 .rpy 内容不一致（游戏升级后 .rpyc 更新而 .rpy 未变），
   翻译会因 identifier 不匹配而失效，游戏仍显示英文。
   删除后 Ren'Py 下次启动会从 .rpy + tl/ 重新编译，保证翻译生效。
   只删除有源码的编译缓存，绝不触碰仅有 .rpyc（未发布 .rpy）的脚本，
   避免误删导致游戏内容丢失。

2. 清理 tl/<语言> 下翻译文件中的重复 translate 块
   同一 identifier 重复定义时 Ren'Py 行为不确定（可能忽略后面的翻译、
   甚至在重新编译时报错）。保留每个 identifier 首次出现的块，删除后续重复。
"""
from __future__ import annotations

import re
from pathlib import Path

_TRANSLATE_RE = re.compile(r"\s*translate\s+(\S+)\s+(\S+):\s*$")


def remove_stale_rpyc(game_dir: Path) -> int:
    """删除有对应 .rpy/.rpym 源文件的 .rpyc/.rpymc，返回删除数量。

    只删除“源文件存在”的编译缓存：Ren'Py 总能从源文件重新编译出等价
    （且与 tl 翻译 identifier 一致）的 .rpyc。只有 .rpyc 没有源文件的
    脚本一律保留，防止内容丢失。
    """
    removed = 0
    if not game_dir.is_dir():
        return 0
    candidates = (list(game_dir.rglob("*.rpyc"))
                  + list(game_dir.rglob("*.rpymc")))
    for rpyc in candidates:
        rel = rpyc.relative_to(game_dir)
        src_rel = rel.with_suffix(".rpy" if rel.suffix == ".rpyc" else ".rpym")
        if (game_dir / src_rel).exists():
            try:
                rpyc.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def dedupe_translate_blocks(tl_lang_dir: Path) -> int:
    """清理 tl/<语言> 目录下重复的 translate 块，返回删除的块数。

    以 `translate <语言> <identifier>:` 行为块边界，同一 (语言, identifier)
    只保留首次出现的块。字符串块 `translate <语言> strings:` 同样按规则去重
    （同文件内只保留第一个）。逐行重建文件，不改动其余内容与注释。
    """
    if not tl_lang_dir.is_dir():
        return 0
    removed = 0
    for f in sorted(tl_lang_dir.rglob("*.rpy")):
        try:
            text = f.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        lines = text.splitlines(keepends=True)
        # 收集所有 translate 块: (块起始行, 块结束行, 语言|identifier)
        blocks: list[tuple[int, int, str]] = []
        start: int | None = None
        key = ""
        for i, ln in enumerate(lines):
            m = _TRANSLATE_RE.match(ln)
            if m:
                if start is not None:
                    blocks.append((start, i, key))
                start, key = i, m.group(1) + "|" + m.group(2)
        if start is not None:
            blocks.append((start, len(lines), key))
        if not blocks:
            continue

        seen: set[str] = set()
        drop_ranges: list[tuple[int, int]] = []
        for start, end, key in blocks:
            if key in seen:
                drop_ranges.append((start, end))
            else:
                seen.add(key)
        if not drop_ranges:
            continue

        kept: list[str] = []
        cursor = 0
        for start, end in drop_ranges:
            kept.extend(lines[cursor:start])
            cursor = end
            removed += 1
        kept.extend(lines[cursor:])
        try:
            f.write_text("".join(kept), encoding="utf-8-sig")
        except OSError:
            continue
    return removed
