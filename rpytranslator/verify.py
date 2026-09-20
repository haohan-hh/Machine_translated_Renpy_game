# -*- coding: utf-8 -*-
"""
运行时翻译验证与自动补译。

背景（Dawn Chorus 等项目实测发现的盲区）：
汉化工具按「反编译脚本」提取文本并生成翻译，但它只能验证
「提取到的文本是否都翻完了」，无法验证「游戏运行时实际显示的所有对话
是否都有翻译」。个别对话可能因为
  1. say 的属性写法特殊（如 ``Mikko strained "..."`` 的裸词属性），
  2. 反编译/提取的 identifier 与官方编译器算出的不一致，
  3. 特殊语法（NVL 段落、多行字符串、块结构）覆盖不到，
而从未被提取/翻译——工具自认为 100% 完成，游戏里却仍是英文
（表现为「部分角色中文、部分角色英文」）。

本模块提供闭环验证：
1. ``write_verification_patch()`` —— 汉化完成后向游戏注入
   ``game/zz_verify_translations.rpy``（init 2000，可安全删除）。
   游戏启动时由 Ren'Py 读取自身的对话翻译注册表
   ``renpy.game.script.translator.default_translates``（全部对话，
   实测为 dict[identifier -> TranslateSay/Translate]）与
   ``language_translates``（dict[(identifier, 语言) -> 翻译]）做精确比对，
   把「运行时实际缺少指定语言翻译」的对话写入
   ``game/tl/<语言>.运行时缺失报告.txt``。
2. ``build_missing_fix()`` —— 再次运行汉化工具时若发现该报告，
   自动为其中仍有实际文本的对话补译（优先复用现有 tl 译文，
   缺失的走 AI），按运行时 identifier 生成
   ``tl/<语言>/zz_missing_fix.rpy``，保证补丁逐条命中。

补充经验固化：
- 空对话 / ``!`` / ``...`` / ``{nw}`` / 纯标签 / 纯换行段等无实际文字的
  「噪音」条目不会被误当作漏译（``is_noise_text``）。
- 多行（NVL）文本补译时只翻译去空前导空行的正文，保留原换行结构，
  显著提高 AI 保留 ``\\n`` 的成功率（此前整段翻译极易丢失占位符）。
- ``Translate`` 类型实测（Ren'Py 8.2）其块内至多包含一个 ``Say``（block 为
  ``[UserStatement, Say]`` 或仅 ``[UserStatement]``），无 Say 即无文字（噪音），
  含 Say 的可与 ``TranslateSay`` 同样安全自动补译；若极端环境出现多 Say 块
  则跳过并提示，避免生成结构不符的块导致 Ren'Py 编译失败（整份翻译失效）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .extract import decode_say_string, encode_say_string
from .patcher import PatchResult

# 运行时验证补丁文件名（game 根目录；zz_ 前缀是工具自动生成的约定，
# engine 扫描 / 更新脚本时会跳过）
RUNTIME_PATCH_NAME = "zz_verify_translations.rpy"
# 运行时缺失报告文件名（放 tl/ 下，随汉化产物一起管理）
RUNTIME_REPORT_TEMPLATE = "{lang}.运行时缺失报告.txt"
# 自动修复补丁文件名（tl/<语言>/ 下）
FIX_FILE_NAME = "zz_missing_fix.rpy"

# 报告行：file:line \t [who] \t what \t identifier \t 类型
_TAB = "\t"
_TITLE_ROWS = 3  # 前 3 行为统计标题

# Ren'Py 文本标签 {…} / 插值 […]（与 translator 一致）
_TAG_RE = re.compile(r"\{[^{}]*\}")
_INSERT_RE = re.compile(r"\[[^\[\]]*\]")

_TL_BLOCK_RE = re.compile(r"translate\s+(\S+)\s+([^\s:]+):\s*$")


# ---------------------------------------------------------------------------
# 无实际文字的“噪音”判定
# ---------------------------------------------------------------------------

def is_noise_text(what: str | None) -> bool:
    """判断对话文本是否没有实际可翻译内容（空 / 符号 / 纯标签 / 空行段）。

    Ren'Py 中 ``""``（空停顿）、``!``、``...``、``{nw}``、纯 ``\\n``
    （NVL 留白）等对话不显示文字，未翻译也不会造成“部分英文”，
    验证与自动修复都应跳过它们。
    """
    w = (what or "").strip()
    if not w:
        return True
    w = w.replace("{nw}", "").strip()
    if not w:
        return True
    if re.fullmatch(r"[!?….\s，。、；：\-—()（）]+", w):
        return True
    plain = _TAG_RE.sub("", _INSERT_RE.sub("", w)).strip()
    if not plain:                      # 纯标签/插值，如 {size=+2} [page]
        return True
    return False


# ---------------------------------------------------------------------------
# 运行时验证补丁
# ---------------------------------------------------------------------------

# 内嵌的 Ren'Py 脚本（init 2000）。__LANG__ 在生成时替换为目标语言。
# 兼容 Ren'Py 7.4+ / 8.x：8.x 的翻译注册表位于
#   renpy.game.script.translator (ScriptTranslator)
# 7.x 早期版本位于 renpy.game.script.translations；脚本对两者做探测回退。
_RUNTIME_PATCH = r'''# -*- coding: utf-8 -*-
# 运行时翻译缺失诊断（汉化工具自动生成，可安全删除）。
# 游戏启动时比对 Ren'Py 对话翻译注册表，把运行时实际缺少 __LANG__ 翻译的
# 对话写入 tl/__LANG__.运行时缺失报告.txt；再次运行汉化工具可据此自动补译。
init 2000 python:
    import io as _vv_io
    import os as _vv_os
    _vv_lang = '__LANG__'
    _vv_path = _vv_os.path.join(config.gamedir, 'tl',
                                _vv_lang + '.运行时缺失报告.txt')
    try:
        # 兼容两种注册表位置（8.x: script.translator；7.x: script.translations）
        _vv_tr = None
        _vv_script = renpy.game.script
        if hasattr(_vv_script, 'translator'):
            _vv_tr = _vv_script.translator
        elif hasattr(_vv_script, 'translations'):
            _vv_tr = _vv_script.translations
        if _vv_tr is None:
            _vv_tr = renpy.translation
        _vv_def = getattr(_vv_tr, 'default_translates', None)
        if _vv_def is None:
            raise Exception('no default_translates')
        _vv_lng = getattr(_vv_tr, 'language_translates', {})
        if not hasattr(_vv_lng, '__contains__'):
            _vv_lng = {}
        _vv_rows = []
        _vv_miss = 0
        for _vv_code in _vv_def:
            if (_vv_code, _vv_lang) in _vv_lng:
                continue
            _vv_node = _vv_def[_vv_code]
            _vv_who = getattr(_vv_node, 'who', None)
            _vv_what = getattr(_vv_node, 'what', '')
            if _vv_who is None:
                _vv_blk = getattr(_vv_node, 'block', None)
                if _vv_blk:
                    for _vv_e in _vv_blk:
                        if type(_vv_e).__name__ == 'Say':
                            _vv_who = getattr(_vv_e, 'who', None)
                            _vv_what = getattr(_vv_e, 'what', '')
                            break
            if _vv_what is None:
                _vv_what = ''
            _vv_what2 = str(_vv_what).replace('\n', '\\n').replace('\t', ' ')
            _vv_who2 = '[' + (str(_vv_who) if _vv_who is not None else '') + ']'
            _vv_fn = str(getattr(_vv_node, 'filename', '') or '')
            _vv_ln = getattr(_vv_node, 'linenumber', 0) or 0
            _vv_rows.append('%s:%s\t%s\t%s\t%s\t%s' % (
                _vv_fn, _vv_ln, _vv_who2, _vv_what2, _vv_code,
                type(_vv_node).__name__))
            _vv_miss += 1
        _vv_dir = _vv_os.path.dirname(_vv_path)
        if not _vv_os.path.isdir(_vv_dir):
            _vv_os.makedirs(_vv_dir)
        with _vv_io.open(_vv_path, 'w', encoding='utf-8') as _vv_f:
            _vv_f.write('total dialogue ids: %d\n' % len(_vv_def))
            _vv_f.write('missing ' + _vv_lang + ' translation: %d\n' % _vv_miss)
            _vv_f.write('---- missing list (file:line [who] what id type) ----\n')
            if _vv_rows:
                _vv_f.write('\n'.join(_vv_rows))
    except Exception:
        pass
'''


def runtime_report_path(game_dir: Path, language: str) -> Path:
    return game_dir / "tl" / RUNTIME_REPORT_TEMPLATE.format(lang=language)


def write_verification_patch(game_dir: Path, language: str = "schinese",
                             log_cb=None) -> PatchResult:
    """向游戏注入运行时验证补丁，返回结果（已存在且内容一致 → skip）。"""
    res = PatchResult()
    patch = game_dir / RUNTIME_PATCH_NAME
    content = _RUNTIME_PATCH.replace("__LANG__", language)
    if patch.is_file():
        try:
            if patch.read_text(encoding="utf-8-sig", errors="ignore") == content:
                res.ok = True
                res.skip = True
                res.message = "运行时翻译验证补丁已存在，跳过"
                return res
        except OSError:
            pass
    try:
        patch.write_text(content, encoding="utf-8-sig")
    except OSError as e:
        res.ok = False
        res.message = f"写运行时验证补丁失败: {e}"
        return res
    res.files.append(patch)
    res.ok = True
    res.detail = (f"启动一次游戏后，若运行时仍有对话缺失翻译，工具会生成 "
                  f"tl/{language}.运行时缺失报告.txt；再次汉化即可自动补译")
    res.message = "已注入运行时翻译验证补丁（游戏启动时自动检查漏译）"
    if log_cb:
        log_cb("  ✓ " + res.message)
        log_cb("    - " + res.detail)
    return res


# ---------------------------------------------------------------------------
# 缺失报告解析
# ---------------------------------------------------------------------------

@dataclass
class MissingItem:
    filename: str
    line: int
    who: str
    what: str
    identifier: str
    node_type: str          # TranslateSay / Translate

    @property
    def is_say(self) -> bool:
        """单句对话（可安全自动补译）；Translate 为多句块，只读不写。"""
        return self.node_type == "TranslateSay"


def parse_runtime_report(path: Path) -> list[MissingItem]:
    """解析运行时缺失报告。返回全部条目（含噪音，调用方自行过滤）。"""
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except OSError:
        return []
    items: list[MissingItem] = []
    for ln in lines[_TITLE_ROWS:]:
        parts = ln.split(_TAB)
        if len(parts) < 5:
            continue
        # 结构固定为: 文件:行 / [who] / what / id / 类型；正文里的制表符
        # 已被补丁替换为空格，仍可能残留极端情况，故取前后列、正文段合并。
        where, who = parts[0], parts[1]
        identifier, node_type = parts[-2], parts[-1]
        what = _TAB.join(parts[2:-2])
        who = who.strip("[]")
        what = what.replace("\\n", "\n")
        fname, _, line_s = where.partition(":")
        try:
            line = int(line_s)
        except ValueError:
            line = 0
        items.append(MissingItem(
            filename=fname, line=line, who=who, what=what,
            identifier=identifier, node_type=node_type.strip() or "TranslateSay"))
    return items


# ---------------------------------------------------------------------------
# 已有译文反查（原文 → 译文）
# ---------------------------------------------------------------------------

def _decode_say(raw: str) -> str:
    """还原 rpy 字符串：``\\\\n → 换行、\\\\" → "、\\\\ → 空格、\\\\\\\\ → \\``。

    透传到 ``extract.decode_say_string``，对已有 tl 的解析保留统一行为
    （含 @@pN@@p 编译位置标记剥离，避免与生成器产生的原文不一致）。
    """
    return decode_say_string(raw)


def load_tl_dialogue_map(tl_lang_dir: Path, language: str) -> dict[str, str]:
    """加载 tl/<语言> 中全部对话翻译：{原文(去首尾空白): 译文}。

    原文取自翻译块的注释行（# who "原文"），译文取自块内首个非注释行；
    与生成器输出格式一致，也兼容官方/手工格式。多行文本以还原后的
    真实换行存储。
    """
    mapping: dict[str, str] = {}
    if not tl_lang_dir.is_dir():
        return mapping
    for f in sorted(tl_lang_dir.rglob("*.rpy")):
        try:
            lines = f.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        except OSError:
            continue
        i, n = 0, len(lines)
        while i < n:
            m = _TL_BLOCK_RE.match(lines[i].strip())
            if not m or m.group(1) != language or m.group(2) == "strings":
                i += 1
                continue
            i += 1
            com: str | None = None
            tl: str | None = None
            while i < n:
                s = lines[i].strip()
                if not s:
                    i += 1
                    continue
                if _TL_BLOCK_RE.match(s):
                    break
                q, rq = s.find('"'), s.rfind('"')
                if q == -1 or rq <= q:
                    i += 1
                    continue
                inner = s[q + 1:rq]
                if s.startswith("#"):
                    if com is None:
                        com = _decode_say(inner)
                else:
                    tl = _decode_say(inner)
                    i += 1
                    break
                i += 1
            if com is not None and tl is not None and com.strip():
                mapping[com.strip()] = tl
    return mapping


# ---------------------------------------------------------------------------
# 自动补译修复补丁
# ---------------------------------------------------------------------------

def _split_leading_ws(s: str) -> tuple[str, str]:
    """把文本拆成「前导空白（含换行）+ 正文」。NVL 段常以 \\n 空行开头，
    补译时只翻译正文，换行结构原样恢复，避免 AI 丢占位符。"""
    m = re.match(r"^([\s]*)(.*)$", s, re.S)
    return m.group(1), m.group(2)


def build_missing_fix(
    game_dir: Path,
    language: str = "schinese",
    client=None,
    names: list[str] | None = None,
    log_cb=None,
) -> PatchResult:
    """根据运行时缺失报告生成 tl/<语言>/zz_missing_fix.rpy。

    规则（实测经验固化，避免踩坏已生效翻译）：
    - 空 / 符号 / 纯标签 / 纯换行段（is_noise_text）→ 跳过，无文字不补。
    - TranslateSay / 含单个 Say 的 Translate → 优先复用现有 tl 译文；
      无译文则 AI 翻译；多行文本先拆出前导空白再翻正文。
    - 无 Say 的 Translate 块（纯 UserStatement 段落）→ 无文字，跳过。
    幂等：已生成的 zz_missing_fix.rpy 会先被解析合并，重复运行不丢旧修复。
    """
    res = PatchResult()
    report = runtime_report_path(game_dir, language)
    if not report.is_file():
        res.ok = True
        res.skip = True
        res.message = "未发现运行时缺失报告，跳过自动补译"
        if log_cb:
            log_cb("  - " + res.message)
        return res

    tl_dir = game_dir / "tl" / language
    items = parse_runtime_report(report)
    if not items:
        res.ok = True
        res.skip = True
        res.message = f"运行时缺失报告无条目：{report.name}"
        return res
    if log_cb:
        log_cb(f"  - 运行时缺失报告：共 {len(items)} 条")

    noise = 0
    reuse = 0
    need_ai = 0
    failed = 0
    # identifier -> (who, lead空白, src原文, 完整译文)
    resolved: dict[str, tuple[str, str, str, str]] = {}

    # 1. 读取旧修复补丁（幂等合并）
    fix_file = tl_dir / FIX_FILE_NAME
    if fix_file.is_file():
        try:
            _merge_old_fix(fix_file, language, resolved)
        except OSError:
            pass

    # 2. 已有 tl 译文反查表
    tl_map = load_tl_dialogue_map(tl_dir, language)

    to_ai: list[tuple[int, str]] = []     # (原索引, 待翻译正文)
    pending: list[MissingItem] = []
    for it in items:
        if it.identifier in resolved:
            continue
        if is_noise_text(it.what):
            noise += 1
            continue
        # 已有译文复用（多行文本以去前导空白的正文匹配）
        lead, body = _split_leading_ws(it.what)
        key = (body or it.what).strip()
        tr = tl_map.get(key)
        if tr and tr.strip() != key:
            resolved[it.identifier] = (it.who, lead, it.what, lead + tr.strip())
            reuse += 1
        else:
            to_ai.append((len(pending), key))
            pending.append(it)

    # 3. AI 翻译缺失的正文
    ai_trans: list[str] = []
    if to_ai and client is not None:
        texts = [key for _, key in to_ai]
        if log_cb:
            log_cb(f"  - 自动补译 {len(texts)} 条（运行时实际缺少翻译的对话）…")
        ai_trans = client.translate_texts(texts, names=names or [])
    for (pi, key), tr in zip(to_ai, ai_trans):
        it = pending[pi]
        if tr and tr.strip() and tr.strip() != key:
            lead, _ = _split_leading_ws(it.what)
            resolved[it.identifier] = (it.who, lead, it.what, lead + tr.strip())
            need_ai += 1
        else:
            failed += 1

    # 4. 写补丁（合并旧修复）
    if resolved:
        _write_fix_file(fix_file, language, resolved)
        res.files.append(fix_file)
    summary = (f"运行时缺失补译完成：新复用 {reuse} 条、新翻译 {need_ai} 条，"
               f"跳过空/符号等无文字条目 {noise} 条"
               + (f"、翻译失败 {failed} 条" if failed else ""))
    res.ok = True
    res.message = summary
    res.detail = f"补丁文件: {fix_file.name if fix_file.exists() else FIX_FILE_NAME}"
    if log_cb:
        log_cb("  ✓ " + summary)
        if not resolved:
            log_cb("  - 本次没有需要补译的条目")
    return res


def _merge_old_fix(path: Path, language: str,
                   resolved: dict[str, tuple[str, str, str, str]]) -> None:
    """把旧 zz_missing_fix.rpy 中已有块并入 resolved，实现幂等合并。"""
    lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    i, n = 0, len(lines)
    while i < n:
        m = _TL_BLOCK_RE.match(lines[i].strip())
        if not m or m.group(1) != language or m.group(2) == "strings":
            i += 1
            continue
        ident = m.group(2)
        i += 1
        com: str | None = None
        who: str | None = None
        tl: str | None = None
        while i < n:
            s = lines[i].strip()
            if not s:
                i += 1
                continue
            if _TL_BLOCK_RE.match(s):
                break
            if s.startswith("#"):
                if com is None:
                    # # who "原文"（who 可选）
                    rest = s[1:].strip()
                    q, rq = rest.find('"'), rest.rfind('"')
                    if q != -1 and rq > q:
                        head = rest[:q].strip()
                        who = head or None
                        com = _decode_say(rest[q + 1:rq])
            else:
                q, rq = s.find('"'), s.rfind('"')
                if q != -1 and rq > q:
                    tl = _decode_say(s[q + 1:rq])
                    if not who:
                        head = s[:q].strip()
                        who = head or None
                    break
            i += 1
        if com is not None and tl is not None:
            lead, body = _split_leading_ws(com)
            resolved[ident] = (who or "", lead, com, lead + tl.strip())
        else:
            i += 1


def _write_fix_file(
    path: Path,
    language: str,
    resolved: dict[str, tuple[str, str, str, str]],
) -> None:
    """写 tl/<语言>/zz_missing_fix.rpy。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "# -*- coding: utf-8 -*-",
        "# 运行时缺失对话自动补译（汉化工具生成）：按 Ren'Py 运行时 identifier",
        "# 补齐翻译，可与主汉化文件共存；删除本文件即可回退这部分补译。",
    ]
    for ident in sorted(resolved):
        who, _lead, src, full = resolved[ident]
        com = encode_say_string(src)
        tln = encode_say_string(full)
        parts.append("")
        parts.append(f"translate {language} {ident}:")
        parts.append("")
        if who:
            parts.append("    # " + who + " " + com)
            parts.append("    " + who + " " + tln)
        else:
            parts.append("    # " + com)
            parts.append("    " + tln)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8-sig")
