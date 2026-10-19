"""GUI 中无需依赖 WinUI 的状态判断与日志批处理。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from . import engine


def has_recoverable_translation(
    game_path: str | Path, language: str, script_dir: Path | None = None,
) -> bool:
    """仅检查已有断点文件；探测时不扫描全文、不自动开始翻译。"""
    root = Path(game_path)
    if not root.is_dir():
        return False
    try:
        actual = script_dir or engine._find_game_dir(root)
        if actual is None:
            return False
        tl_dir = actual / "tl"
        return (tl_dir / f".{language}.汉化进度.json").is_file() or (
            tl_dir / f".{language}.暂停标记.txt").is_file()
    except OSError:
        return False


def pending_matches(pending: object, game_path: str, language: str) -> bool:
    """记录刚启动但还未来得及写断点的任务，以便重启后也给出提示。"""
    if not isinstance(pending, dict):
        return False
    saved_path = pending.get("game_path")
    return (isinstance(saved_path, str)
            and pending.get("language") == language
            and os.path.normcase(os.path.abspath(saved_path))
            == os.path.normcase(os.path.abspath(game_path)))


def should_follow_log(offset: float, scrollable_height: float) -> bool:
    """用户停在日志末尾时才自动跟随新内容。"""
    return offset >= max(0.0, scrollable_height - 24.0)


def group_log_entries(entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """合并相邻同级日志，减少 WinUI Run 数量和布局次数。"""
    groups: list[tuple[str, str]] = []
    current_level: str | None = None
    lines: list[str] = []
    for text, level in entries:
        if current_level is not None and level != current_level:
            groups.append((current_level, "".join(lines)))
            lines = []
        current_level = level
        lines.append(text)
        lines.append("\n")
    if current_level is not None:
        groups.append((current_level, "".join(lines)))
    return groups
