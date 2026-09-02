# -*- coding: utf-8 -*-
"""
汉化后处理补丁（在翻译文件生成后自动执行）：

1. 中文字体补丁（解决中文显示为方框）
   - 解析字体文件 cmap 表，检测游戏自带字体是否含汉字字形
   - 没有则从系统复制中文字体（Windows 黑体/雅黑等）到 game/fonts/
   - 生成 game/zz_cn_font.rpy：用 FontGroup 让中文走中文字体、英文保留原字体
   - 关键经验（Wild Harmonies 实测）：仅设置 style.default.font 无法覆盖界面
     字体——Ren'Py 界面字体来自 gui.*_font 变量（screens.rpy 通过
     gui.text_properties() 取字体），必须同时覆盖 gui 字体变量 + default +
     全部命名样式；且中文字体必须是静态字体（可变字体在 SDL_ttf 下
     字形支持不完整，会导致部分汉字仍为方框）

2. 语言切换界面注入（解决设置里没有语言选项）
   - 检测游戏的 preferences 屏幕是否已含语言按钮
   - 没有则提取 `screen preferences` 定义，在其末尾注入语言选择 vbox，
     生成 game/zz_language_ui.rpy（后定义同名屏幕覆盖原定义，不动原文件）
   - 同时生成 tl/<语言>/languages.rpy，让设置里语言显示为中文名
"""
from __future__ import annotations

import ast
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

# preferences 屏幕定义行。兼容两种形式：
#   screen preferences():                      # .rpy 源码
#   init -501 screen preferences():            # 反编译 .rpyc 所得
_PREF_SCREEN_RE = re.compile(
    r"(?m)^ *(?:init\s+[+-]?\d+\s+)?screen\s+preferences\b")


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
    """生成 zz_cn_font.rpy。

    实测总结（Wild Harmonies 项目验证）：
    - 仅设置 style.default.font 不够：Ren'Py 界面字体大多来自
      gui.text_font / gui.interface_text_font 等变量（screens.rpy 通过
      gui.text_properties() 取字体），显式指定了字体的样式不会继承 default。
    - 因此必须同时覆盖：① gui.*_font 全部字体变量 ② style.default.font
      ③ 遍历所有命名样式统一替换为 FontGroup。
    - gui 不是 Python 模块，不能 import gui，直接在 init python 块中访问即可。
    - Ren'Py 8 的样式字典在 renpy.game.style.styles。
    - 中文必须用静态字体：可变字体（*Variable*.ttf）在 Ren'Py/SDL_ttf 下
      字形支持不完整，会导致部分汉字仍是方框。
    """
    esc = cjk_font_rel.replace("\\", "/")
    warn_var = (
        "\n# 注意：当前中文字体可能是可变字体（Variable Font），Ren'Py 对其支持不完整，\n"
        "# 若仍有部分汉字显示为方框，请将中文字体换成静态字体（如 SourceHanSansSC / NotoSansSC 静态版）。\n"
        if "variable" in cjk_font_rel.lower() else ""
    )
    return (
        "# -*- coding: utf-8 -*-\n"
        "# 中文字体补丁（汉化工具自动生成）：解决中文显示为方框。\n"
        f"# 中文字体: {cjk_font_rel}\n"
        "# 说明：Ren'Py 界面字体大多来自 gui.*_font 变量与命名样式，仅设置\n"
        "#       style.default.font 无法覆盖它们，因此这里同时覆盖三者。\n"
        f"{warn_var}"
        "init 999 python:\n"
        "    def _zh_cn_font():\n"
        "        fg = FontGroup()\n"
        f"        fg.add(\"{esc}\", 0x2E80, 0x9FFF)   # CJK 部首/标点/统一表意文字\n"
        f"        fg.add(\"{esc}\", 0xF900, 0xFAFF)   # CJK 兼容表意文字\n"
        f"        fg.add(\"{esc}\", 0xFF00, 0xFFEF)   # 全角符号（中文引号等）\n"
        f"        fg.add(\"{original_font}\", 0x0000, 0x2E7F)  # 拉丁等（保留原观感）\n"
        "        return fg\n"
        "\n"
        "    _zh_fg = _zh_cn_font()\n"
        "\n"
        "    # 1) 覆盖 gui 命名空间中的字体变量（screens.rpy 大量样式通过 gui.text_properties 取字体）\n"
        "    for _k in (\"text_font\", \"name_text_font\", \"interface_text_font\",\n"
        "               \"aboutpage_text_font\", \"curse_text_font\", \"button_text_font\",\n"
        "               \"choice_button_text_font\", \"savemenu_button_text_font\",\n"
        "               \"startbutton_text_font\"):\n"
        "        if hasattr(gui, _k):\n"
        "            setattr(gui, _k, _zh_fg)\n"
        "\n"
        "    # 2) 默认样式兜底\n"
        "    style.default.font = _zh_fg\n"
        "\n"
        "    # 3) 其余命名样式兜底（显式指定 Peignot/Vollkorn 等拉丁字体的按钮、菜单）\n"
        "    try:\n"
        "        _zh_styles = renpy.game.style.styles\n"
        "    except Exception:\n"
        "        _zh_styles = {}\n"
        "    for _n, _s in _zh_styles.items():\n"
        "        try:\n"
        "            _s.font = _zh_fg\n"
        "        except Exception:\n"
        "            pass\n"
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
    "            $ _lang_ui_names = _lang_ui_languages()\n"
    "            for _lang_ui_i in _lang_ui_names:\n"
    '                $ _lang_ui_label = "English" if _lang_ui_i is None else _lang_ui_i\n'
    '                textbutton _(_lang_ui_label) action '
    'Preference("language", _lang_ui_i) style "radio_button"\n'
)

# 语言列表辅助函数：Ren'Py 8.2+ 提供 renpy.translation.known_languages()，
# get_languages() 为更新版本 API，老版本需回退扫描 game/tl 目录。
_LANG_UI_HELPER = (
    "\n\n"
    "init python:\n"
    "    # 可用语言列表（含默认英文 None）。跨 Ren'Py 版本兼容。\n"
    "    def _lang_ui_languages():\n"
    "        langs = []\n"
    "        found = False\n"
    "        for _fn_name in ('known_languages', 'get_languages'):\n"
    "            _fn = getattr(renpy.translation, _fn_name, None)\n"
    "            if _fn is None:\n"
    "                continue\n"
    "            try:\n"
    "                _langs = [_i for _i in _fn() if _i is not None]\n"
    "            except Exception:\n"
    "                continue\n"
    "            langs = sorted(_langs)\n"
    "            found = True\n"
    "            break\n"
    "        if not found:\n"
    "            import os\n"
    "            _tl_dir = os.path.join(config.gamedir, 'tl')\n"
    "            if os.path.isdir(_tl_dir):\n"
    "                for _name in sorted(os.listdir(_tl_dir)):\n"
    "                    if _name.lower() != 'none' and os.path.isdir(\n"
    "                            os.path.join(_tl_dir, _name)):\n"
    "                        langs.append(_name)\n"
    "        return [None] + langs\n"
)

# 各语言语言码对应的「Ren'Py 语言显示名 → 中文显示名」
# 注意：English 不能统一译为目标语言名，否则语言切换菜单里会全是“简体中文”。
_LANGUAGE_DISPLAY: dict[str, dict[str, str]] = {
    "schinese": {
        "schinese": "简体中文",
        "Simplified Chinese": "简体中文",
        "Chinese (Simplified)": "简体中文",
        "Chinese": "中文",
        "English": "英语",
    },
    "tchinese": {
        "tchinese": "繁體中文",
        "Traditional Chinese": "繁体中文",
        "Chinese (Traditional)": "繁体中文",
        "Chinese": "中文",
        "English": "英语",
    },
    "zh_cn": {
        "zh_cn": "简体中文",
        "Simplified Chinese": "简体中文",
        "Chinese (Simplified)": "简体中文",
        "Chinese": "中文",
        "English": "英语",
    },
    "zh_hans": {
        "zh_hans": "简体中文",
        "Simplified Chinese": "简体中文",
        "Chinese (Simplified)": "简体中文",
        "Chinese": "中文",
        "English": "英语",
    },
    "zh": {
        "zh": "中文",
        "Chinese": "中文",
        "Simplified Chinese": "简体中文",
        "English": "英语",
    },
}

_LANG_UI_PATTERNS = (
    r'Preference\(\s*["\']language["\']',
    r"renpy\.translation\.(?:get_languages|known_languages)\s*\(",
    r"\blanguage_button\b",
)


def _extract_screen_block(text: str, screen_name: str) -> str | None:
    """提取 text 中 `screen <name>...:` 的完整定义块（含 screen 行）。

    兼容 `.rpy` 源码（`screen xxx():`）与反编译 `.rpyc`
    （`init -501 screen xxx():`）两种形式。
    """
    lines = text.splitlines(keepends=True)
    start = None
    indent = 0
    pat = re.compile(
        r"( *)(?:init\s+[+-]?\d+\s+)?screen\s+" + re.escape(screen_name) + r"\b")
    for idx, line in enumerate(lines):
        m = pat.match(line)
        if m:
            start = idx
            indent = len(m.group(1))
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
        text = _decompile_rpyc_source(p, p.name)
        if text and _check(text):
            return True
    return False


def _decompile_rpyc_source(src: Path | bytes, name: str) -> str | None:
    """反编译单个 .rpyc（磁盘文件或内存字节）为脚本文本。

    name 用于生成临时文件与目标文件名（如 "screens.rpyc"）。用完即删。
    """
    try:
        unrpyc = _load_unrpyc()
    except RuntimeError:
        return None
    with tempfile.TemporaryDirectory(prefix="rpy_scr_") as td:
        base = Path(td)
        tmp_in = base / name
        try:
            if isinstance(src, bytes):
                tmp_in.write_bytes(src)
            else:
                shutil.copy2(src, tmp_in)
            ctx = unrpyc.Context()
            unrpyc.decompile_rpyc(tmp_in, ctx)
        except Exception:
            return None
        target = base / (Path(name).stem + ".rpy")
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
            if re.search(_PREF_SCREEN_RE, text):
                return text, p
    # 2. screens.rpyc / screens.rpymc
    for fn in ("screens.rpyc", "screens.rpymc"):
        p = game_dir / fn
        if p.is_file():
            text = _decompile_to_text(p)
            if text and re.search(_PREF_SCREEN_RE, text):
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
            if re.search(_PREF_SCREEN_RE, text):
                return text, p
    # 4. 全目录搜索（编译文件）
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() not in (".rpyc", ".rpymc"):
                continue
            p = Path(dirpath) / fn
            text = _decompile_rpyc_source(p, p.name)
            if text and re.search(_PREF_SCREEN_RE, text):
                return text, p
    # 5. 脚本打包在 .rpa 归档中：game 目录没有 screens 源码/编译文件，
    #    从归档读取 screens.rpyc（或同源 .rpy）反编译提取屏幕定义。
    rpas = [p for p in game_dir.glob("*.rpa")
            if p.is_file() and p.suffix.lower() == ".rpa"]
    if rpas:
        from . import rpa_loader
        for cand in ("screens.rpyc", "screens.rpymc",
                     "screens.rpy", "screens.rpym"):
            data = rpa_loader.read_script_data(rpas, cand)
            if not data:
                continue
            if cand.endswith((".rpy", ".rpym")):
                text = data.decode("utf-8-sig", errors="ignore")
            else:
                text = _decompile_rpyc_source(data, cand)
            if text and re.search(_PREF_SCREEN_RE, text):
                return text, None  # 来源归档内（无磁盘路径）
    return None, None


def _build_language_ui(source_text: str) -> str | None:
    """在 preferences 屏幕块末尾注入语言选择器，返回新的整个屏幕定义。"""
    block = _extract_screen_block(source_text, "preferences")
    if block is None:
        return None
    if _LANG_UI_MARKER in block:
        return None  # 已注入
    block = block.rstrip() + "\n"
    return block + _LANG_UI_SNIPPET + _LANG_UI_HELPER


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
            f"# 来源: {src_path.name if src_path else '归档内 screens 脚本'}\n\n"
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


_LANG_DISPLAY_FILE = "zz_language_display.rpy"


def _existing_old_strings(tl_dir: Path) -> set[str]:
    """收集 tl 目录中已生成翻译文件的全部字符串翻译 old（还原为原文本）。

    用于避免语言显示名文件与翻译文件重复定义同一字符串——Ren'Py 的字符串
    翻译全局唯一，同语言下 `old "Language"` 只能出现一次，否则运行时报
    "A translation for ... already exists"。
    """
    olds: set[str] = set()
    if not tl_dir.is_dir():
        return olds
    for f in tl_dir.glob("*.rpy"):
        if f.name == _LANG_DISPLAY_FILE:
            continue
        try:
            text = f.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"^\s*old\s+(.+?)\s*$", text, re.M):
            val = m.group(1).strip()
            try:
                olds.add(ast.literal_eval(val))
            except Exception:
                olds.add(val)
    return olds


def ensure_language_name(game_dir: Path, language: str) -> PatchResult | None:
    """生成 tl/<语言>/zz_language_display.rpy，让设置里语言显示为中文名。

    仅补充「翻译文件未覆盖」的语言名/界面字符串：若源文件中已出现
    "Language"（Ren'Py 模板的偏好设置里普遍存在），翻译流程会生成
    `old "Language"`，此处必须跳过，避免字符串翻译重复定义冲突。
    """
    tl_dir = game_dir / "tl" / language
    if not tl_dir.is_dir():
        return None
    res = PatchResult()
    # 用 zz_ 前缀的独立文件名，避免与源文件 languages.rpy 生成的翻译文件同名覆盖
    f = tl_dir / _LANG_DISPLAY_FILE
    mappings = _LANGUAGE_DISPLAY.get(language.lower())
    if mappings is None:
        # 未知语言码：兜底只保留 English 不翻译（避免意外覆盖）
        mappings = {"English": "English"}
    existing = _existing_old_strings(tl_dir)
    body = f"# 语言显示名（汉化工具自动生成）\ntranslate {language} strings:\n"
    for en, cn in mappings.items():
        if en not in existing:
            body += f"    old {en!r}\n    new {cn!r}\n"
    if "Language" not in existing:
        body += '    old "Language"\n    new "语言"\n'
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
    res.message = f"已设置语言显示名（tl/{language}/{_LANG_DISPLAY_FILE}）"
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
