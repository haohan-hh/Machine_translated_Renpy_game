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

import filecmp
import os
import re
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import rpa_loader
from .engine import is_chinese_language
from .rpyc_loader import _load_unrpyc
from .tl_files import TranslationMergeError, editable_output_path, index_tl_directory, write_if_unchanged

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

# Game templates commonly put preferences at init -501. Ren'Py reserves
# priorities outside -999..999 for internal use, so use the highest supported
# project priority for the loose override.
_LANGUAGE_UI_PRIORITY = 999


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


def _list_game_fonts(game_dir: Path) -> list[str]:
    """枚举 game/font 与 game/fonts 下的字体路径（相对 game 前缀）。"""
    out: list[str] = []
    seen: set[str] = set()
    for sub in ("font", "fonts"):
        d = game_dir / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            ext = p.suffix.lower()
            if ext not in _FONT_EXTS:
                continue
            try:
                rel = p.relative_to(game_dir).as_posix()
            except ValueError:
                continue
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def _list_non_cjk_fonts(game_dir: Path, cjk_font: Path | None) -> list[str]:
    """枚举不含 CJK 字形的字体（用于 config.font_replacement_map）。"""
    cjk_str = str(cjk_font.resolve()).lower() if cjk_font else ""
    out: list[str] = []
    for rel in _list_game_fonts(game_dir):
        try:
            p = (game_dir / rel).resolve()
            if cjk_str and str(p).lower() == cjk_str:
                continue
            if font_supports_cjk(p):
                continue
        except OSError:
            pass
        out.append(rel)
    return out


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
    if key == "windows":
        # Prefer the font selected for this user's Windows interface. LOGFONTW
        # stores lfFaceName after the 28-byte numeric header; the system Fonts
        # registry maps the localized face name to its installed font file.
        try:
            import winreg

            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Control Panel\Desktop\WindowMetrics") as metrics:
                raw, _kind = winreg.QueryValueEx(metrics, "MessageFont")
            face = raw[28:92].decode("utf-16-le", errors="ignore").split("\0", 1)[0].strip()
            preferred = []
            roots = ((winreg.HKEY_CURRENT_USER, Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"),
                     (winreg.HKEY_LOCAL_MACHINE, Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"))
            for hive, directory in roots if face else ():
                try:
                    fonts_key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
                except OSError:
                    continue
                with fonts_key as fonts:
                    for index in range(winreg.QueryInfoKey(fonts)[1]):
                        name, filename, _kind = winreg.EnumValue(fonts, index)
                        if face.casefold() in name.casefold():
                            path = str(directory / filename)
                            variant = name.casefold()
                            is_variant = any(tag in variant for tag in (
                                " bold", " light", " italic", " semibold",
                                " black", " condensed"))
                            preferred.append((is_variant, path, face))
            preferred.sort(key=lambda item: item[0])
            candidates = [(path, face) for _, path, face in preferred] + candidates
        except (OSError, ImportError, ValueError):
            pass
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
            if not target.is_file() or not filecmp.cmp(p, target, shallow=False):
                shutil.copy2(p, target)
            return target
        except OSError:
            continue
    return None


_FONT_PATTERNS = (
    r"gui\.text_font\s*=\s*[\"']([^\"']+)[\"']",
    r"style\.default\.font\s*=\s*[\"']([^\"']+)[\"']",
    r"config\.font\s*=\s*[\"']([^\"']+)[\"']",
)


def _scan_font_text(text: str) -> str | None:
    for pat in _FONT_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _archive_sources(game_dir: Path, accepts):
    """Read effective archived scripts, skipping paths shadowed by loose files."""
    archives = rpa_loader.find_archives(game_dir)
    for archive, name, entry in rpa_loader.iter_script_entries(archives):
        rel = Path(name)
        if (any(part.lower() in _SKIP_DIRS for part in rel.parts[:-1])
                or rel.name.lower().startswith("zz_") or not accepts(rel)):
            continue
        loose = game_dir / rel
        source_suffix = rel.suffix.lower().removesuffix("c")
        if loose.with_suffix(source_suffix).is_file() or loose.with_suffix(source_suffix + "c").is_file():
            continue
        try:
            data = rpa_loader.read_entry(archive, entry)
        except (OSError, ValueError):
            continue
        if rel.suffix.lower().endswith("c"):
            text = _decompile_rpyc_source(data, rel.name)
        else:
            text = data.decode("utf-8-sig", errors="replace")
        if text:
            yield f"{archive.name}::{name}", text


def find_original_default_font(game_dir: Path) -> str:
    """从游戏源码（含 .rpa 归档）提取默认字体路径，找不到用 DejaVuSans。

    仅返回磁盘上实际存在的字体名，避免 FontGroup 引用归档内字体导致报错。
    """
    # 1. 游戏的松散脚本（跳过上次生成的字体覆盖）
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            if fn.lower().startswith("zz_") or os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            hit = _scan_font_text(text)
            if hit and (game_dir / hit).is_file():
                return hit
    # 2. 脚本在 .rpa 归档中：读 gui/options 源码或反编译 .rpyc
    for _label, text in _archive_sources(
            game_dir, lambda path: path.stem.lower() in ("gui", "options", "screens")):
        hit = _scan_font_text(text)
        if hit and (game_dir / hit).is_file():
            return hit
    return "DejaVuSans.ttf"


def _build_font_patch(cjk_font_rel: str, original_font: str,
                      replacement_fonts: list[str] | None = None) -> str:
    """生成 zz_cn_font.rpy。

    实测总结（Wild Harmonies / Dawn Chorus 项目验证）：
    - 仅设置 style.default.font 不够：Ren'Py 界面字体大多来自
      gui.text_font / gui.interface_text_font 等变量，screens.rpy 在 init 阶段
      通过 gui.text_properties() 构建样式，把字体字符串直接写死进样式对象。
    - 因此必须在**样式构建之前**（init -1）就把 gui.*_font 换成中文字体；
      init 999 再兜底：重新覆盖 gui 变量、style.default.font。
    - gui 不是 Python 模块，不能 import gui，直接在 init python 块中访问即可。
    - 遍历样式注册表用 .items() 即可（8.2 中 renpy.style.styles 是类映射对象
      而非 dict，len=207；renpy/lint.py 正是用其 .items() 遍历）。
    - 中文必须用静态字体：可变字体（*Variable*.ttf）在 Ren'Py/SDL_ttf 下
      字形支持不完整，会导致部分汉字仍是方框。
    - 上面的覆盖仍漏掉屏幕里**显式**写 `font "xxx.ttf"` 的元素（属性优先级
      最高）。这层用 config.font_replacement_map 把所有游戏字体重定向到中文
      字体——Ren'Py 在加载字体时即替换，绕过 screen 的 font= 属性。
    - **设置界面字体选项**：中文字体补丁使用当前系统可用的中文字体，
      设置界面只提供「系统字体」选项。init 999 定义 `_zz_set_font(f)`
      （gui 变量 + 默认样式 + 全部命名样式 + font_replacement_map 一站式
      应用）与 `_zz_font_select(choice)`（设置界面调用：记住选择并即时
      重建样式）。启动时按 persistent._zz_font_choice 应用上次选择。
      「字体」单选出现在 zz_language_ui.rpy 注入的设置界面里。
    """
    esc = cjk_font_rel.replace("\\", "/")
    warn_var = (
        "\n# 注意：当前中文字体可能是可变字体（Variable Font），Ren'Py 对其支持不完整，\n"
        "# 若仍有部分汉字显示为方框，请将中文字体换成静态字体（如 SourceHanSansSC / NotoSansSC 静态版）。\n"
        if "variable" in cjk_font_rel.lower() else ""
    )
    gui_keys = (
        "text_font", "name_text_font", "interface_text_font",
        "aboutpage_text_font", "curse_text_font", "button_text_font",
        "choice_button_text_font", "savemenu_button_text_font",
        "startbutton_text_font",
    )
    gui_keys_lit = ", ".join(f'"{k}"' for k in gui_keys)

    # font_replacement_map 要重定向的游戏原字体（去重）
    rep_fonts = list(replacement_fonts or [])
    if original_font and original_font not in rep_fonts:
        rep_fonts.append(original_font)
    seen = set()
    rep_unique = []
    for f in rep_fonts:
        if f and f not in seen and f != esc:
            seen.add(f)
            rep_unique.append(f)
    rep_list_lit = ", ".join(repr(f) for f in rep_unique)

    return (
        "# -*- coding: utf-8 -*-\n"
        "# 中文字体补丁（汉化工具自动生成）：解决中文显示为方框。\n"
        f"# 中文字体: {cjk_font_rel}\n"
        "# 说明：界面样式在 init 阶段读取 gui.*_font 构建并写死字体，因此先于\n"
        "#       样式构建（init -1）把 gui.*_font 换成中文字体路径；init 999\n"
        "#       再统一应用（gui 变量、默认样式、全部命名样式、字体替换映射），\n"
        "#       并提供设置界面中的「系统字体」选项。\n"
        f"{warn_var}"
        "init -1 python:\n"
        f"    _zh_cn_font = \"{esc}\"\n"
        "\n"
        "    # 抢在 screens 样式构建前替换 gui 字体变量（定义于 init -2）\n"
        "    try:\n"
        "        _zh_gui = gui\n"
        "    except Exception:\n"
        "        _zh_gui = None\n"
        "    if _zh_gui is not None:\n"
        f"        for _k in ({gui_keys_lit}):\n"
        "            if hasattr(_zh_gui, _k):\n"
        "                setattr(_zh_gui, _k, _zh_cn_font)\n"
        "\n"
        "init 999 python:\n"
        "    # 字体选项表：system 使用当前系统字体补丁。\n"
        "    _zz_font_choices = {'system': _zh_cn_font}\n"
        f"    _zz_rep_font_list = [{rep_list_lit}]\n"
        "\n"
        "    def _zz_all_style_items():\n"
        "        # Ren'Py 8.2 的 renpy.style.styles 是类映射对象（非 dict），\n"
        "        # lint.py 即用其 .items() 遍历；低版本回退 game.style.styles。\n"
        "        _zh_items = None\n"
        "        try:\n"
        "            import renpy.style as _zh_style_mod\n"
        "            if hasattr(_zh_style_mod, 'styles') and hasattr(\n"
        "                    _zh_style_mod.styles, 'items'):\n"
        "                _zh_items = _zh_style_mod.styles.items()\n"
        "        except Exception:\n"
        "            pass\n"
        "        if _zh_items is None:\n"
        "            try:\n"
        "                if hasattr(renpy.game.style.styles, 'items'):\n"
        "                    _zh_items = renpy.game.style.styles.items()\n"
        "            except Exception:\n"
        "                pass\n"
        "        return _zh_items or []\n"
        "\n"
        "    def _zz_set_font(f):\n"
        "        # 一站式应用字体：gui 变量 + 默认样式 + 全部命名样式 + 替换映射\n"
        "        try:\n"
        f"            for _k in ({gui_keys_lit}):\n"
        "                if hasattr(gui, _k):\n"
        "                    setattr(gui, _k, f)\n"
        "        except Exception:\n"
        "            pass\n"
        "        try:\n"
        "            style.default.font = f\n"
        "        except Exception:\n"
        "            pass\n"
        "        for _zh_n, _zh_s in _zz_all_style_items():\n"
        "            try:\n"
        "                _zh_s.font = f\n"
        "            except Exception:\n"
        "                pass\n"
        "        # 字体替换映射：把屏幕里显式 `font=\"xxx.ttf\"` 的元素也换掉\n"
        "        try:\n"
        "            if not hasattr(config, 'font_replacement_map'):\n"
        "                config.font_replacement_map = {}\n"
        "            for _f in _zz_rep_font_list:\n"
        "                for _b in (False, True):\n"
        "                    for _i in (False, True):\n"
        "                        config.font_replacement_map[(_f, _b, _i)] = (f, _b, _i)\n"
        "        except Exception:\n"
        "            pass\n"
        "\n"
        "    def _zz_font_select(choice):\n"
        "        # 设置界面「字体」选项调用：记住选择并即时应用到全部文本\n"
        "        try:\n"
        "            persistent._zz_font_choice = choice\n"
        "        except Exception:\n"
        "            pass\n"
        "        _zz_set_font(_zz_font_choices.get(choice, _zh_cn_font))\n"
        "        try:\n"
        "            renpy.style.rebuild()\n"
        "        except Exception:\n"
        "            pass\n"
        "        try:\n"
        "            renpy.restart_interaction()\n"
        "        except Exception:\n"
        "            pass\n"
        "\n"
        "    # 启动时应用用户上次的选择。旧版的 yahei/cjk 值统一迁移到 system。\n"
        "    _zz_saved_font_choice = getattr(\n"
        "        persistent, '_zz_font_choice', 'system')\n"
        "    if _zz_saved_font_choice in ('yahei', 'cjk'):\n"
        "        _zz_saved_font_choice = 'system'\n"
        "        try:\n"
        "            persistent._zz_font_choice = 'system'\n"
        "        except Exception:\n"
        "            pass\n"
        "    _zz_set_font(_zz_font_choices.get(\n"
        "        _zz_saved_font_choice, _zh_cn_font))\n"
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
    # Re-evaluate the current user's font even when an older patch already
    # exists. A game may have been copied from a different PC or profile.
    copied = copy_system_cjk_font(game_dir)

    # 已有补丁、字体文件仍在、且已是当前「系统字体」版 → 跳过。
    # 旧版补丁（包括带 yahei/cjk 选项的版本）在此升级重写。
    if patch.is_file():
        text = patch.read_text(encoding="utf-8-sig", errors="ignore")
        m = re.search(r"^# 中文字体: (.+)$", text, re.M)
        if (m and (game_dir / m.group(1).strip()).is_file()
                and (copied is None or copied == game_dir / m.group(1).strip())
                and "_zz_font_select" in text
                and "_zz_font_choices = {'system': _zh_cn_font}" in text
                and '微软雅黑' not in text
                and '汉化默认字体' not in text):
            res.ok = True
            res.skip = True
            res.message = "中文字体补丁已存在，跳过"
            return res

    # 1. 优先复制当前系统界面字体，确保设置中的「系统字体」名实相符。
    if copied is not None:
        cjk_font = copied
        rel = copied.relative_to(game_dir).as_posix()
        res.files.append(copied)
        res.message = f"已复制当前系统中文字体到 {rel}"
    else:
        # 2. 找不到可用系统字体时，退回游戏自带的中文字体。
        cjk_font = find_game_cjk_font(game_dir)
        if cjk_font is None:
            res.ok = False
            res.message = "未找到中文字体，且系统无可用中文字体（中文可能显示为方框）"
            return res
        rel = cjk_font.relative_to(game_dir).as_posix()
        res.message = f"系统字体不可用，改用游戏自带中文字体: {rel}"

    original_font = find_original_default_font(game_dir)
    # 枚举所有非 CJK 字体，用于 font_replacement_map（覆盖屏幕里
    # 显式写 font=xxx 的元素）。中文是否仍保留游戏原美术字体无所谓——
    # 关键是中文能渲染。
    rep_fonts = _list_non_cjk_fonts(game_dir, cjk_font)
    try:
        patch.write_text(
            _build_font_patch(rel, original_font, rep_fonts), encoding="utf-8-sig")
    except OSError as e:
        res.ok = False
        res.message = f"写字体补丁失败: {e}"
        return res
    res.files.append(patch)
    res.ok = True
    extra = f"；font_replacement_map: {len(rep_fonts)} 个" if rep_fonts else ""
    res.detail = (
        f"中文字体: {rel}（覆盖 gui.*_font + 全部命名样式{extra}）")
    res.message = f"已配置中文字体（{res.message}）"
    return res


# ---------------------------------------------------------------------------
# 语言切换界面
# ---------------------------------------------------------------------------

_LANG_UI_MARKER = "# ===== 语言切换（汉化工具自动生成） ====="

_LANG_UI_SNIPPET = (
    _LANG_UI_MARKER + "\n"
    'timer 0.25 repeat True action Function(_ls_observe_language_switch)\n'
    "hbox:\n"
    '    style_prefix "radio"\n'
    "    box_wrap True\n"
    '    label _("Language")\n'
    "    $ _lang_ui_names = _lang_ui_languages()\n"
    "    for _lang_ui_i in _lang_ui_names:\n"
    "        textbutton _lang_ui_display_name(_lang_ui_i) selected "
    "(_preferences.language == _lang_ui_i) action "
    "[Function(_lang_ui_note_selection, _lang_ui_i, __SCREEN_NAME__), "
    "Language(_lang_ui_i)] style \"radio_button\"\n"
)


def _find_direct_child_vbox(lines: list[str], start: int,
                            parent_indent: int) -> tuple[int | None, int]:
    """Find a vbox directly inside a screen/use block, skipping comments."""
    child_indent = parent_indent + 4
    for i in range(start, len(lines)):
        body = lines[i].rstrip("\r\n")
        stripped = body.strip()
        if not stripped:
            continue
        indent = len(body) - len(body.lstrip(" \t"))
        if indent <= parent_indent:
            break
        if stripped.startswith("#"):
            continue
        if indent != child_indent:
            continue
        if re.match(r"^[ \t]*vbox\s*:\s*(?:#.*)?$", body):
            return i, indent
    return None, child_indent


def _first_direct_child(lines: list[str], start: int,
                        parent_indent: int) -> int | None:
    """Return the first nonblank direct child line, including comments."""
    child_indent = parent_indent + 4
    for i in range(start, len(lines)):
        body = lines[i].rstrip("\r\n")
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" \t"))
        if indent <= parent_indent:
            break
        if indent == child_indent:
            return i
    return None


def _reserve_fixed_menu_space(lines: list[str], start: int, indent: int) -> None:
    """Use part of the stock spacer for the new row on a positioned viewport."""
    spacer = re.compile(r"^null\s+height\s+\(\s*4\s*\*\s*gui\.pref_spacing\s*\)(?=\s*(?:#|$))")
    for i in range(start, len(lines)):
        body = lines[i].lstrip(" \t")
        if not body.strip() or body.startswith("#"):
            continue
        depth = len(lines[i]) - len(body)
        if depth < indent:
            break
        if depth == indent and spacer.match(body):
            lines[i] = lines[i][:depth] + spacer.sub("null height gui.pref_spacing", body, count=1)
            break


def _insert_language_ui(block: str, screen_name: str = "preferences") -> str:
    """Place the language row at the top of the preferences content.

    Appending it after the stock sliders can put it below a game's fixed-height
    menu viewport, where it compiles successfully but is never visible.
    """
    snippet = _LANG_UI_SNIPPET.replace("__SCREEN_NAME__", repr(screen_name))
    lines = block.splitlines(keepends=True)
    use_index = None
    root_vbox_index = None
    indent = 4
    line_end = "\n"
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = re.match(r"^(?P<indent>[ \t]*)use\s+game_menu\b", body)
        if not match or not body.rstrip().endswith(":"):
            continue
        use_index = i
        use_indent = len(match.group("indent"))
        indent = use_indent + 4
        line_end = "\r\n" if line.endswith("\r\n") else "\n"
        root_vbox_index, root_indent = _find_direct_child_vbox(
            lines, i + 1, use_indent)
        if root_vbox_index is not None:
            indent = root_indent + 4
        break

    # Custom settings screens often use ``viewport: ...; has vbox`` rather
    # than ``use game_menu``. Insert as the viewport's first child so the
    # language selector shares the same scrollable layout as the controls.
    if use_index is None:
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            viewport = re.match(
                r"^(?P<indent>[ \t]*)viewport\b.*:\s*(?:#.*)?$", body)
            if viewport is None:
                continue
            viewport_indent = len(viewport.group("indent"))
            for j in range(i + 1, len(lines)):
                child = lines[j].rstrip("\r\n")
                stripped = child.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                current_indent = len(child) - len(child.lstrip(" \t"))
                if current_indent <= viewport_indent:
                    break
                has_vbox = re.match(r"^has\s+vbox\b", stripped)
                child_vbox = re.match(r"^vbox\s*:\s*(?:#.*)?$", stripped)
                if current_indent == viewport_indent + 4 and (has_vbox or child_vbox):
                    indent = current_indent if has_vbox else current_indent + 4
                    line_end = "\r\n" if lines[j].endswith("\r\n") else "\n"
                    # A fixed-position viewport may share space with an
                    # absolute-positioned Back button. Reuse the original
                    # large blank separator without moving other widgets.
                    header = _strip_comments("".join(lines[i:j + 1]))
                    if (re.search(r"\bypos\s+[1-9]\d*\b", header) and
                            not re.search(r"\b(?:scrollbars|mousewheel|draggable)\b", header)):
                        _reserve_fixed_menu_space(lines, j + 1, indent)
                    injected = "".join(
                        (" " * indent + item if item else "") + line_end
                        for item in snippet.splitlines())
                    lines.insert(j + 1, injected)
                    return "".join(lines)

    if use_index is None:
        for line in lines[:1]:
            match = re.match(r"^(?P<indent>[ \t]*)(?:init\s+-?\d+\s+)?screen\b", line)
            if match:
                indent = len(match.group("indent")) + 4
                break

    injected = "".join(
        (" " * indent + line if line else "") + line_end
        for line in snippet.splitlines()
    )
    if root_vbox_index is None:
        root_vbox_index = use_index
    if use_index is None:
        return block.rstrip("\r\n") + line_end + injected
    lines.insert(root_vbox_index + 1, injected)
    return "".join(lines)


def _append_to_preferences_vbox(block: str, snippet: str) -> str:
    """Append a generated section inside the root vbox transcluded by game_menu."""
    lines = block.splitlines(keepends=True)
    use_index = None
    root_vbox_index = None
    child_indent = 4
    line_end = "\n"
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = re.match(r"^(?P<indent>[ \t]*)use\s+game_menu\b", body)
        if not match or not body.rstrip().endswith(":"):
            continue
        use_index = i
        use_indent = len(match.group("indent"))
        child_indent = use_indent + 4
        line_end = "\r\n" if line.endswith("\r\n") else "\n"
        root_vbox_index, root_indent = _find_direct_child_vbox(
            lines, i + 1, use_indent)
        if root_vbox_index is not None:
            child_indent = root_indent + 4
        break
    if root_vbox_index is None and use_index is None and lines:
        match = re.match(
            r"^(?P<indent>[ \t]*)(?:init\s+-?\d+\s+)?screen\b", lines[0])
        if match:
            screen_indent = len(match.group("indent"))
            child_indent = screen_indent + 4
            root_vbox_index, root_indent = _find_direct_child_vbox(
                lines, 1, screen_indent)
            if root_vbox_index is not None:
                child_indent = root_indent + 4
    injected = "".join(
        (" " * child_indent + line if line else "") + line_end
        for line in snippet.splitlines()
    )
    lines.append(injected)
    return "".join(lines)


def _append_to_settings_viewport(block: str, snippet: str) -> str:
    """Append a generated options section inside a custom viewport vbox."""
    lines = block.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        viewport = re.match(
            r"^(?P<indent>[ \t]*)viewport\b.*:\s*(?:#.*)?$", body)
        if viewport is None:
            continue
        viewport_indent = len(viewport.group("indent"))
        for j in range(i + 1, len(lines)):
            child = lines[j].rstrip("\r\n")
            stripped = child.strip()
            if not stripped or stripped.startswith("#"):
                continue
            current_indent = len(child) - len(child.lstrip(" \t"))
            if current_indent <= viewport_indent:
                break
            has_vbox = (current_indent == viewport_indent + 4 and
                        re.match(r"^has\s+vbox\b", stripped))
            child_vbox = (current_indent == viewport_indent + 4 and
                          re.match(r"^vbox\s*:\s*(?:#.*)?$", stripped))
            if has_vbox or child_vbox:
                content_indent = current_indent if has_vbox else current_indent + 4
                end = j + 1
                while end < len(lines):
                    item = lines[end].rstrip("\r\n")
                    if item.strip():
                        item_indent = len(item) - len(item.lstrip(" \t"))
                        if item_indent < content_indent:
                            break
                    end += 1
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                generated = "".join(
                    (" " * content_indent + item if item else "") + newline
                    for item in snippet.splitlines())
                lines.insert(end, generated)
                return "".join(lines)
    return _append_to_preferences_vbox(block, snippet)


def _insert_custom_font_ui(block: str, language: str) -> str:
    """Keep the system-font button in the language row on fixed-height menus."""
    if _FONT_UI_MARKER in block:
        return block
    lines = block.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("textbutton _lang_ui_display_name(_lang_ui_i) "):
            # Place this control beside the loop, not inside it (one per language).
            indent = line[:len(line) - len(line.lstrip()) - 4]
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            entries = _font_list_entries(indent, ' style "radio_button"', language)
            lines.insert(i + 1, newline.join(entries) + newline)
            return "".join(lines)
    # Native language menu with no font list: add a single conditional button.
    append = (_append_to_preferences_vbox if "use game_menu" in block
              else _append_to_settings_viewport)
    return append(block, "\n".join(_font_list_entries("", ' style "radio_button"', language)) + "\n")

# 字体选项（设置界面）：单选「系统字体」。
# _zz_font_select / _zz_font_choices 定义在 zz_cn_font.rpy。为避免「字体补丁
# 缺失时屏幕引用未定义变量」（NameError），这里做两层保护：
#   1. apply_language_ui 只在 zz_cn_font.rpy 确实含这些定义时才注入本段
#      （见 _font_ui_available）；
#   2. 本段把两个按钮都套在 `if _zz_font_choices:` 内，且 _LANG_UI_HELPER 会
#      在缺少定义时兜底 `_zz_font_choices = {}` —— 此时只渲染标题，不产生
#      任何对 _zz_font_select 的引用。
_FONT_UI_MARKER = "# ===== 字体选项（汉化工具自动生成） ====="

# （屏幕片段由 _apply_font_ui 生成：有原生字体列表时插入该列表，否则追加自有段。）

# --- 原生字体选择列表 -------------------------------------------------------
#
# 不少游戏自带字体选择（如本作 ``Font: Default / OpenDyslexic``）。此时再
# 另起一个「字体」段会让设置界面出现两个 Font（截图反馈的问题），正确做法是
# 把汉化字体**追加到游戏原生列表里**。原生列表的典型形态：
#
#     label _("Font")
#     textbutton _("{font=font/Itim-Regular.ttf}Default") action Language(None)
#     textbutton _("{font=font/OpenDyslexicReg.otf}OpenDyslexic") action Language("opendyslexic")
#
# 检测不到原生列表时，才注入自有的「字体」段，且**仅在当前语言为汉化目标
# 语言时显示**（否则切到中文前多出一段无意义的字体选项）。
_FONT_LABEL_RE = re.compile(
    r'^(\s*)label\s+_?\(\s*["\'](?:Font|字体)["\']\s*\)\s*$')
_TEXTBUTTON_RE = re.compile(r"^(\s*)textbutton\b")
_RADIO_STYLE_RE = re.compile(r'\bstyle\s+["\']radio_button["\']')


def _find_native_font_list(block: str) -> tuple[int, str, str] | None:
    """在 preferences 屏幕块里定位游戏原生的「字体」选择列表。

    返回 ``(插入行号(0 基，在其后插入), 缩进串, 条目样式后缀)``；
    未找到返回 None。样式后缀是 ``style "radio_button"``（原生条目带则跟着带，
    保证视觉一致），否则空串。

    关键：屏幕语言里 ``label`` 与 ``textbutton`` 是**同级**（同一 vbox 的子
    元素，缩进相同），所以「Font 组」= 该 label 及其后续同级/更深缩进的行，
    直到缩进变浅（父块结束）或遇到下一个同级 ``label``。
    """
    lines = block.splitlines()
    for i, ln in enumerate(lines):
        m = _FONT_LABEL_RE.match(ln)
        if not m:
            continue
        label_indent = len(m.group(1))
        last_btn: int | None = None
        end = i
        j = i + 1
        while j < len(lines):
            s = lines[j]
            if not s.strip():
                j += 1
                continue
            cur = len(s) - len(s.lstrip())
            if cur < label_indent:
                break                      # 回到父块 → 本组结束
            if cur == label_indent and _FONT_LABEL_RE.match(s):
                break                      # 下一个同级标题 → 本组结束
            if _TEXTBUTTON_RE.match(s):
                last_btn = j
            end = j
            j += 1
        if last_btn is None:
            continue
        # textbutton 语句可能带续行（缩进更深，如 `style "radio_button"`）
        stmt_end = last_btn
        k = last_btn + 1
        while k <= end:
            s = lines[k]
            if s.strip() and (len(s) - len(s.lstrip())) > label_indent:
                stmt_end = k
                k += 1
                continue
            break
        head = lines[last_btn]
        indent = head[: len(head) - len(head.lstrip())]
        style_suffix = ' style "radio_button"' if _RADIO_STYLE_RE.search(
            "\n".join(lines[last_btn:stmt_end + 1])) else ""
        return stmt_end, indent, style_suffix
    return None


def _font_list_entries(indent: str, style_suffix: str, language: str) -> list[str]:
    """追加到原生字体列表的条目（与原生条目同缩进 / 同样式）。"""
    return [
        f"{indent}{_FONT_UI_MARKER}",
        f"{indent}if _preferences.language == {language!r}:",
        f"{indent}    if _zz_font_choices:",
        f"{indent}        textbutton \"系统字体\" selected "
        f"(persistent._zz_font_choice == 'system') action "
        f"Function(_zz_font_select, 'system'){style_suffix}",
    ]


def _apply_font_ui(block: str, language: str) -> str:
    """把字体选项加进屏幕文本，返回**整段**屏幕定义（已含插入内容）。

    两种模式：
    - 定位到游戏原生字体列表 → 把条目插入该列表末尾（不新增 Font 标题，
      避免设置界面出现两个 Font 段）；
    - 没有原生列表时，在语言按钮的同一行显示系统字体，给原有游戏控件留足高度。
    """
    found = _find_native_font_list(block)
    if found is not None:
        end, indent, style_suffix = found
        lines = block.rstrip("\n").splitlines()
        lines[end + 1:end + 1] = _font_list_entries(indent, style_suffix, language)
        return "\n".join(lines) + "\n"
    return _insert_custom_font_ui(block, language)


def _font_ui_available(game_dir: Path) -> bool:
    """字体补丁是否已就位（可用于设置界面的字体选项）。

    只有 zz_cn_font.rpy 存在且含 `_zz_font_choices` / `_zz_font_select` 定义时
    才认为可用——否则注入字体 UI 会让屏幕引用未定义的变量。
    """
    p = game_dir / "zz_cn_font.rpy"
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return False
    return "_zz_font_choices" in text and "_zz_font_select" in text
# 语言辅助函数：
# - 语言列表：Ren'Py 8.2+ 提供 renpy.translation.known_languages()；
#   get_languages() 为更新版本 API，老版本需回退扫描 game/tl 目录。
# - 切换：Ren'Py 8.2 的 Preference() 尚无 language 分支，直接调
#   renpy.change_language()（None 表示恢复默认英文）。
_LANG_UI_HELPER = (
    "\n\n"
    "# 兜底默认值：字体补丁（zz_cn_font.rpy，init 999）未生效时，设置界面里的\n"
    "# 「字体」段会读到空表并只渲染标题（不引用 _zz_font_select），避免\n"
    "# NameError。补丁存在时其 init 999 会用真实字体表覆盖本默认值。\n"
    "init -1 python:\n"
    "    try:\n"
    "        _zz_font_choices\n"
    "    except NameError:\n"
    "        _zz_font_choices = {}\n"
    "    def _ls_observe_language_switch():\n"
    "        pass\n"
    "\n"
    "init python:\n"
    "    _rpytranslator_language_request = (0, None, None, None)\n"
    "    def _lang_ui_note_selection(language, screen_name):\n"
    "        global _rpytranslator_language_request\n"
    "        _rpytranslator_language_request = (\n"
    "            _rpytranslator_language_request[0] + 1, screen_name,\n"
    "            _preferences.language, language)\n"
    "\n"
    "    def _lang_ui_display_name(language):\n"
    "        # 语言码是运行时动态字符串，不能交给 _() 翻译：默认英文时，\n"
    "        # translate schinese strings 尚未生效，会把 schinese 原样显示出来。\n"
    "        # 这里直接映射显示名，同时保留未知语言码，确保按钮始终可见。\n"
    "        if language is None:\n"
    "            return \"English\"\n"
    "        _code = str(language).lower()\n"
    "        _names = {\n"
    "            'schinese': \"简体中文\",\n"
    "            'zh': \"简体中文\",\n"
    "            'zh_cn': \"简体中文\",\n"
    "            'zh_hans': \"简体中文\",\n"
    "            'tchinese': \"繁体中文\",\n"
    "            'zh_tw': \"繁体中文\",\n"
    "            'zh_hant': \"繁体中文\",\n"
    "            'japanese': \"日本語\",\n"
    "            'korean': \"한국어\",\n"
    "            'english': \"English\",\n"
    "        }\n"
    "        return _names.get(_code, str(language))\n"
    "\n"
    "    def _lang_ui_languages():\n"
    "        # 可用语言列表（含默认英文 None）。跨 Ren'Py 版本兼容。\n"
    "        langs = []\n"
    "        # Ren'Py 8.2 在翻译初始化早期可能让 known_languages() 返回空表；\n"
    "        # 空表不能视为成功，否则 game/tl/schinese 会被漏掉。\n"
    "        _providers = (getattr(renpy, 'translation', None), renpy)\n"
    "        for _provider in _providers:\n"
    "            if _provider is None:\n"
    "                continue\n"
    "            for _fn_name in ('known_languages', 'get_languages'):\n"
    "                _fn = getattr(_provider, _fn_name, None)\n"
    "                if _fn is None:\n"
    "                    continue\n"
    "                try:\n"
    "                    _langs = [_i for _i in (_fn() or [])\n"
    "                              if _i is not None and str(_i).lower() != 'none']\n"
    "                except Exception:\n"
    "                    continue\n"
    "                if _langs:\n"
    "                    langs = list(_langs)\n"
    "                    break\n"
    "            if langs:\n"
    "                break\n"
    "        # API results may be incomplete during startup; union them with\n"
    "        # installed tl/<language> folders so every generated language\n"
    "        # remains selectable even when Ren'Py returns a partial list.\n"
    "        import os\n"
    "        _tl_dir = os.path.join(config.gamedir, 'tl')\n"
    "        if os.path.isdir(_tl_dir):\n"
    "            for _name in sorted(os.listdir(_tl_dir), key=str.lower):\n"
    "                if (_name.lower() != 'none' and\n"
    "                        os.path.isdir(os.path.join(_tl_dir, _name))):\n"
    "                    langs.append(_name)\n"
    "        _seen = set()\n"
    "        _unique = []\n"
    "        for _name in langs:\n"
    "            _key = str(_name).lower()\n"
    "            if _key not in _seen:\n"
    "                _seen.add(_key)\n"
    "                _unique.append(_name)\n"
    "        return [None] + sorted(_unique, key=lambda _x: str(_x).lower())\n"
    "\n"
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
        "Font": "字体",
    },
    "tchinese": {
        "tchinese": "繁體中文",
        "Traditional Chinese": "繁體中文",
        "Chinese (Traditional)": "繁體中文",
        "Chinese": "中文",
        "English": "英語",
        "Font": "字型",
    },
    "zh_cn": {
        "zh_cn": "简体中文",
        "Simplified Chinese": "简体中文",
        "Chinese (Simplified)": "简体中文",
        "Chinese": "中文",
        "English": "英语",
        "Font": "字体",
    },
    "zh_hans": {
        "zh_hans": "简体中文",
        "Simplified Chinese": "简体中文",
        "Chinese (Simplified)": "简体中文",
        "Chinese": "中文",
        "English": "英语",
        "Font": "字体",
    },
    "zh": {
        "zh": "中文",
        "Chinese": "中文",
        "Simplified Chinese": "简体中文",
        "English": "英语",
        "Font": "字体",
    },
}

def _strip_comments(text: str) -> str:
    """去掉整行注释与行尾注释（保留字符串内的 ``#``，按引号状态判断）。

    用途：判断「源码是否使用了某 API」时必须忽略注释——本工具自己生成的
    补丁里就有 ``# 老版 Ren'Py 无 Preference('language')，…`` 这类说明注释。
    """
    out: list[str] = []
    for line in text.splitlines():
        quote: str | None = None
        cut = len(line)
        i = 0
        while i < len(line):
            c = line[i]
            if quote:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in ("'", '"'):
                quote = c
            elif c == "#":
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


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


def _raise_screen_priority(block: str, screen_name: str) -> str:
    """Make an injected preferences screen win over archived definitions.

    Ren'Py's stock screens are commonly declared at ``init -501``. Keeping
    that priority in a loose patch is order-dependent when the original screen
    remains inside an RPA archive, so normalize the first screen header to a
    later priority while preserving its signature and line ending.
    """
    lines = block.splitlines(keepends=True)
    if not lines:
        return block
    first = lines[0]
    if first.endswith("\r\n"):
        first_body, line_end = first[:-2], "\r\n"
    elif first.endswith("\n"):
        first_body, line_end = first[:-1], "\n"
    else:
        first_body, line_end = first, ""
    match = re.match(
        r"^(?P<indent>[ \t]*)(?:(?:init[ \t]+[+-]?\d+[ \t]+)?"
        r"screen[ \t]+" + re.escape(screen_name) + r"\b)(?P<tail>.*)$",
        first_body,
    )
    if match is None:
        return block
    lines[0] = (
        f"{match.group('indent')}init {_LANGUAGE_UI_PRIORITY} "
        f"screen {screen_name}{match.group('tail')}{line_end}"
    )
    return "".join(lines)


def _raise_preferences_priority(block: str) -> str:
    """Compatibility wrapper for the standard preferences screen."""
    return _raise_screen_priority(block, "preferences")


def _screen_has_language_ui(block: str, language: str = "schinese") -> bool:
    """Only skip a settings screen with an action that can select this language."""
    stripped = _strip_comments(block)
    action = r"""\b(?:Language\s*\(\s*|Preference\s*\(\s*["']language["']\s*,\s*)"""
    if re.search(action + r"""(["'])""" + re.escape(language) + r"\1\s*\)", stripped):
        return True
    return bool(re.search(r"\b(?:get_languages|known_languages)\s*\(", stripped)
                and re.search(action + r"[A-Za-z_]\w*\s*\)", stripped))


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
    """Return the stock settings source, with a disk path for loose scripts."""
    for label, text in _settings_sources(game_dir):
        if _PREF_SCREEN_RE.search(text):
            return text, None if "::" in label else game_dir / label
    return None, None


_MENU_SCREEN_CALL_RE = re.compile(
    r"\bShowMenu\s*\(\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1")
_SETTING_SCREEN_NAME_RE = re.compile(r"(?:pref|option|setting)", re.I)
_SETTING_SCRIPT_HINTS = (
    "screen", "menu", "pref", "option", "setting", "section", "ingame", "mm_",
)


def _settings_script(path: Path) -> bool:
    return any(hint in path.stem.lower() for hint in _SETTING_SCRIPT_HINTS)


def _settings_sources(game_dir: Path) -> list[tuple[str, str]]:
    """Read UI scripts once for both stock and custom settings destinations."""
    sources: list[tuple[str, str]] = []
    other_compiled: list[Path] = []

    def add_source(label: str, text: str | None) -> None:
        if text and ("screen " in text or "ShowMenu" in text):
            sources.append((label, text))

    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.lower().startswith("zz_"):
                continue
            path = Path(dirpath) / fn
            ext = path.suffix.lower()
            if ext in (".rpy", ".rpym"):
                try:
                    add_source(str(path.relative_to(game_dir)), path.read_text(
                        encoding="utf-8-sig", errors="ignore"))
                except OSError:
                    continue
            elif ext in (".rpyc", ".rpymc") and not path.with_suffix(ext[:-1]).is_file():
                if _settings_script(path):
                    add_source(str(path.relative_to(game_dir)), _decompile_rpyc_source(path, fn))
                else:
                    other_compiled.append(path)
    sources.sort(key=lambda source: Path(source[0]).stem.casefold() != "screens")
    for label, text in _archive_sources(
            game_dir, lambda path: _settings_script(path) or path.suffix.lower() in (".rpy", ".rpym")):
        add_source(label, text)
    # Some projects use opaque filenames. Only pay for decompiling their story
    # scripts when the preferred sources lack a stock settings definition.
    if not any(_PREF_SCREEN_RE.search(text) for _, text in sources):
        for path in other_compiled:
            add_source(str(path.relative_to(game_dir)), _decompile_rpyc_source(path, path.name))
    return sources


def _settings_screens(game_dir: Path) -> list[tuple[str, str, str]]:
    """Return standard settings and custom settings reached by ShowMenu."""
    sources = _settings_sources(game_dir)
    requested = {"preferences", "mm_preferences", "quick_preferences"}
    for _label, text in sources:
        stripped = _strip_comments(text)
        requested.update(
            name for _quote, name in _MENU_SCREEN_CALL_RE.findall(stripped)
            if _SETTING_SCREEN_NAME_RE.search(name))

    found: dict[str, tuple[str, str]] = {}
    for label, text in sources:
        for name in requested:
            if name in found:
                continue
            block = _extract_screen_block(text, name)
            if block is not None:
                found[name] = (block, label)
    return [(name, block, label) for name, (block, label)
            in sorted(found.items())]


def _count_screen_defs(text: str, name: str) -> int:
    """统计文本里 ``screen <name>`` 定义的个数（含 ``init -N`` 前缀写法）。"""
    pat = re.compile(r"^\s*(?:init\s+-?\d+\s+)?screen\s+" + re.escape(name) + r"\b")
    return sum(1 for ln in text.splitlines() if pat.match(ln))


def _language_ui_is_top_aligned(text: str,
                               screen_name: str = "preferences") -> bool:
    """Require the generated selector at the start of its settings content."""
    block = _extract_screen_block(text, screen_name)
    if block is None:
        return False
    lines = block.splitlines()
    use_index = next((i for i, line in enumerate(lines)
                      if re.match(r"^[ \t]*use\s+game_menu\b.*:\s*(?:#.*)?$",
                                  line)), None)
    marker_index = next((i for i, line in enumerate(lines)
                         if line.strip() == _LANG_UI_MARKER), None)
    if marker_index is None:
        return False
    if use_index is None:
        viewport_index = next((i for i, line in enumerate(lines)
                               if re.match(r"^[ \t]*viewport\b.*:\s*(?:#.*)?$",
                                           line)), None)
        if viewport_index is None:
            return False
        viewport_indent = len(lines[viewport_index]) - len(
            lines[viewport_index].lstrip(" \t"))
        # A viewport may declare properties such as ``xysize`` or
        # ``scrollbars`` before its layout root. Mirror the injector and skip
        # those direct-child properties until ``has vbox``/``vbox`` appears.
        root_index = None
        for i in range(viewport_index + 1, len(lines)):
            body = lines[i].rstrip("\r\n")
            stripped = body.strip()
            if not stripped or stripped.startswith("#"):
                continue
            current_indent = len(body) - len(body.lstrip(" \t"))
            if current_indent <= viewport_indent:
                break
            if (current_indent == viewport_indent + 4 and
                    (re.match(r"^has\s+vbox\b", stripped) or
                     re.match(r"^vbox\s*:", stripped))):
                root_index = i
                break
        if root_index is None:
            return False
        root_line = lines[root_index].strip()
        if re.match(r"^has\s+vbox\b", root_line):
            expected_indent = viewport_indent + 4
            expected_index = root_index + 1
        elif re.match(r"^vbox\s*:", root_line):
            expected_indent = viewport_indent + 8
            expected_index = root_index + 1
        else:
            return False
        while expected_index < len(lines) and (
                not lines[expected_index].strip() or
                (lines[expected_index].lstrip().startswith("#") and
                 lines[expected_index].strip() != _LANG_UI_MARKER)):
            expected_index += 1
        return (expected_index == marker_index and
                len(lines[marker_index]) -
                len(lines[marker_index].lstrip(" \t")) == expected_indent)
    use_match = re.match(r"^([ \t]*)use\s+game_menu\b", lines[use_index])
    if use_match is None:
        return False
    use_indent = len(use_match.group(1))
    root_vbox_index, root_indent = _find_direct_child_vbox(
        lines, use_index + 1, use_indent)
    if root_vbox_index is not None:
        first_child = _first_direct_child(
            lines, root_vbox_index + 1, root_indent)
        return (first_child == marker_index
                and len(lines[marker_index]) -
                len(lines[marker_index].lstrip(" \t")) == root_indent + 4)
    first_child = _first_direct_child(lines, use_index + 1, use_indent)
    return (first_child == marker_index
            and len(lines[marker_index]) -
            len(lines[marker_index].lstrip(" \t")) == use_indent + 4)


def _font_ui_is_current(text: str,
                        screen_name: str | None = None) -> bool:
    """Accept the compact conditional button, including decompiler blank lines."""
    if screen_name is not None:
        text = _extract_screen_block(text, screen_name) or ""
    lines = text.splitlines()
    marker_index = next((i for i, line in enumerate(lines)
                         if line.strip() == _FONT_UI_MARKER), None)
    if marker_index is None:
        return False
    meaningful = [line for line in lines[marker_index + 1:marker_index + 14]
                  if line.strip() and not line.lstrip().startswith("#")]
    if len(meaningful) < 3:
        return False
    condition, choices, button = meaningful[:3]
    indent = lines[marker_index].split("#", 1)[0]
    return (condition.startswith(indent + "if _preferences.language ==")
            and choices == indent + "    if _zz_font_choices:"
            and button.startswith(indent + '        textbutton "系统字体" '))


def _language_ui_is_current(text: str,
                            screen_name: str = "preferences") -> bool:
    """Require the official action and timer in the generated screen body."""
    block = _extract_screen_block(text, screen_name) or ""
    code = _strip_comments(block)
    return (
        _LANG_UI_MARKER in block and
        "Language(_lang_ui_i)" in code and
        f"Function(_lang_ui_note_selection, _lang_ui_i, {screen_name!r})" in code and
        "timer 0.25 repeat True action Function(_ls_observe_language_switch)" in code
    )


def monitored_settings_screens(source: str) -> list[str]:
    """Names of overrides that actually include both the selection action and timer."""
    names = re.findall(
        rf"(?m)^ *init +{_LANGUAGE_UI_PRIORITY} +screen +([A-Za-z_]\w*)\b", source)
    return [name for name in names if _language_ui_is_current(source, name)]


def _build_language_ui(source_text: str, with_font: bool = False,
                      language: str = "schinese",
                      screen_name: str = "preferences") -> str | None:
    """Inject language/font selectors into one settings screen definition.

    返回新的整个屏幕定义；屏幕块已注入过或定位不到时返回 None。
    字体选项优先追加进游戏的**原生字体列表**（避免出现两个 Font 段）。
    """
    block = _extract_screen_block(source_text, screen_name)
    if block is None:
        return None
    if _LANG_UI_MARKER in block:
        return None  # 已注入
    out = _insert_language_ui(_raise_screen_priority(block, screen_name), screen_name)
    if with_font:
        out = _apply_font_ui(out, language)
    return out + _LANG_UI_HELPER


def apply_language_ui(game_dir: Path, language: str = "schinese",
                      with_font: bool = False) -> PatchResult:
    """Reconcile each settings override against its current game source."""
    patch = game_dir / "zz_language_ui.rpy"
    header = "# -*- coding: utf-8 -*-\n# 设置界面选项（汉化工具自动生成）：语言切换 / 字体选择。\n\n"
    want_font = with_font and _font_ui_available(game_dir)
    screens = _settings_screens(game_dir)
    if not screens:
        return PatchResult(message="未找到可注入的设置界面屏幕定义")

    generated = []
    for name, block, location in screens:
        add_language = not _screen_has_language_ui(block, language)
        if not add_language and not want_font:
            continue
        body = _raise_screen_priority(block, name)
        if add_language:
            body = _insert_language_ui(body, name)
        if want_font:
            body = _apply_font_ui(body, language)
        generated.append(f"# Settings screen override: {name} ({location})\n"
                         + body.rstrip() + "\n")
    try:
        previous = patch.read_text(encoding="utf-8-sig") if patch.is_file() else ""
        if not generated:
            if previous.startswith(header):
                # An update added native controls. A loose compiled override
                # would still mask the updated screen if only its source went.
                patch.with_suffix(".rpyc").unlink(missing_ok=True)
                patch.unlink()
                return PatchResult(ok=True, message="游戏设置已支持目标语言，已移除旧设置补丁")
            return PatchResult(ok=True, skip=True, message="游戏已自带语言切换界面")
        content = header + "\n".join(generated) + _LANG_UI_HELPER
        if content == previous:
            return PatchResult(ok=True, skip=True, message="设置界面补丁与游戏源码一致")
        patch.write_text(content, encoding="utf-8-sig")
    except (OSError, UnicodeError) as e:
        return PatchResult(message=f"写设置界面补丁失败: {e}")
    return PatchResult(ok=True, files=[patch],
                       message=f"已更新 {len(generated)} 个设置界面的选项")


_LANG_DISPLAY_FILE = "zz_language_display.rpy"


def ensure_language_name(game_dir: Path, language: str) -> PatchResult | None:
    """生成 tl/<语言>/zz_language_display.rpy，让设置里语言显示为中文名。

    仅补充「翻译文件未覆盖」的语言名/界面字符串：若源文件中已出现
    "Language"（Ren'Py 模板的偏好设置里普遍存在），翻译流程会生成
    `old "Language"`，此处必须跳过，避免字符串翻译重复定义冲突。
    """
    tl_dir = game_dir / "tl" / language
    res = PatchResult()
    code = language.lower()
    mappings = _LANGUAGE_DISPLAY.get(code, {})
    if not mappings and is_chinese_language(code):
        variant = "tchinese" if code in {
            "zh_tw", "zh_hk", "zh_hant", "zh-hant", "cht", "traditional",
            "traditional_chinese", "traditionalchinese"} else "schinese"
        mappings = {code if name == variant else name: text
                    for name, text in _LANGUAGE_DISPLAY[variant].items()}
    try:
        docs = index_tl_directory(tl_dir, language)
        if not docs and not tl_dir.is_dir():
            return None
        header = "# 语言显示名（汉化工具自动生成）\n"
        f = tl_dir / _LANG_DISPLAY_FILE
        while True:
            f = editable_output_path(f, docs)
            old = docs.get(f)
            if not old or (old.lines and old.lines[0].rstrip("\r\n") == header.rstrip()):
                break
            f = f.with_name(f.stem + "_rpytranslator.rpy")
        existing = {entry.original for path, doc in docs.items() if path != f
                    for entry in doc.entries if entry.identifier is None}
        missing = [(en, cn) for en, cn in mappings.items() if en not in existing]
        if "Language" not in existing and mappings:
            missing.append(("Language", "語言" if mappings.get("Font") == "字型" else "语言"))
        if not missing and not old:
            return PatchResult(ok=True, skip=True, message="已有译文已包含语言显示名")
        body = header
        if missing:
            body += f"translate {language} strings:\n"
            body += "".join(f"    old {en!r}\n    new {cn!r}\n" for en, cn in missing)
        if old:
            body = body.replace("\n", old.newline)
            if "".join(old.lines) == body:
                return PatchResult(ok=True, skip=True, message="语言显示名已配置")
        write_if_unchanged(f, body, old)
    except (OSError, TranslationMergeError) as e:
        res.ok = False
        res.message = f"写语言显示名失败: {e}"
        return res
    res.files.append(f)
    res.ok = True
    res.message = f"已设置语言显示名（tl/{language}/{f.name}）"
    return res


def apply_all(game_dir: Path, language: str = "schinese",
              with_font: bool = True,
              with_language_ui: bool = True) -> list[PatchResult]:
    """依次执行全部后处理，返回各项结果（供日志展示）。"""
    results: list[PatchResult] = []
    with_font = with_font and is_chinese_language(language)
    if with_font:
        results.append(apply_font_patch(game_dir))
    if with_language_ui:
        # 字体补丁启用时，设置界面同时提供「系统字体」选项。
        results.append(apply_language_ui(game_dir, language,
                                         with_font=with_font))
        name_res = ensure_language_name(game_dir, language)
        if name_res is not None:
            results.append(name_res)
    return results
