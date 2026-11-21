# -*- coding: utf-8 -*-
"""补漏查缺：扫描已生成的 tl/<语言>/ 翻译文件，分类检测残留问题。

审校问题（报告中的中文类别名）：

1. ``untranslated`` 未翻译残留：译文与原文相同，且原文含成串英文单词。
2. ``mixed`` 中英参半：译文含目标语言文字，同时残留成段英文（连续 ≥3 个
   英文单词）。单个英文单词多为有意保留的人名 / 术语（H.A.A.L.O、Mike），
   不算问题，避免误报。
3. ``wrong_lang`` 目标语言不正确：译文不含目标语言文字，却含其他语言
   文字（日文假名 / 韩文 / 西里尔 / 泰文 / 希腊文），或整句仍是英文
   且与原文不同（模型把原文「英文改写」了一遍而没有翻译）。
4. ``traditional`` 繁体残留（简体中文目标）：译文含繁体字形。这类问题
   **不需要重译**——繁→简是一一对应的确定性替换，直接在本地把 tl 文件的
   译文行改写为简体（fix_traditional=True，默认开启），零 API 成本。
5. ``invalid_markup`` 把对白误写成了花括号标签，可能导致游戏报错。
6. ``missing_name`` 已定义的角色专名或用户指定的保留词丢失。

检测结果写入 ``tl/<语言>.补漏查缺报告.txt`` 供人工逐条核查（含
文件:行号 定位）；除 ``traditional``（已本地修复）外，问题项原文会合并进
「未翻译报告」，下次运行「开始汉化」时增量模式会自动重译这些文本——
这就是「补充修正」的闭环。

解析格式与 generator.py 的输出逐字节对称（encode_say_string 的反转义）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .extract import DialogueUnit, StringUnit, nontext_source_literals
from .glossary import read_name_groups, required_game_names
from .reports import AUDIT_EXPORT_SECTION, _escape_report_text, read_retry_sections, sync_audit_queue
from .tl_files import TLEntry, TLFile, TranslationMergeError, index_tl_directory, read_tl_file, write_if_unchanged
from .translator import (protect_names, protect_text, restore_names, restore_text,
                         target_language_name, unprotected_text, untranslated_first_person,
                         valid_unchanged_text)
from .zhconv import has_simplified_forms, traditional_chars, to_simplified

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

# Han-only Japanese menu labels such as 設定 are legitimate. Audit installed
# text conservatively: script alone cannot tell which language wrote them.
_EXPECTED_SCRIPTS: dict[str, tuple[re.Pattern, ...]] = {
    "Simplified Chinese": (_CJK_RE,),
    "Traditional Chinese": (_CJK_RE,),
    "Japanese": (_KANA_RE, _CJK_RE),
    "Korean": (_HANGUL_RE, _CJK_RE),
    "English": (_ASCII_WORD_RE,),
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
# 界面字符串的单词级可翻译判定（"SAVE"/"LOAD" 单词也必须翻译）
_UI_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# 3 个及以上连续英文单词（"the door is locked"）→ 疑似整句漏译
_WORD_RUN3_RE = re.compile(r"[A-Za-z]{2,}(?:[\s'\u2019\-]+[A-Za-z]{2,}){2,}")
_RETRY_CATEGORIES = frozenset(("untranslated", "mixed", "wrong_lang", "invalid_markup", "missing_name"))


@dataclass
class AuditItem:
    """一个翻译条目及其判定结果（category 为空表示无问题）。"""
    tl_file: str          # tl 文件名（相对 tl/<语言>/）
    src_file: str         # 源文件（注释里的位置标记）
    src_line: int
    who: str = ""
    original: str = ""
    translation: str = ""
    category: str = ""    # 报告中的问题类别；空 = 正常
    note: str = ""        # 判定依据（报告里展示）
    tl_line: int = 0      # 译文行在 tl 文件里的 1 基行号（traditional 本地修复用）
    is_string: bool = False   # True = translate strings 块（界面文本）
    entry: TLEntry | None = field(default=None, repr=False)


@dataclass
class AuditSummary:
    total: int = 0
    counts: dict = field(default_factory=dict)   # category -> 条数
    report_path: str = ""
    queued: int = 0                                # 合并进未翻译报告的条数
    fixed_traditional: int = 0                     # 已本地转换为简体的条数
    read_only_count: int = 0                       # 需要提供对应翻译源码才能修改
    fixed_files: list[Path] = field(default_factory=list)


_CONTROL_HEADERS = {
    "zz_language_display": "# 语言显示名（汉化工具自动生成）",
    "zz_language_switch_probe": "# 语言切换探针（汉化工具自动生成）",
}


def _is_tool_control(doc: TLFile) -> bool:
    stem = re.sub(r"(?:_rpytranslator)+$", "", doc.path.stem)
    return bool(doc.lines and doc.lines[0].strip() == _CONTROL_HEADERS.get(stem))


_KEYCAP = re.compile(r"(?:(?:Ctrl|Alt|Shift|Meta|Super|Cmd)\+)*(?:[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])"
                     r"|Ctrl|Alt|Shift|Tab|Enter|Return|Space|Esc|Escape|Backspace|Delete|Home|End|Page Up|Page Down)")
_SCREEN_HEADER = re.compile(r"^screen\s+(\w+)\b")
_KEY_LABEL = re.compile(r'''\s+label\s+(?:_\(\s*)?(["'])([^"']+)\1\s*\)?\s*:?(?:\s*#.*)?$''')


def _keyboard_labels(source: str) -> set[str]:
    """A displayed physical key in Ren'Py's keyboard help can remain unchanged.

    A strings key is shared throughout the game. A literal seen in a different
    context (e.g. an Enter button in another screen in this file) is ambiguous.
    Shipped translations often refer to an older source line, so use the label
    and its screen instead of assuming line numbers still coincide.
    """
    if "keyboard_help" not in source:
        return set()
    labels, other = set(), set()
    in_help = False
    for line in source.splitlines():
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        if not line[0].isspace():
            header = _SCREEN_HEADER.match(code)
            in_help = bool(header and header[1] == "keyboard_help")
        if in_help and (match := _KEY_LABEL.fullmatch(line)) and _KEYCAP.fullmatch(match[2]):
            labels.add(match[2])
        else:
            other.update(v for _, v in re.findall(r'''(["'])([^"'\n]+)\1''', line) if _KEYCAP.fullmatch(v))
    return labels - other


class _NonTextSources:
    """Confirm historical false positives against today's loose game source."""

    def __init__(self, game: Path):
        self.game = game.resolve()
        self.cache: dict[str, set[tuple[int, str, bool]]] = {}
        self.keyboard: dict[str, set[str]] = {}

    def unchanged_keyboard_label(self, entry: TLEntry) -> bool:
        return (entry.identifier is None and entry.original == entry.translation
                and entry.original in self.keyboard.get(entry.src_file, ()))

    def contains(self, entry: TLEntry) -> bool:
        if not entry.src_file or not entry.src_line or not entry.original:
            return False
        if entry.src_file not in self.cache:
            matches = set()
            try:
                path = Path(entry.src_file)
                if path.parts[:1] == ("game",):
                    path = Path(*path.parts[1:])
                path = (self.game / path).resolve()
                if (path.suffix.lower() in (".rpy", ".rpym") and path.is_relative_to(self.game)
                        and path.relative_to(self.game).parts[0].lower() != "tl" and path.is_file()):
                    text = path.read_text(encoding="utf-8-sig")
                    matches = nontext_source_literals(text)
                    self.keyboard[entry.src_file] = _keyboard_labels(text)
            except (OSError, ValueError, RuntimeError):
                pass  # Missing, moved, compiled-only, or unverifiable: keep the translation visible.
            self.cache[entry.src_file] = matches
        if self.unchanged_keyboard_label(entry):
            return True
        return (entry.src_line, entry.original, entry.identifier is None) in self.cache[entry.src_file]


@dataclass
class RetryPlan:
    entries: dict[str, str]
    manual_texts: set[str]
    dialogues: dict[str, DialogueUnit] = field(default_factory=dict)
    strings: dict[str, StringUnit] = field(default_factory=dict)


def plan_retry_queue(tl_dir: Path, language: str, queue_path: Path,
                     dialogues: list[DialogueUnit], strings: list[StringUnit],
                     protected_terms=(), *, required_terms=None,
                     docs: dict[Path, TLFile] | None = None) -> RetryPlan:
    """Resolve queued text to the individual definitions which still need repair.

    A manual import requests translation explicitly. An automatically queued
    item may have been repaired by the player or have a good translation in
    another scene. An installed game can also use common UI strings which do
    not appear in its own sources.
    """
    entries, manual, failed = read_retry_sections(queue_path)
    plan = RetryPlan(entries, manual)
    if not entries:
        return plan
    if docs is None:
        docs = index_tl_directory(tl_dir, language)
    by_id = {unit.identifier: unit for unit in dialogues}
    by_text = {unit.text: unit for unit in strings}
    nontext = _NonTextSources(tl_dir.parent.parent)
    active = {(e.identifier, e.original if not e.identifier else None)
              for doc in docs.values() for e in doc.entries}
    for doc in docs.values():
        if _is_tool_control(doc):
            continue
        candidates = doc.reference_entries if doc.read_only else doc.entries
        for entry in candidates:
            if entry.span is None or entry.translation is None:
                continue
            unit = by_id.get(entry.identifier) if entry.identifier else by_text.get(entry.original)
            if doc.read_only and (unit is None or
                    (entry.identifier, entry.original if not entry.identifier else None) in active):
                continue
            original = unit.what if isinstance(unit, DialogueUnit) else entry.original
            if original is None or original not in entries:
                continue
            if nontext.contains(entry):
                # A keyboard-help string *is* still extracted, and may come
                # from a failed request in an older version. Respect deliberate
                # manual requests, but do not waste another request on the key.
                if nontext.unchanged_keyboard_label(entry):
                    if original not in manual:
                        continue
                elif unit is None:
                    continue
            if original not in manual and not (original in failed and original == entry.translation):
                check = AuditItem(doc.path.name, entry.src_file, entry.src_line,
                                  original=original, translation=entry.translation,
                                  is_string=entry.identifier is None)
                if classify(check, language, protected_terms, required_terms=required_terms) not in _RETRY_CATEGORIES:
                    continue
            if entry.identifier:
                plan.dialogues[entry.identifier] = unit or DialogueUnit(
                    entry.who, original, None, entry.src_file, entry.src_line, identifier=entry.identifier)
            else:
                plan.strings[original] = unit or StringUnit(original, entry.src_file, entry.src_line)
    return plan


# ---------------------------------------------------------------------------
# tl 文件解析（与 generator.py 的输出对称）
# ---------------------------------------------------------------------------

def _strip_markup(text: str, protected_terms=()) -> str:
    """去掉 Ren'Py 标签 {…} 与插值 […]，只留要给玩家看的字面文本。"""
    return unprotected_text(text, protected_terms)


def _audit_items(doc: TLFile) -> list[AuditItem]:
    return [AuditItem(
        tl_file=doc.path.name, src_file=entry.src_file, src_line=entry.src_line,
        who=entry.who, original=entry.original, translation=entry.translation,
        tl_line=entry.line + 1, is_string=entry.identifier is None, entry=entry,
    ) for entry in doc.entries if entry.original is not None and entry.translation is not None]


def parse_tl_file(path: Path, language: str | None = None) -> list[AuditItem]:
    return _audit_items(read_tl_file(path, language))


# ---------------------------------------------------------------------------
# 分类判定
# ---------------------------------------------------------------------------

def _sentence_tag(source_tags, translated_tags) -> str:
    """Catch new sentences read as Ren'Py tags without assuming a fixed list of custom tags."""
    for tag in translated_tags:
        if tag in source_tags or not tag.startswith("{") or tag == "{{":
            continue
        if tag == "{":
            return tag
        # Names may be Unicode, prefixed with `#` (invisible disambiguation),
        # or followed by :axis / =value. Punctuation and spaces in the value
        # are fine. Never judge based on the contents of a legitimate argument.
        body = tag[1:-1]
        if body.startswith("#"):
            continue
        key = body.lstrip("/").split("=", 1)[0].split(":", 1)[0]
        if key and re.search(r"[\s!?.,，。！？；;…]", key):
            return tag
    return ""


def classify(item: AuditItem, language: str, protected_terms=(), *, required_terms=None) -> str | None:
    """检查玩家可见的译文，包括原文本身已部分翻译的条目。"""
    target = target_language_name(language)
    orig, trans = item.original, item.translation
    if not trans.strip() and orig.strip():
        return "untranslated"
    if orig != trans:
        src_visible, src_tags = protect_text(orig)
        dst_visible, dst_tags = protect_text(trans)
        if tag := _sentence_tag(set(src_tags.values()), dst_tags.values()):
            item.note = f"译文新增的花括号会被当成文本标签：{tag}"
            return "invalid_markup"
        names = protected_terms if required_terms is None else required_terms
        if names:
            _, src_names = protect_names(src_visible, names)
            if src_names:
                _, dst_names = protect_names(dst_visible, names)
                missing = Counter(src_names.values()) - Counter(dst_names.values())
                if missing:
                    item.note = "未保留原拼写或次数：" + "、".join(
                        f"{key} ×{count}" if count > 1 else key for key, count in missing.items())
                    return "missing_name"
    orig_plain = _strip_markup(orig, protected_terms).strip()
    if not orig_plain:
        return None
    expected = _EXPECTED_SCRIPTS.get(target)
    if expected is None:
        return None
    trans_plain = orig_plain if trans == orig else _strip_markup(trans, protected_terms).strip()
    same = trans_plain == orig_plain
    if untranslated_first_person(orig_plain, trans_plain, target):
        item.note = "第一人称代词 I 残留在中文译文中"
        return "mixed"
    for name, pattern in _OTHER_SCRIPTS:
        if pattern not in expected and pattern.search(trans_plain):
            item.note = f"含{name}"
            return "untranslated" if same else "wrong_lang"

    if target != "English":
        has_expected = any(pattern.search(trans_plain) for pattern in expected)
        if has_expected and _WORD_RUN3_RE.search(trans_plain):
            item.note = "残留成段英文"
            return "mixed"
        if not has_expected:
            # A single untranslated menu command is actionable. In dialogue
            # require two words so a short name or key does not enter the queue.
            pattern = _UI_WORD_RE if same and item.is_string else _WORD_RUN2_RE
            if pattern.search(trans_plain):
                item.note = "仍含英文"
                return "untranslated" if same else "wrong_lang"
    if target == "Simplified Chinese":
        tch = traditional_chars(trans_plain)
        if tch:
            item.note = "含繁体字: " + "".join(tch[:12]) + (
                "…" if len(tch) > 12 else "")
            return "traditional"
    elif target == "Traditional Chinese" and has_simplified_forms(trans_plain):
        item.note = "含简体字形"
        return "wrong_lang"
    return None


def reusable_dialogue(source: str, candidate: str, language: str, protected_terms=()) -> bool:
    """Check a rendering before borrowing it for a different dialogue ID.

    Do not apply this to the original owner. Its hand-edited wording and custom
    tags belong to that scene. Every source control (including variables and
    waits) must survive in order; valid extra styling in a hand translation is
    allowed, but extra substitutions or a sentence masquerading as a tag are not.
    """
    if source == candidate:
        return valid_unchanged_text(source, language, protected_terms)
    check = AuditItem("", "", 0, original=source, translation=candidate)
    if classify(check, language, protected_terms) is not None:
        return False
    controls = list(protect_text(source)[1].values())
    matched = 0
    for control in protect_text(candidate)[1].values():
        if matched < len(controls) and control == controls[matched]:
            matched += 1
        elif not (control.startswith("{") and control.endswith("}")) or control in controls:
            return False
    return matched == len(controls)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

_CATEGORY_TITLES = {
    "untranslated": "未翻译残留（译文为空或与原文相同）",
    "mixed": "中英参半（译文残留英文片段或代词）",
    "wrong_lang": "目标语言不正确（含其他语言 / 整句英文）",
    "traditional": "繁体残留",
    "invalid_markup": "疑似无效的 Ren'Py 文本标签",
    "missing_name": "未保留角色专名或指定术语",
}


def _fix_traditional_lines(items: list[AuditItem],
                           item_docs: dict[int, TLFile], protected_terms, log) -> dict[Path, int]:
    """把繁体译文行就地改写为简体（繁→简一一对应，无需重译）。

    只改 ``new "…"`` / 译文行的引号内容；``old`` 原文行绝不碰——原文是
    游戏文本的匹配键，动了会导致 Ren'Py 对不上号而整块失效。
    返回实际写入的文件及其改写条数。
    """
    by_file: dict[Path, list[AuditItem]] = {}
    for it in items:
        doc = item_docs[id(it)]
        if not doc.read_only and it.entry is not None and it.entry.span is not None:
            by_file.setdefault(doc.path, []).append(it)

    fixed = {}
    for path, group in by_file.items():
        doc = item_docs[id(group[0])]
        lines = doc.lines.copy()
        touched = []
        for it in group:
            text, markup = protect_text(it.translation)
            text, names = protect_names(text, protected_terms)
            simplified = restore_text(restore_names(to_simplified(text), names), markup)
            if simplified != it.translation:
                idx = it.entry.line
                lines[idx] = it.entry.with_translation(lines[idx], simplified)
                touched.append((it, simplified))
        if not touched:
            continue
        try:
            write_if_unchanged(path, "".join(lines), doc)
        except (OSError, TranslationMergeError) as exc:
            if log:
                log(f"补漏查缺：未写回 {path.name}: {exc}")
            continue
        fixed[path] = len(touched)
        for it, simplified in touched:
            it.translation = simplified
            it.note = "已自动转换为简体"
    return fixed


def _resolve_game_dir(game_path: str | Path) -> Path:
    p = Path(game_path)
    if (p / "game").is_dir():
        return p / "game"
    return p


def run_audit(game_path: str | Path, language: str, log=None,
              queue_for_retranslate: bool = True,
              fix_traditional: bool = True, protected_terms=()) -> AuditSummary:
    """扫描 tl/<语言>/ 全部翻译文件，写报告并把问题项排入重译队列。

    ``fix_traditional``（默认开）：简体中文目标下检出繁体残留时，直接在
    本地把 tl 文件译文行改写为简体——确定性替换，零 API 成本，改完不进
    重译队列（其他三类问题才需要重译）。
    """
    summary = AuditSummary()
    game_dir = _resolve_game_dir(game_path)
    lang = language.split("（")[0].split("(")[0].strip()
    if not lang.isidentifier() or lang.lower() == "none":
        raise ValueError(f"目标语言标识无效：{lang}")
    glossary = read_name_groups(game_dir, lang)
    explicit = tuple(protected_terms or ())
    required = required_game_names(glossary.get("characters", ()), explicit)
    protected_terms = tuple(dict.fromkeys([*explicit, *(n for group in glossary.values() for n in group)]))
    tl_dir = game_dir / "tl" / lang
    docs = index_tl_directory(tl_dir, lang)
    if not docs and not tl_dir.is_dir():
        if log:
            log(f"补漏查缺：未找到 {tl_dir}，跳过（先完成一次翻译）")
        return summary

    all_items: list[AuditItem] = []
    item_docs: dict[int, TLFile] = {}
    for path, doc in docs.items():
        if _is_tool_control(doc):
            continue
        for it in _audit_items(doc):
            it.tl_file = doc.origin or str(path.relative_to(tl_dir))
            item_docs[id(it)] = doc
            all_items.append(it)

    problems: list[AuditItem] = []
    nontext = _NonTextSources(game_dir)
    for it in all_items:
        cat = classify(it, lang, protected_terms, required_terms=required)
        if cat and not nontext.contains(it.entry):
            it.category = cat
            if item_docs[id(it)].read_only or it.entry.span is None:
                summary.read_only_count += 1
                it.note = (it.note + "；" if it.note else "") + "只读译文，需提供对应 .rpy 源文件"
            problems.append(it)

    # 繁体残留 → 本地修复（把译文行改写为简体）。来自模型输出质量，
    # 但修复与模型无关：繁→简一一对应，直接替换 tl 文件即可。
    if fix_traditional and target_language_name(lang) == "Simplified Chinese":
        trad_items = [it for it in problems if it.category == "traditional"]
        if trad_items:
            fixed = _fix_traditional_lines(trad_items, item_docs, protected_terms, log)
            summary.fixed_files = list(fixed)
            summary.fixed_traditional = sum(fixed.values())
            if log:
                log(f"补漏查缺：已把 {summary.fixed_traditional} 条繁体译文本地转换为简体"
                    f"（无需重译）")

    summary.total = len(all_items)
    for it in problems:
        summary.counts[it.category] = summary.counts.get(it.category, 0) + 1

    actionable = {}
    for it in problems:
        if it.category != "traditional" and not item_docs[id(it)].read_only and it.entry.span is not None:
            actionable.setdefault(it.original, f"{it.src_file}:{it.src_line}")

    report = tl_dir.parent / f"{lang}.补漏查缺报告.txt"
    summary.report_path = str(report)

    if problems:
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            with open(report, "w", encoding="utf-8-sig") as f:
                f.write("补漏查缺报告（翻译完成后自动扫描）\n")
                f.write(f"扫描时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write(f"扫描范围: tl/{lang} 的松散及归档脚本，共 {len(all_items)} 条翻译\n")
                if summary.fixed_traditional:
                    f.write(f"繁体残留: {summary.fixed_traditional} 条已本地转换为简体（不占重译名额）\n")
                if summary.read_only_count:
                    f.write(f"其中 {summary.read_only_count} 条位于只读脚本，提供对应 .rpy 译文源码后可修正\n")
                queued_note = ("可编辑的未翻译或错译原文将排入重译队列"
                               if queue_for_retranslate else "未排入重译队列")
                f.write(f"发现问题: {len(problems)} 条（{queued_note}）\n")
                for cat, title in _CATEGORY_TITLES.items():
                    group = [it for it in problems if it.category == cat]
                    if not group:
                        continue
                    f.write(f"\n== {title}（{len(group)} 条）==\n")
                    for it in group:
                        where = f"{it.src_file}:{it.src_line}"
                        who = f" [{it.who}]" if it.who else ""
                        note = f"（{it.note}）" if it.note else ""
                        f.write(f"\n[{where}]{who} {note}\n  位置: {it.tl_file}:{it.tl_line}\n")
                        f.write(f"  原文: {it.original}\n")
                        f.write(f"  译文: {it.translation}\n")
                # Escape multiline source just once, so importing this report
                # cannot select read-only controls or a locally repaired entry.
                f.write("\n" + AUDIT_EXPORT_SECTION + "\n")
                for text, loc in actionable.items():
                    f.write(f"{loc}  {_escape_report_text(text)}\n")
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

    if queue_for_retranslate:
        untran = tl_dir.parent / f"{lang}.未翻译报告.txt"
        summary.queued = sync_audit_queue(untran, actionable)
        if log and summary.queued:
            log(f"已把 {summary.queued} 条问题项排入重译队列"
                f"（{untran.name}），再次点击「开始汉化」即可补充修正")
    return summary
