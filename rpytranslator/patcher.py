# -*- coding: utf-8 -*-
"""
汉化后处理补丁（在翻译文件生成后自动执行）：

1. 中文字体补丁（解决中文显示为方框）
   - 解析字体文件 cmap 表，检测游戏自带字体是否含汉字字形
   - 没有则从系统复制中文字体（Windows 黑体/雅黑等）到 game/fonts/
   - 生成 game/zz_cn_font.rpy：用 FontGroup 让中文走中文字体、英文保留原字体

2. 语言切换界面注入（解决设置里没有语言选项）
   - 检测游戏的 preferences 屏幕是否已含语言按钮
   - 没有则提取 `screen preferences` 定义，在其末尾注入语言选择 vbox，
     生成 game/zz_language_ui.rpy（后定义同名屏幕覆盖原定义，不动原文件）
   - 同时生成 tl/<语言>/languages.rpy，让设置里语言显示为中文名
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .rpyc_loader import _load_unrpyc

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

_FONT_EXTS = (".ttf", ".ttc", ".otf")

# 系统候选中文字体（按优先级）。value 为 (路径, 显示名)。
_SYSTEM_FONTS = {
    "windows": [
        (r"C:\Windows\Fonts\simhei.ttf", "黑体"),
        (r"C:\Windows\Fonts\msyh.ttc", "微软雅黑"),
        (r"C:\Windows\Fonts\simsun.ttc", "宋体"),
    ],
    "darwin": [
        ("/System/Library/Fonts/PingFang.ttc", "苹方"),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", "冬青黑体"),
    ],
    "linux": [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "思源黑体"),
        ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "思源黑体"),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "文泉驿微米黑"),
    ],
}

# 用于判定字体是否支持中文的常用汉字码位（一/中/文/汉）
_CJK_TEST_CODEPOINTS = (0x4E00, 0x4E2D, 0x6587, 0x6C49)

# 排除的内部目录（与 engine._scan_files 保持一致）
_SKIP_DIRS = {"tl", "renpy", "cache", "saves", "log", "errors",
              "__pycache__", ".git", "lib"}


def _read_u16(data: bytes, off: int) -> int:
    return struct.unpack_from(">H", data, off)[0]


def _read_u32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def font_supports_cjk(path: Path) -> bool:
    """解析字体文件的 cmap 表，判断是否包含常用汉字字形。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if len(raw) < 12:
        return False
    try:
        if raw[:4] == b"ttcf":  # TTC 字体集合，取第一个
            num = _read_u32(raw, 8)
            if num < 1:
                return False
            sfnt_off = _read_u32(raw, 12)
        elif raw[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            sfnt_off = 0
        else:
            return False
        if sfnt_off + 12 > len(raw):
            return False
        num_tables = _read_u16(raw, sfnt_off + 4)
        cmap_off = None
        for i in range(num_tables):
            rec = sfnt_off + 12 + i * 16
            if rec + 16 > len(raw):
                break
            if raw[rec:rec + 4] == b"cmap":
                cmap_off = _read_u32(raw, rec + 8)
                break
        if cmap_off is None:
            return False
        num_cmaps = _read_u16(raw, cmap_off + 2)
        for i in range(num_cmaps):
            rec = cmap_off + 4 + i * 8
            if rec + 8 > len(raw):
                break
            sub_off = cmap_off + _read_u32(raw, rec + 4)
            if sub_off + 2 > len(raw):
                continue
            fmt = _read_u16(raw, sub_off)
            if fmt == 4:
                if sub_off + 14 > len(raw):
                    continue
                seg_x2 = _read_u16(raw, sub_off + 6)
                seg = seg_x2 // 2
                if seg <= 0 or sub_off + 14 + seg * 4 + 2 > len(raw):
                    continue
                end_codes = [
                    _read_u16(raw, sub_off + 14 + i * 2) for i in range(seg)
                ]
                start_off = sub_off + 14 + seg * 2 + 2
                for i in range(seg):
                    start = _read_u16(raw, start_off + i * 2)
                    end = end_codes[i]
                    if any(start <= cp <= end for cp in _CJK_TEST_CODEPOINTS):
                        return True
            elif fmt == 12:
                if sub_off + 16 > len(raw):
                    continue
                num_groups = _read_u32(raw, sub_off + 12)
                base = sub_off + 16
                for g in range(num_groups):
                    off = base + g * 12
                    if off + 12 > len(raw):
                        break
                    start = _read_u32(raw, off)
                    end = _read_u32(raw, off + 4)
                    if any(start <= cp <= end for cp in _CJK_TEST_CODEPOINTS):
                        return True
    except struct.error:
        return False
    return False


def find_game_cjk_font(game_dir: Path) -> Path | None:
    """在 game 目录（含子目录，排除 tl/renpy 等）中查找含中文字形的字体。"""
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() not in _FONT_EXTS:
                continue
            p = Path(dirpath) / fn
            if font_supports_cjk(p):
                return p
    return None


def copy_system_cjk_font(game_dir: Path) -> Path | None:
    """从系统复制一个中文字体到 game/fonts/，返回目标路径。"""
    key = "windows" if os.name == "nt" else sys.platform
    candidates = list(_SYSTEM_FONTS.get(key, []))
    # 非 Windows 也顺带尝试 Windows 常用字体（跨系统）
    if key != "windows":
        candidates += _SYSTEM_FONTS["windows"]
    out_dir = game_dir / "fonts"
    for src, _name in candidates:
        p = Path(src)
        if not p.is_file() or not font_supports_cjk(p):
            continue
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / ("cn_font" + p.suffix.lower())
            shutil.copy2(p, target)
            return target
        except OSError:
            continue
    return None


def find_original_default_font(game_dir: Path) -> str:
    """从游戏源码中提取默认字体路径（用于 FontGroup 兜底英文），找不到用 DejaVuSans。"""
    patterns = (
        r"gui\.text_font\s*=\s*[\"']([^\"']+)[\"']",
        r"style\.default\.font\s*=\s*[\"']([^\"']+)[\"']",
        r"config\.font\s*=\s*[\"']([^\"']+)[\"']",
    )
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    return m.group(1)
    return "DejaVuSans.ttf"


def _build_font_patch(cjk_font_rel: str, original_font: str) -> str:
    esc = cjk_font_rel.replace("\\", "/")
    return (
        "# -*- coding: utf-8 -*-\n"
        "# 中文字体补丁（汉化工具自动生成）：解决中文显示为方框。\n"
        f"# 中文字体: {cjk_font_rel}\n"
        "init 999 python:\n"
        "    style.default.font = FontGroup()\n"
        f"        .add(\"{esc}\", 0x2E80, 0x9FFF)   # CJK 部首/标点/统一表意文字\n"
        f"        .add(\"{esc}\", 0xF900, 0xFAFF)   # CJK 兼容表意文字\n"
        f"        .add(\"{esc}\", 0xFF00, 0xFFEF)   # 全角符号（中文引号等）\n"
        f"        .add(\"{original_font}\", 0x0000, 0x2E7F)  # 拉丁等（保留原观感）\n"
    )


@dataclass
class PatchResult:
    ok: bool = False
    skip: bool = False        # True = 无需处理（已具备）
    message: str = ""
    detail: str = ""
    files: list[Path] = field(default_factory=list)


def apply_font_patch(game_dir: Path) -> PatchResult:
    """确保游戏使用含中文字形的字体，返回结果。"""
    res = PatchResult()
    patch = game_dir / "zz_cn_font.rpy"

    # 已有补丁且字体文件仍存在 → 跳过
    if patch.is_file():
        text = patch.read_text(encoding="utf-8-sig", errors="ignore")
        m = re.search(r"^# 中文字体: (.+)$", text, re.M)
        if m and (game_dir / m.group(1).strip()).is_file():
            res.ok = True
            res.skip = True
            res.message = "中文字体补丁已存在，跳过"
            return res

    # 1. 优先使用游戏自带的中文字体
    cjk_font = find_game_cjk_font(game_dir)
    if cjk_font is not None:
        rel = cjk_font.relative_to(game_dir).as_posix()
        res.message = f"使用游戏自带中文字体: {rel}"
    else:
        # 2. 复制系统字体
        copied = copy_system_cjk_font(game_dir)
        if copied is None:
            res.ok = False
            res.message = "未找到中文字体，且系统无可用中文字体（中文可能显示为方框）"
            return res
        rel = copied.relative_to(game_dir).as_posix()
        res.files.append(copied)
        res.message = f"已复制系统字体到 {rel}"

    original_font = find_original_default_font(game_dir)
    try:
        patch.write_text(
            _build_font_patch(rel, original_font), encoding="utf-8-sig")
    except OSError as e:
        res.ok = False
        res.message = f"写字体补丁失败: {e}"
        return res
    res.files.append(patch)
    res.ok = True
    res.detail = f"中文字体: {rel}；英文保留原字体 {original_font}"
    res.message = f"已配置中文字体（{res.message}）"
    return res


# ---------------------------------------------------------------------------
# 语言切换界面
# ---------------------------------------------------------------------------

_LANG_UI_MARKER = "# ===== 语言切换（汉化工具自动生成） ====="

_LANG_UI_SNIPPET = (
    "        " + _LANG_UI_MARKER + "\n"
    "        vbox:\n"
    '            style_prefix "radio"\n'
    '            label _("Language")\n'
    "            for i in renpy.translation.get_languages():\n"
    "                $ _language = i\n"
    '                textbutton _language action Preference("language", i) '
    'style "radio_button"\n'
)

# 语言英文名 → 设置界面显示的中文名
_LANGUAGE_DISPLAY = {
    "schinese": ("简体中文", ("Simplified Chinese", "Chinese (Simplified)",
                              "Chinese", "English")),
    "tchinese": ("繁体中文", ("Traditional Chinese", "Chinese (Traditional)",
                             "Chinese", "English")),
    "zh_cn": ("简体中文", ("Simplified Chinese", "Chinese (Simplified)",
                          "Chinese", "English")),
    "zh_hans": ("简体中文", ("Simplified Chinese", "Chinese (Simplified)",
                            "Chinese", "English")),
    "zh": ("简体中文", ("Chinese", "Simplified Chinese", "English")),
}

_LANG_UI_PATTERNS = (
    r'Preference\(\s*["\']language["\']',
    r"renpy\.translation\.get_languages\s*\(",
    r"\blanguage_button\b",
)


def _extract_screen_block(text: str, screen_name: str) -> str | None:
    """提取 text 中 `screen <name>...:` 的完整定义块（含 screen 行）。"""
    lines = text.splitlines(keepends=True)
    start = None
    indent = 0
    for idx, line in enumerate(lines):
        m = re.match(r"( *screen\s+" + re.escape(screen_name) + r"\b)", line)
        if m:
            start = idx
            indent = len(m.group(1)) - len(m.group(1).lstrip())
            break
    if start is None:
        return None
    block = [lines[start]]
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            block.append(line)
            continue
        cur_indent = len(line) - len(line.lstrip())
        if stripped.startswith("#"):
            if cur_indent <= indent:
                break
            block.append(line)
            continue
        if cur_indent <= indent:
            break
        block.append(line)
    return "".join(block)


def _has_language_ui(game_dir: Path, language: str = "schinese") -> bool:
    """判断游戏是否已具备可切换到目标语言的语言界面。

    满足其一即视为已具备：
    1. 动态语言列表（get_languages / Preference("language") / language_button）
       —— 新语言会自动出现在列表中
    2. 硬编码语言按钮中包含目标语言标签（如 Language("schinese")）
    """
    # 硬编码按钮含目标语言
    hardcoded = (
        r'Language\(\s*["\']' + re.escape(language) + r'["\']',
        r'Language\(\s*["\']chinese["\']',
    )

    def _check(text: str) -> bool:
        if any(re.search(p, text) for p in _LANG_UI_PATTERNS):
            return True
        return any(re.search(p, text) for p in hardcoded)

    # 先扫 .rpy
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            if _check(text):
                return True
    # screens.rpyc 反编译检查
    for fn in ("screens.rpyc", "screens.rpymc"):
        p = game_dir / fn
        if not p.is_file():
            continue
        text = _decompile_to_text(p)
        if text and _check(text):
            return True
    return False


def _decompile_to_text(rpyc_path: Path) -> str | None:
    """反编译单个 .rpyc 为文本（临时目录，用完即删）。"""
    try:
        unrpyc = _load_unrpyc()
    except RuntimeError:
        return None
    with tempfile.TemporaryDirectory(prefix="rpy_scr_") as td:
        base = Path(td)
        tmp_in = base / rpyc_path.name
        try:
            shutil.copy2(rpyc_path, tmp_in)
            ctx = unrpyc.Context()
            unrpyc.decompile_rpyc(tmp_in, ctx)
        except Exception:
            return None
        target = base / (rpyc_path.stem + ".rpy")
        if target.is_file():
            try:
                return target.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                return None
    return None


def _find_preferences_source(game_dir: Path) -> tuple[str | None, Path | None]:
    """定位包含 preferences 屏幕定义的源文本。

    返回 (源码文本, 来源路径)。来源可能是 .rpy 源码或 .rpyc（已反编译）。
    """
    # 1. screens.rpy / screens.rpym 优先
    for fn in ("screens.rpy", "screens.rpym"):
        p = game_dir / fn
        if p.is_file():
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
            if re.search(r"(?m)^ *screen\s+preferences\b", text):
                return text, p
    # 2. screens.rpyc / screens.rpymc
    for fn in ("screens.rpyc", "screens.rpymc"):
        p = game_dir / fn
        if p.is_file():
            text = _decompile_to_text(p)
            if text and re.search(r"(?m)^ *screen\s+preferences\b", text):
                return text, p
    # 3. 全目录搜索（源码）
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            p = Path(dirpath) / fn
            try:
                text = p.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            if re.search(r"(?m)^ *screen\s+preferences\b", text):
                return text, p
    # 4. 全目录搜索（编译文件）
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() not in (".rpyc", ".rpymc"):
                continue
            p = Path(dirpath) / fn
            text = _decompile_to_text(p)
            if text and re.search(r"(?m)^ *screen\s+preferences\b", text):
                return text, p
    return None, None


def _build_language_ui(source_text: str) -> str | None:
    """在 preferences 屏幕块末尾注入语言选择器，返回新的整个屏幕定义。"""
    block = _extract_screen_block(source_text, "preferences")
    if block is None:
        return None
    if _LANG_UI_MARKER in block:
        return None  # 已注入
    block = block.rstrip() + "\n"
    return block + _LANG_UI_SNIPPET


def apply_language_ui(game_dir: Path, language: str = "schinese") -> PatchResult:
    """确保游戏设置界面有语言切换选项，返回结果。"""
    res = PatchResult()
    patch = game_dir / "zz_language_ui.rpy"

    # 1. 游戏已有语言切换 → 跳过
    if _has_language_ui(game_dir, language):
        res.ok = True
        res.skip = True
        res.message = "游戏已自带语言切换界面，跳过注入"
        return res
    # 2. 已有补丁 → 跳过
    if patch.is_file():
        try:
            text = patch.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            text = ""
        if _LANG_UI_MARKER in text:
            res.ok = True
            res.skip = True
            res.message = "语言切换界面已注入，跳过"
            return res

    # 3. 定位 preferences 屏幕并注入
    source, src_path = _find_preferences_source(game_dir)
    if source is None:
        res.ok = False
        res.message = "未找到 preferences 屏幕定义，无法注入语言切换界面"
        return res
    new_block = _build_language_ui(source)
    if new_block is None:
        res.ok = False
        res.message = "无法定位 preferences 屏幕，跳过语言 UI 注入"
        return res
    try:
        header = (
            "# -*- coding: utf-8 -*-\n"
            "# 语言切换界面（汉化工具自动生成）：为无语言按钮的游戏补上语言选择。\n"
            f"# 来源: {src_path.name}\n\n"
        )
        patch.write_text(header + new_block, encoding="utf-8-sig")
    except OSError as e:
        res.ok = False
        res.message = f"写语言界面补丁失败: {e}"
        return res
    res.files.append(patch)
    res.ok = True
    res.detail = "重启游戏后，在 设置 → 语言 中选择中文即可切换"
    res.message = "已注入语言切换界面（设置 → 语言）"
    return res


def ensure_language_name(game_dir: Path, language: str) -> PatchResult | None:
    """生成 tl/<语言>/languages.rpy，让设置里语言显示为中文名。"""
    tl_dir = game_dir / "tl" / language
    if not tl_dir.is_dir():
        return None
    res = PatchResult()
    f = tl_dir / "languages.rpy"
    names = _LANGUAGE_DISPLAY.get(language.lower())
    if names is None:
        cn, english = language, ("English",)
    else:
        cn, english = names
    body = f"# 语言显示名（汉化工具自动生成）\ntranslate {language} python:\n"
    for en in english:
        body += f"    _({en!r}) = {cn!r}\n"
    body += '    _("Language") = "语言"\n'
    # 已存在且内容相同 → 跳过
    if f.is_file():
        try:
            if f.read_text(encoding="utf-8-sig", errors="ignore") == body:
                res.ok = True
                res.skip = True
                res.message = "语言显示名已配置"
                return res
        except OSError:
            pass
    try:
        f.write_text(body, encoding="utf-8-sig")
    except OSError as e:
        res.ok = False
        res.message = f"写语言显示名失败: {e}"
        return res
    res.files.append(f)
    res.ok = True
    res.message = f"已设置语言显示名（tl/{language}/languages.rpy）"
    return res


def apply_all(game_dir: Path, language: str = "schinese",
              with_font: bool = True,
              with_language_ui: bool = True) -> list[PatchResult]:
    """依次执行全部后处理，返回各项结果（供日志展示）。"""
    results: list[PatchResult] = []
    if with_font:
        results.append(apply_font_patch(game_dir))
    if with_language_ui:
        results.append(apply_language_ui(game_dir, language))
        name_res = ensure_language_name(game_dir, language)
        if name_res is not None:
            results.append(name_res)
    return results
