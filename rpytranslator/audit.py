# -*- coding: utf-8 -*-
"""补漏查缺：扫描已生成的 tl/<语言>/ 翻译文件，分类检测残留问题。

三类问题（报告中的中文类别名）：

1. ``untranslated`` 未翻译残留：译文与原文相同，且原文含成串英文单词。
2. ``mixed`` 中英参半：译文含目标语言文字，同时残留成段英文（连续 ≥3 个
   英文单词）。单个英文单词多为有意保留的人名 / 术语（H.A.A.L.O、Mike），
   不算问题，避免误报。
3. ``wrong_lang`` 目标语言不正确：译文不含目标语言文字，却含其他语言
   文字（日文假名 / 韩文 / 西里尔 / 泰文 / 希腊文），或整句仍是英文
   且与原文不同（模型把原文「英文改写」了一遍而没有翻译）。

检测结果写入 ``tl/<语言>.补漏查缺报告.txt`` 供人工逐条核查（含
文件:行号 定位）；同时把问题项原文合并进「未翻译报告」，下次运行
「开始汉化」时增量模式会自动重译这些文本——这就是「补充修正」的闭环。

解析格式与 generator.py 的输出逐字节对称（encode_say_string 的反转义）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 语言 → 目标文字
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")          # 汉字
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")          # 平/片假名
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")        # 韩文
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")                   # 西里尔（俄文等）
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")                       # 泰文
_GREEK_RE = re.compile(r"[\u0370-\u03ff]")                      # 希腊文
_ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")                    # 英文单词（≥2 字母）

# 各目标语言「应有的文字」。None（英语）表示以 ASCII 字母为期望。
_EXPECTED_SCRIPTS: dict[str, re.Pattern | None] = {
    "schinese": _CJK_RE, "tchinese": _CJK_RE,
    "zh": _CJK_RE, "zh_cn": _CJK_RE, "zh_hans": _CJK_RE, "zh_hant": _CJK_RE,
    "japanese": _KANA_RE, "ja": _KANA_RE,
    "korean": _HANGUL_RE, "ko": _HANGUL_RE,
    "english": None, "en": None,
}

# 「错误语言」的文字特征：(显示名, 正则)。
# 期望文字本身不会被判为错误（如日语目标的假名）。
_OTHER_SCRIPTS: list[tuple[str, re.Pattern]] = [
    ("日文假名", _KANA_RE),
    ("韩文", _HANGUL_RE),
    ("西里尔文(俄文等)", _CYRILLIC_RE),
    ("泰文", _THAI_RE),
    ("希腊文", _GREEK_RE),
    ("汉字", _CJK_RE),        # 仅当目标不是中文时才视为异常
]

# 2 个及以上连续英文单词（"Good morning"）→ 疑似未翻译
_WORD_RUN2_RE = re.compile(r"[A-Za-z]{2,}(?:[\s'\u2019\-]+[A-Za-z]{2,}){1,}")
# 3 个及以上连续英文单词（"the door is locked"）→ 疑似整句漏译
_WORD_RUN3_RE = re.compile(r"[A-Za-z]{2,}(?:[\s'\u2019\-]+[A-Za-z]{2,}){2,}")


def _norm_language(language: str) -> str:
    """GUI 的语言显示名（如 ``schinese（简体中文）``）→ 语言码。"""
    return language.split("（")[0].split("(")[0].strip().lower()


@dataclass
class AuditItem:
    """一个翻译条目及其判定结果（category 为空表示无问题）。"""
    tl_file: str          # tl 文件名（相对 tl/<语言>/）
    src_file: str         # 源文件（注释里的位置标记）
    src_line: int
    who: str = ""
    original: str = ""
    translation: str = ""
    category: str = ""    # untranslated / mixed / wrong_lang；空 = 正常
    note: str = ""        # 判定依据（报告里展示）


@dataclass
class AuditSummary:
    total: int = 0
    counts: dict = field(default_factory=dict)   # category -> 条数
    report_path: str = ""
    queued: int = 0                                # 合并进未翻译报告的条数


# ---------------------------------------------------------------------------
# tl 文件解析（与 generator.py 的输出对称）
# ---------------------------------------------------------------------------

_LOC_RE = re.compile(r"^#\s*(.+?):(\d+)\s*$")
_TRANSLATE_RE = re.compile(r"^translate\s+\S+\s+(\S+):\s*$")
_STRINGS_RE = re.compile(r"^translate\s+\S+\s+strings:\s*$")
_OLD_RE = re.compile(r"^old\s+(.*)$")
_NEW_RE = re.compile(r"^new\s+(.*)$")
# 注释原句行（块内缩进已被 strip）：`who "..."` 或 `"..."`
_ORIG_RE = re.compile(r"^#\s*(?:(\S+)\s+)?(\".*)$")
# 译文行：`who "..."` 或 `"..."`
_SAY_RE = re.compile(r"^(?:(\S+)\s+)?(\".*)$")


def _decode_say_string(s: str) -> str:
    r"""反转义 generator.encode_say_string（\n、\"、\\、双空格前的\ ）。"""
    if not s:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == '"':
                out.append('"')
                i += 2
                continue
            if nxt == " ":
                out.append(" ")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _quoted(text: str) -> str:
    """取行内成对引号中的内容并反转义（`"abc"` / ``'abc'`` → ``abc``）。

    对话块用双引号（generator 的输出）；工具自己生成的显示名映射
    （zz_language_display.rpy）用单引号，两种都要吃得下。
    不合法（没有成对引号）时返回原文。
    """
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return _decode_say_string(s[1:-1])
    return s


def _strip_markup(text: str) -> str:
    """去掉 Ren'Py 标签 {…} 与插值 […]，只留要给玩家看的字面文本。"""
    text = re.sub(r"\{[^{}]*\}", " ", text)
    text = re.sub(r"\[[^\[\]]*\]", " ", text)
    return text


def parse_tl_file(path: Path) -> list[AuditItem]:
    """解析一个 tl/<语言>/xxx.rpy，返回其中的对话与字符串翻译条目。"""
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except OSError:
        return []
    items: list[AuditItem] = []
    src_file, src_line = path.name, 0
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()
        m = _LOC_RE.match(s)
        if m:
            src_file, src_line = m.group(1), int(m.group(2))
            i += 1
            continue
        if _TRANSLATE_RE.match(s) and not _STRINGS_RE.match(s):
            # 对话块：其后第一行 `# ...` 为原句，下一个非注释行为译文
            who, original, translation = "", "", ""
            j = i + 1
            while j < n:
                t = lines[j].strip()
                if not t:
                    j += 1
                    continue
                if t.startswith("#"):
                    if not original:
                        mm = _ORIG_RE.match(t)
                        if mm:
                            who = mm.group(1) or ""
                            original = _quoted(mm.group(2))
                    j += 1
                    continue
                # 第一个非注释、非空行 = 译文行
                mm = _SAY_RE.match(t)
                if mm:
                    translation = _quoted(mm.group(2))
                break
            if original or translation:
                items.append(AuditItem(
                    tl_file=path.name, src_file=src_file, src_line=src_line,
                    who=who, original=original, translation=translation))
            # 跳到块外（下一个 0 缩进的行）
            i = max(j, i + 1)
            while i < n and (not lines[i].strip() or lines[i][:1].isspace()):
                i += 1
            continue
        if _STRINGS_RE.match(s):
            # 字符串块：old "..." / new "..." 成对
            j = i + 1
            pending_old: str | None = None
            p_file, p_line = src_file, src_line
            while j < n:
                t = lines[j].strip()
                if not t:
                    j += 1
                    continue
                mloc = _LOC_RE.match(t)
                if mloc:
                    p_file, p_line = mloc.group(1), int(mloc.group(2))
                    j += 1
                    continue
                if t.startswith("translate"):
                    break
                mo = _OLD_RE.match(t)
                if mo:
                    pending_old = _quoted(mo.group(1))
                    j += 1
                    continue
                mn = _NEW_RE.match(t)
                if mn and pending_old is not None:
                    items.append(AuditItem(
                        tl_file=path.name, src_file=p_file, src_line=p_line,
                        original=pending_old, translation=_quoted(mn.group(1))))
                    pending_old = None
                    j += 1
                    continue
                j += 1
            i = j
            continue
        i += 1
    return items


# ---------------------------------------------------------------------------
# 分类判定
# ---------------------------------------------------------------------------

def classify(item: AuditItem, language: str) -> str | None:
    """对单条翻译判定问题类别；返回 None 表示正常。

    判定只在「原文确实是外文（不含目标语言文字）」时进行——
    游戏源文本本身已含中文（例如自带部分汉化）的条目跳过。
    """
    lang = _norm_language(language)
    expected_re = _EXPECTED_SCRIPTS.get(lang, _CJK_RE)
    orig, trans = item.original, item.translation
    if not orig.strip() or not trans.strip():
        return None
    # 原文已含目标语言文字 → 不是「待翻译成目标语言」的条目，跳过
    if expected_re is not None and expected_re.search(_strip_markup(orig)):
        return None
    if lang == "english" and _ASCII_WORD_RE.search(orig):
        pass  # 英文目标的原文检查在下面统一处理

    trans_plain = _strip_markup(trans)
    orig_plain = _strip_markup(orig)
    has_expected = (
        _ASCII_WORD_RE.search(trans_plain) is not None
        if expected_re is None
        else expected_re.search(trans_plain) is not None)

    # 1) 未翻译残留：译文 == 原文，且原文有成串英文单词
    if trans_plain.strip() == orig_plain.strip():
        if _WORD_RUN2_RE.search(orig_plain):
            return "untranslated"
        return None

    if not has_expected:
        # 3) 目标语言不正确
        #    a) 含其他语言文字（假名/韩文/西里尔/泰文/希腊；汉字在非中文目标下）
        for name, rx in _OTHER_SCRIPTS:
            if expected_re is not None and rx is expected_re:
                continue
            if lang.startswith("zh") or lang in ("schinese", "tchinese"):
                if rx is _CJK_RE:
                    continue
            elif expected_re is None and rx is _CJK_RE:
                pass  # 英文目标：译文是中文 → 确实异常，保留判定
            if rx.search(trans_plain):
                item.note = f"含{name}"
                return "wrong_lang"
        #    b) 整句仍是英文（与原文不同）——模型「改写」而非「翻译」
        if _WORD_RUN2_RE.search(trans_plain):
            item.note = "整句仍为英文"
            return "wrong_lang"
        return None

    # 2) 中英参半：有目标语言文字，又残留 ≥3 个连续英文单词
    if _WORD_RUN3_RE.search(trans_plain):
        item.note = "残留成段英文"
        return "mixed"
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

_CATEGORY_TITLES = {
    "untranslated": "未翻译残留（译文与原文相同）",
    "mixed": "中英参半（译文残留成段英文）",
    "wrong_lang": "目标语言不正确（含其他语言 / 整句英文）",
}


def _resolve_game_dir(game_path: str | Path) -> Path:
    p = Path(game_path)
    if (p / "game").is_dir():
        return p / "game"
    return p


def run_audit(game_path: str | Path, language: str, log=None,
              queue_for_retranslate: bool = True) -> AuditSummary:
    """扫描 tl/<语言>/ 全部翻译文件，写报告并把问题项排入重译队列。"""
    summary = AuditSummary()
    game_dir = _resolve_game_dir(game_path)
    lang = _norm_language(language)
    tl_dir = game_dir / "tl" / lang
    if not tl_dir.is_dir():
        if log:
            log(f"补漏查缺：未找到 {tl_dir}，跳过（先完成一次翻译）")
        return summary

    # 不扫注入的界面补丁（无 translate 块，扫了也无害，但明确排除省时间）
    all_items: list[AuditItem] = []
    for f in sorted(tl_dir.rglob("*.rpy")):
        if f.name in ("zz_cn_font.rpy", "zz_language_ui.rpy"):
            continue
        all_items.extend(parse_tl_file(f))

    problems: list[AuditItem] = []
    for it in all_items:
        cat = classify(it, lang)
        if cat:
            it.category = cat
            problems.append(it)

    summary.total = len(all_items)
    for it in problems:
        summary.counts[it.category] = summary.counts.get(it.category, 0) + 1

    report = tl_dir.parent / f"{lang}.补漏查缺报告.txt"
    summary.report_path = str(report)

    if problems:
        try:
            with open(report, "w", encoding="utf-8-sig") as f:
                f.write("补漏查缺报告（翻译完成后自动扫描）\n")
                f.write(f"扫描时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write(f"扫描范围: tl/{lang}/*.rpy，共 {len(all_items)} 条翻译\n")
                f.write(f"发现问题: {len(problems)} 条"
                        f"（已自动排入重译队列，点击「开始汉化」即可补充修正）\n")
                for cat, title in _CATEGORY_TITLES.items():
                    group = [it for it in problems if it.category == cat]
                    if not group:
                        continue
                    f.write(f"\n== {title}（{len(group)} 条）==\n")
                    for it in group:
                        where = f"{it.src_file}:{it.src_line}"
                        who = f" [{it.who}]" if it.who else ""
                        note = f"（{it.note}）" if it.note else ""
                        f.write(f"\n[{where}]{who} {note}\n")
                        f.write(f"  原文: {it.original}\n")
                        f.write(f"  译文: {it.translation}\n")
        except OSError as exc:
            if log:
                log(f"补漏查缺：写报告失败: {exc}")
        if log:
            parts = "、".join(
                f"{_CATEGORY_TITLES[c]} {n} 条"
                for c, n in summary.counts.items())
            log(f"补漏查缺：发现 {len(problems)} 条问题（{parts}），"
                f"详情见: {report}")
    else:
        # 干净：清掉旧报告，避免误导
        try:
            if report.exists():
                report.unlink()
        except OSError:
            pass
        if log:
            log(f"补漏查缺：{len(all_items)} 条翻译全部正常，未发现残留问题")

    # 排入重译队列：把问题项原文合并进「未翻译报告」（按文本去重）。
    # 下次「开始汉化」时增量模式只重译报告中列出的文本。
    if problems and queue_for_retranslate:
        untran = tl_dir.parent / f"{lang}.未翻译报告.txt"
        existing: set[str] = set()
        if untran.is_file():
            try:
                for ln in untran.read_text(
                        encoding="utf-8-sig", errors="ignore").splitlines():
                    m = re.match(r"^.+:\d+\s{2,}(.+)$", ln.strip())
                    if m:
                        existing.add(m.group(1).strip())
            except OSError:
                pass
        added = 0
        seen_new: set[str] = set()
        chunk: list[str] = []
        for it in problems:
            text = it.original.strip()
            if not text or "\n" in text or text in existing or text in seen_new:
                continue
            seen_new.add(text)
            chunk.append(f"{it.src_file}:{it.src_line}  {text}")
            added += 1
        if chunk:
            try:
                with open(untran, "a", encoding="utf-8-sig") as f:
                    f.write("\n\n== 补漏查缺发现的问题（自动排入，重译成功后消失）==\n")
                    for line in chunk:
                        f.write("\n" + line)
                summary.queued = added
                if log:
                    log(f"已把 {added} 条问题项排入重译队列"
                        f"（{untran.name}），再次点击「开始汉化」即可补充修正")
            except OSError as exc:
                if log:
                    log(f"补漏查缺：排入重译队列失败: {exc}")
    return summary