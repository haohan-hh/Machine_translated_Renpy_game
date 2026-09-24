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


def find_original_default_font(game_dir: Path) -> str:
    """从游戏源码（含 .rpa 归档）提取默认字体路径，找不到用 DejaVuSans。

    仅返回磁盘上实际存在的字体名，避免 FontGroup 引用归档内字体导致报错。
    """
    # 1. 磁盘松散的 .rpy/.rpym（含汉化产物）
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            hit = _scan_font_text(text)
            if hit and (game_dir / hit).is_file():
                return hit
    # 2. 脚本在 .rpa 归档中：读 gui/options 源码或反编译 .rpyc
    rpas = [p for p in game_dir.glob("*.rpa") if p.is_file()]
    rpas += [p for p in game_dir.glob("*.RPA") if p.is_file()]
    for rpa in rpas:
        from . import rpa_loader
        for cand in ("gui.rpy", "gui.rpyc", "options.rpyc", "screens.rpyc"):
            data = rpa_loader.read_script_data([rpa], cand)
            if data is None:
                continue
            try:
                if cand.endswith(".rpy"):
                    text = data.decode("utf-8-sig", errors="ignore")
                else:
                    text = _decompile_rpyc_source(data, cand)
            except Exception:
                continue
            if not text:
                continue
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
    - **设置界面字体选项**：init -1 探测系统微软雅黑（msyh.ttc，只加入
      config.searchpath，不复制文件）；init 999 定义 `_zz_set_font(f)`
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
        "#       并提供设置界面可切换的字体选项（汉化默认字体 / 微软雅黑）。\n"
        f"{warn_var}"
        "init -1 python:\n"
        f"    _zh_cn_font = \"{esc}\"\n"
        "\n"
        "    # 探测系统微软雅黑（不复制进游戏目录，加入搜索路径即可引用）\n"
        "    _zz_yahei = None\n"
        "    import os as _zz_os\n"
        "    _zz_fonts_dir = _zz_os.path.join(\n"
        "        _zz_os.environ.get('WINDIR',\n"
        "            _zz_os.environ.get('SystemRoot', 'C:\\\\Windows')),\n"
        "        'Fonts')\n"
        "    for _zz_c in ('msyh.ttc', 'msyh.ttf'):\n"
        "        try:\n"
        "            if _zz_os.path.isfile(\n"
        "                    _zz_os.path.join(_zz_fonts_dir, _zz_c)):\n"
        "                _zz_yahei = _zz_c\n"
        "                if _zz_fonts_dir not in config.searchpath:\n"
        "                    config.searchpath.append(_zz_fonts_dir)\n"
        "                break\n"
        "        except Exception:\n"
        "            pass\n"
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
        "    # 字体选项表：cjk=汉化默认字体；yahei=系统微软雅黑（探测到才有）\n"
        "    _zz_font_choices = {'cjk': _zh_cn_font}\n"
        "    if _zz_yahei is not None:\n"
        "        _zz_font_choices['yahei'] = _zz_yahei\n"
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
        "    # 启动时应用用户上次的选择（默认汉化字体）\n"
        "    _zz_set_font(_zz_font_choices.get(\n"
        "        persistent._zz_font_choice, _zh_cn_font))\n"
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

    # 已有补丁、字体文件仍在、且已是「带字体选项」的新版补丁 → 跳过。
    # 旧版补丁（无 _zz_font_select，设置界面不可切换字体）在此升级重写。
    if patch.is_file():
        text = patch.read_text(encoding="utf-8-sig", errors="ignore")
        m = re.search(r"^# 中文字体: (.+)$", text, re.M)
        if (m and (game_dir / m.group(1).strip()).is_file()
                and "_zz_font_select" in text):
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
    "        " + _LANG_UI_MARKER + "\n"
    "        vbox:\n"
    '            style_prefix "radio"\n'
    '            label _("Language")\n'
    "            $ _lang_ui_names = _lang_ui_languages()\n"
    "            for _lang_ui_i in _lang_ui_names:\n"
    '                $ _lang_ui_label = "English" if _lang_ui_i is None else _lang_ui_i\n'
    "                textbutton _(_lang_ui_label) selected "
    "(_preferences.language == _lang_ui_i) action "
    "Function(_lang_ui_switch, _lang_ui_i) style \"radio_button\"\n"
)

# 字体选项（设置界面）：单选「汉化默认字体 / 微软雅黑」。
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


def _font_list_entries(indent: str, style_suffix: str) -> list[str]:
    """追加到原生字体列表的条目（与原生条目同缩进 / 同样式）。"""
    return [
        f"{indent}{_FONT_UI_MARKER}",
        f"{indent}if _zz_font_choices:",
        f"{indent}    textbutton \"汉化默认字体\" selected "
        f"(persistent._zz_font_choice != 'yahei') action "
        f"Function(_zz_font_select, 'cjk'){style_suffix}",
        f"{indent}    if 'yahei' in _zz_font_choices:",
        f"{indent}        textbutton \"微软雅黑\" selected "
        f"(persistent._zz_font_choice == 'yahei') action "
        f"Function(_zz_font_select, 'yahei'){style_suffix}",
    ]


def _apply_font_ui(block: str, language: str) -> str:
    """把字体选项加进屏幕文本，返回**整段**屏幕定义（已含插入内容）。

    两种模式：
    - 定位到游戏原生字体列表 → 把条目插入该列表末尾（不新增 Font 标题，
      避免设置界面出现两个 Font 段）；
    - 没有原生列表 → 在屏幕末尾追加自有「字体」段，且仅在当前语言为汉化
      目标语言时显示。
    """
    found = _find_native_font_list(block)
    if found is not None:
        end, indent, style_suffix = found
        lines = block.rstrip("\n").splitlines()
        lines[end + 1:end + 1] = _font_list_entries(indent, style_suffix)
        return "\n".join(lines) + "\n"
    # 无原生字体列表：自有「字体」段，仅在当前语言 == 汉化语言时显示
    return block.rstrip("\n") + "\n" + (
        "        " + _FONT_UI_MARKER + "\n"
        f"        if _preferences.language == {language!r}:\n"
        "            vbox:\n"
        '                style_prefix "radio"\n'
        '                label _("Font")\n'
        "                if _zz_font_choices:\n"
        "                    textbutton \"汉化默认字体\" selected "
        "(persistent._zz_font_choice != 'yahei') action "
        "Function(_zz_font_select, 'cjk') style \"radio_button\"\n"
        "                    if 'yahei' in _zz_font_choices:\n"
        "                        textbutton \"微软雅黑\" selected "
        "(persistent._zz_font_choice == 'yahei') action "
        "Function(_zz_font_select, 'yahei') style \"radio_button\"\n"
    )


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
    "\n"
    "init python:\n"
    "    def _lang_ui_languages():\n"
    "        # 可用语言列表（含默认英文 None）。跨 Ren'Py 版本兼容。\n"
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
    "\n"
    "    def _lang_ui_switch(language):\n"
    "        # 老版 Ren'Py 无 Preference('language')，直接切换语言。\n"
    "        renpy.change_language(language)\n"
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
        "Traditional Chinese": "繁体中文",
        "Chinese (Traditional)": "繁体中文",
        "Chinese": "中文",
        "English": "英语",
        "Font": "字体",
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

_LANG_UI_PATTERNS = (
    r'Preference\(\s*["\']language["\']',
    r"renpy\.translation\.(?:get_languages|known_languages)\s*\(",
    r"\blanguage_button\b",
)


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


def _has_language_ui(game_dir: Path, language: str = "schinese") -> bool:
    """判断游戏是否已具备可切换到目标语言的语言界面。

    满足其一即视为已具备：
    1. 动态语言列表（get_languages / Preference("language") / language_button）
       —— 新语言会自动出现在列表中
    2. 硬编码语言按钮中包含目标语言标签（如 Language("schinese")）

    两个必须排除的干扰源（都曾导致语言界面被误判为「游戏已有」而不再注入）：
    - 本工具自己注入的 ``zz_*.rpy`` / ``zz_*.rpyc`` 补丁：它们的注释里就写着
      ``Preference('language')`` 这样的说明文字，扫进去必然命中；
    - 注释行：游戏/工具源码里「提到」某 API 不等于「使用」该 API。
    """
    # 硬编码按钮含目标语言
    hardcoded = (
        r'Language\(\s*["\']' + re.escape(language) + r'["\']',
        r'Language\(\s*["\']chinese["\']',
    )

    def _check(text: str) -> bool:
        stripped = _strip_comments(text)
        if any(re.search(p, stripped) for p in _LANG_UI_PATTERNS):
            return True
        return any(re.search(p, stripped) for p in hardcoded)

    # 先扫 .rpy（跳过本工具注入的 zz_* 补丁）
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            if fn.lower().startswith("zz_"):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            if _check(text):
                return True
    # screens.rpyc 反编译检查（同样跳过 zz_*）
    for fn in ("screens.rpyc", "screens.rpymc"):
        p = game_dir / fn
        if not p.is_file() or fn.lower().startswith("zz_"):
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
    # 3. 全目录搜索（源码）。跳过本工具注入的 zz_*.rpy 补丁——
    #    它们 redefine 了 preferences 屏幕，若被当成“游戏源码”，
    #    会因块内已有注入标记而无法再次注入（无限升级失败）。
    for dirpath, dirnames, filenames in os.walk(game_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() not in (".rpy", ".rpym"):
                continue
            if fn.lower().startswith("zz_"):
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
            # 跳过本工具注入的 zz_* 补丁（.rpyc 同样要跳！否则会把上轮
            # 自己生成的补丁当成「游戏源码」，在它之上反复注入，导致
            # 块内容逐轮退化——语言段就是在这一步被丢掉的）。
            if fn.lower().startswith("zz_"):
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


def _count_screen_defs(text: str, name: str) -> int:
    """统计文本里 ``screen <name>`` 定义的个数（含 ``init -N`` 前缀写法）。"""
    pat = re.compile(r"^\s*(?:init\s+-?\d+\s+)?screen\s+" + re.escape(name) + r"\b")
    return sum(1 for ln in text.splitlines() if pat.match(ln))


def _build_language_ui(source_text: str, with_font: bool = False,
                      language: str = "schinese") -> str | None:
    """在 preferences 屏幕块末尾注入语言选择器（及可选的字体选项）。

    返回新的整个屏幕定义；屏幕块已注入过或定位不到时返回 None。
    字体选项优先追加进游戏的**原生字体列表**（避免出现两个 Font 段）。
    """
    block = _extract_screen_block(source_text, "preferences")
    if block is None:
        return None
    if _LANG_UI_MARKER in block:
        return None  # 已注入
    out = block.rstrip() + "\n" + _LANG_UI_SNIPPET
    if with_font:
        out = _apply_font_ui(out, language)
    return out + _LANG_UI_HELPER


def apply_language_ui(game_dir: Path, language: str = "schinese",
                      with_font: bool = False) -> PatchResult:
    """确保游戏设置界面有语言切换（及可选字体）选项，返回结果。"""
    res = PatchResult()
    patch = game_dir / "zz_language_ui.rpy"

    def _inject(with_lang: bool) -> None:
        """从游戏原始 preferences 屏幕重建注入文件（with_lang 控制是否含语言）。"""
        source, src_path = _find_preferences_source(game_dir)
        if source is None:
            res.ok = False
            res.message = "未找到 preferences 屏幕定义，无法注入设置界面选项"
            return
        block = _extract_screen_block(source, "preferences")
        if block is None or _LANG_UI_MARKER in block:
            res.ok = False
            res.message = "无法定位 preferences 屏幕块，跳过设置界面注入"
            return
        body = block.rstrip() + "\n"
        if with_lang:
            body += _LANG_UI_SNIPPET
        # 字体选项仅在字体补丁确实就位时注入——否则屏幕会引用未定义的
        # _zz_font_choices / _zz_font_select（虽然 helper 有兜底空表，
        # 但缺补丁时字体选项本身也没有意义）。
        want_font = with_font and _font_ui_available(game_dir)
        if want_font:
            # 有原生字体列表 → 追加进去；否则注入自有段（仅在中文下显示）
            body = _apply_font_ui(body, language)
        try:
            header = (
                "# -*- coding: utf-8 -*-\n"
                "# 设置界面选项（汉化工具自动生成）：语言切换 / 字体选择。\n"
                f"# 来源: {src_path.name if src_path else '归档内 screens 脚本'}\n\n"
            )
            patch.write_text(header + body + _LANG_UI_HELPER,
                             encoding="utf-8-sig")
        except OSError as e:
            res.ok = False
            res.message = f"写设置界面补丁失败: {e}"
            return
        res.files.append(patch)
        res.ok = True
        bits = ([] + ["语言切换"] if with_lang else []) + \
               (["字体选项"] if want_font else [])
        res.detail = "重启游戏后，在 设置 界面即可使用「%s」" % "、".join(bits)
        res.message = "已注入设置界面选项（%s）" % "、".join(bits)

    # 只有字体补丁确实就位时，字体选项才有意义（见 _font_ui_available）
    want_font = with_font and _font_ui_available(game_dir)

    # 1. 游戏已有语言切换 → 仅当需要字体选项时注入字体（不重复加语言）
    if _has_language_ui(game_dir, language):
        if not want_font:
            res.ok = True
            res.skip = True
            res.message = "游戏已自带语言切换界面，跳过注入"
            return res
        _inject(with_lang=False)
        if res.ok:
            res.message = "游戏已自带语言切换界面，已补充字体选项"
        return res
    # 2. 已有补丁 → 仅在「字体选项该有/不该有」与现状不一致时重建
    if patch.is_file():
        try:
            text = patch.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            text = ""
        if _LANG_UI_MARKER in text:
            # 结构自检：补丁里 preferences 屏幕定义必须恰好 1 个，且语言段
            # 不能只有标记没有按钮——历史上出现过「_has_language_ui 误判
            # 游戏已有语言界面 → 重注入时只写字体段」的坏文件：语言标记
            # 还在（helper 里），但语言列表按钮整个没了。判定「已注入」必须
            # 看实际按钮代码（_lang_ui_label 只出现在语言段的按钮行里）。
            sane = (_count_screen_defs(text, "preferences") == 1
                    and "_lang_ui_label" in text)
            if sane and (_FONT_UI_MARKER in text) == want_font:
                res.ok = True
                res.skip = True
                res.message = "设置界面选项已注入，跳过"
                return res
            # 需要补字体选项 / 移除失效字体选项 / 修复损坏文件 → 重建
            _inject(with_lang=True)
            if res.ok:
                if not sane:
                    res.message = "设置界面补丁结构异常（屏幕被重复写入），已重建"
                else:
                    res.message = ("已为现有设置界面补上字体选项" if want_font
                                   else "已移除失效的字体选项（字体补丁缺失）")
            return res

    # 3. 全新注入（语言 + 可选字体）
    _inject(with_lang=True)
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
        # 字体补丁启用时，设置界面同时提供「字体」选项（汉化默认 / 微软雅黑）
        results.append(apply_language_ui(game_dir, language,
                                         with_font=with_font))
        name_res = ensure_language_name(game_dir, language)
        if name_res is not None:
            results.append(name_res)
    return results
