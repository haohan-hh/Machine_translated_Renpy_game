# -*- coding: utf-8 -*-
"""
游戏检测与文本文件扫描：
- 定位 Ren'Py 游戏目录（game/）
- 扫描 .rpy / .rpym / .rpyc / .rpymc 文本文件
- 检测游戏是否已自带汉化（tl/ 目录）
- 粗略识别 Ren'Py 版本
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import rpa_loader

# 常见的中文语言标识（tl 目录名）
CHINESE_LANGUAGE_NAMES = {
    "chinese", "chinese_simplified", "simplifiedchinese", "simplified_chinese",
    "zh", "zh_cn", "zh-hans", "zh_CN", "zh_Hans", "zh_hans", "zhs",
    "schinese", "simplified",
}

SCRIPT_EXTS = (".rpy", ".rpym")
COMPILED_EXTS = (".rpyc", ".rpymc")
ALL_EXTS = SCRIPT_EXTS + COMPILED_EXTS


@dataclass
class GameInfo:
    """一次扫描得到的游戏信息。"""
    root: Path                 # 用户选择的目录
    game_dir: Path | None = None  # 实际游戏脚本目录（含 .rpy/.rpyc 的目录）
    rpy_files: list[Path] = field(default_factory=list)
    rpyc_files: list[Path] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)   # tl/ 下的语言列表
    has_chinese: bool = False
    version_hint: str = ""
    notes: list[str] = field(default_factory=list)
    # 脚本打包在 .rpa 归档中（游戏目录无松散 .rpy/.rpyc）时的归档信息
    rpa_files: list[Path] = field(default_factory=list)
    archive_scripts: list[str] = field(default_factory=list)

    @property
    def text_count(self) -> int:
        n = len(self.rpy_files) + len(self.rpyc_files)
        return n if n else len(self.archive_scripts)


def is_chinese_language(name: str) -> bool:
    """判断语言标识是否属于中文（schinese/tchinese/zh_cn…）。"""
    return name.lower() in CHINESE_LANGUAGE_NAMES


# 向后兼容别名
_is_chinese_language = is_chinese_language


def _find_game_dir(path: Path) -> Path | None:
    """在用户选择的目录中定位游戏脚本目录。
    优先寻找名为 game 的目录；否则查找直接包含 .rpy/.rpyc 的目录。"""
    if not path.is_dir():
        return None

    # 1. 直接是 game 目录
    if path.name.lower() == "game":
        return path

    # 2. 包含 game 子目录
    for child in path.iterdir():
        if child.is_dir() and child.name.lower() == "game":
            return child

    # 3. 目录内直接含 .rpy/.rpyc
    try:
        for child in path.iterdir():
            if child.is_file() and child.suffix.lower() in ALL_EXTS:
                return path
    except OSError:
        pass

    # 4. 子目录中含 .rpy/.rpyc（仅一层，避免误入 tl/renpy/ 等目录）
    for child in path.iterdir():
        if not child.is_dir():
            continue
        base = child.name.lower()
        if base in ("tl", "renpy", "cache", "saves", "log", "errors", "game") and base != "game":
            continue
        try:
            if any(f.is_file() and f.suffix.lower() in ALL_EXTS for f in child.iterdir()):
                return child
        except OSError:
            pass

    return None


def _scan_files(game_dir: Path) -> tuple[list[Path], list[Path]]:
    """递归扫描 game 目录下的脚本/编译文件，排除 tl/、renpy/ 等内部目录。"""
    rpy_files: list[Path] = []
    rpyc_files: list[Path] = []

    skip_dirs = {"tl", "renpy", "cache", "saves", "log", "errors", "__pycache__", ".git", "lib"}
    # renpy/ 目录在 8.x 中通常位于 game 外部；但旧版游戏可能内嵌

    for dirpath, dirnames, filenames in os.walk(game_dir):
        # 就地过滤目录
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            # 跳过工具自动生成的补丁文件（zz_ 前缀）
            if fn.lower().startswith("zz_"):
                continue
            p = Path(dirpath) / fn
            if ext in SCRIPT_EXTS:
                rpy_files.append(p)
            elif ext in COMPILED_EXTS:
                rpyc_files.append(p)

    rpy_files.sort(key=lambda p: str(p).lower())
    rpyc_files.sort(key=lambda p: str(p).lower())
    return rpy_files, rpyc_files


def _scan_languages(game_dir: Path) -> list[str]:
    """读取 tl/ 目录下的语言标识。"""
    tl_dir = game_dir / "tl"
    if not tl_dir.is_dir():
        return []
    languages = []
    for child in tl_dir.iterdir():
        if child.is_dir() and child.name.lower() != "none":
            languages.append(child.name)
    return sorted(set(languages), key=str.lower)


def _detect_version(game_dir: Path, root: Path) -> str:
    """粗略识别 Ren'Py 版本。"""
    # 1. 引擎自带版本文件（部分游戏打包时保留）
    candidates = [
        game_dir / "renpy" / "common" / "00version.rpy",
        root / "renpy" / "common" / "00version.rpy",
    ]
    for c in candidates:
        if c.is_file():
            try:
                text = c.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'version\s*=\s*"([^"]+)"', text)
                if m:
                    return m.group(1)
            except OSError:
                pass

    # 2. game/script_version.txt（部分发行版携带，格式如 "(8, 2, 0)"）
    sv = game_dir / "script_version.txt"
    if sv.is_file():
        try:
            m = re.search(r"\(?\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
                          sv.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
        except OSError:
            pass

    # 3. 通过可执行文件判断（8.x 通常使用 renpy 可执行文件，7.x 为 .exe）
    for ext in (".exe", ""):
        for name in ("renpy", "game", root.name):
            p = root / (name + ext)
            if p.is_file():
                return "Ren'Py (8.x/7.x 系列)"

    # 4. rpyc 文件头判断
    if game_dir:
        for f in list(game_dir.glob("*.rpyc"))[:5]:
            try:
                head = f.read_bytes()[:11]
                if head.startswith(b"RENPY RPC2"):
                    return "Ren'Py 8.x / 7.4+（新版 .rpyc）"
            except OSError:
                pass

    return "未知"


def _detect_rpa_archives(info: GameInfo, game_dir: Path) -> None:
    """当游戏目录没有松散脚本时，尝试从 .rpa 归档中识别脚本。

    Ren'Py 发行版常见做法是把全部脚本（连同资源）打包进 game/archive.rpa，
    game 目录只剩缓存与资源文件。此处只读取归档索引、统计脚本清单，
    实际物化脚本的工作由 pipeline 在开始汉化时执行。
    """
    try:
        rpas = list(game_dir.glob("*.rpa")) + list(game_dir.glob("*.RPA"))
    except OSError:
        return
    if not rpas:
        return
    seen: set[str] = set()
    rpas = [p for p in rpas if p.name not in seen and not seen.add(p.name)]
    rpas.sort(key=lambda p: str(p).lower())

    scripts: list[str] = []
    for rpa in rpas:
        try:
            names = rpa_loader.list_script_names(rpa)
        except (ValueError, OSError):
            continue
        # 多个归档同名脚本：后处理的归档覆盖先前的（Ren'Py config.archives
        # 顺序），以最后一个归档为准。
        for n in names:
            scripts = [s for s in scripts if s.lower() != n.lower()]
            scripts.append(n)
    if scripts:
        info.rpa_files = rpas
        info.archive_scripts = scripts
        info.notes.append(
            f"发现脚本归档：{'、'.join(p.name for p in rpas)}"
            f"（内含 {len(scripts)} 个脚本，汉化时自动解包处理）")
    else:
        info.notes.append("发现 .rpa 归档，但其中不包含 .rpy/.rpyc 脚本")


def scan_game(path: str | Path) -> GameInfo:
    """扫描指定路径下的 Ren'Py 游戏。"""
    root = Path(path).expanduser().resolve()
    info = GameInfo(root=root)

    if not root.exists():
        info.notes.append(f"路径不存在: {root}")
        return info

    if root.is_file():
        root = root.parent
        info.root = root

    game_dir = _find_game_dir(root)
    if game_dir is None:
        info.notes.append("未找到 Ren'Py 游戏脚本目录（未发现 game/ 目录或 .rpy/.rpyc 文件）")
        return info

    info.game_dir = game_dir
    info.rpy_files, info.rpyc_files = _scan_files(game_dir)
    info.languages = _scan_languages(game_dir)
    info.has_chinese = any(_is_chinese_language(l) for l in info.languages)
    info.version_hint = _detect_version(game_dir, root)

    if not info.rpy_files and not info.rpyc_files:
        # 脚本可能打包在 .rpa 归档中（Ren'Py 发行版常见做法）
        _detect_rpa_archives(info, game_dir)
    if not info.rpy_files and not info.rpyc_files and not info.archive_scripts:
        info.notes.append("游戏目录中未发现任何文本脚本文件")

    return info
