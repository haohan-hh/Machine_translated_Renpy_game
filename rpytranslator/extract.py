# -*- coding: utf-8 -*-
"""
.rpy 脚本解析与文本提取。

严格遵循 Ren'Py 官方实现（renpy/ast.py 的 Say.get_code、
renpy/translation/__init__.py 的 Restructurer.create_translate）：

  翻译 ID = label.replace(".", "_") + "_" + md5(code + "\\r\\n").hexdigest()[:8]
  显式 id 优先于自动生成的 ID；code 是 Say.get_code() 的规范化输出。

对话（say）              → translate <lang> <id> 块
菜单选项 / _() 字符串 /
Character 名称 / 屏幕文本 → translate <lang> strings 块
"""
from __future__ import annotations

import ast
import hashlib
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础：与 Ren'Py 一致的代码规范化
# ---------------------------------------------------------------------------

# Script-level statements. A screen-language word such as `text` or `button`
# can be a character's name in script language, even if defined in another
# file or dynamically. Screen, style and ATL bodies are dispatched separately.
NON_WHO_KEYWORDS = {
    "play", "stop", "queue", "show", "hide", "scene", "with", "window",
    "voice", "pause", "call", "jump", "return", "label", "menu", "define",
    "default", "init", "image", "transform", "style", "screen", "translate",
    "python", "if", "elif", "else", "while", "for", "pass", "break",
    "continue", "renpy", "not", "and", "or", "in", "is", "as", "id",
    "camera", "layeredimage", "testcase", "rpy",
}


def encode_say_string(s: str) -> str:
    """对应 renpy.translation.encode_say_string。"""
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    s = re.sub(r"(?<= ) ", "\\ ", s)
    return '"' + s + '"'


# Ren'Py 编译 translate 块时插入的位置标记（unrpyc 反编译后残留），如
# `balto "@@p0@@p这样自我介绍可能不是最好的方式。"`。游戏运行时无害，
# 但会污染译文、干扰增量匹配，解析已有 tl 时统一剥离。
_SAY_POS_MARKER = re.compile(r"@@p\d+@@p")
_SAY_ESCAPE = re.compile(r"\\(u([0-9a-fA-F]{1,4})|.)", re.DOTALL)


def _say_value(raw: str) -> str:
    """Ren'Py collapses whitespace before interpreting escapes in a say literal."""
    def unescape(m):
        if m[2]:
            return chr(int(m[2], 16))
        return {"n": "\n", "{": "{{", "[": "[[", "%": "%%"}.get(m[1], m[1])
    return _SAY_ESCAPE.sub(unescape, re.sub(r"[ \n]+", " ", raw))


def decode_say_string(raw: str) -> str:
    """反向还原 ``encode_say_string``：\\n → 换行、\\" → "、\\ → 空格、\\\\ → \\，
    并剥离 @@pN@@p 编译残留的位置标记。

    解析既有 tl 时复用本函数，与生成器输出格式一致；剥离位置标记可避免
    Ren'Py 编译残留干扰译文增量匹配（同一原文出现两条生成式与以不同形式）。
    """
    return _SAY_POS_MARKER.sub("", _say_value(raw))


def make_say_code(
    who: str | None,
    what: str,
    attributes: tuple[str, ...] | None = None,
    temporary_attributes: tuple[str, ...] | None = None,
    interact: bool = True,
    with_expr: str | None = None,
    explicit_id: str | None = None,
    arguments: str | None = None,
) -> str:
    """对应 renpy.ast.Say.get_code() 的规范化输出。"""
    rv: list[str] = []
    if who:
        rv.append(who)
    if attributes:
        rv.extend(attributes)
    if temporary_attributes:
        rv.append("@")
        rv.extend(temporary_attributes)
    rv.append(encode_say_string(what))
    if not interact:
        rv.append("nointeract")
    if explicit_id:
        rv.append("id")
        rv.append(explicit_id)
    if arguments:
        rv.append(arguments)
    if with_expr:
        rv.append("with")
        rv.append(with_expr)
    return " ".join(rv)


def _code_digest(code: str) -> str:
    md5 = hashlib.md5()
    md5.update((code + "\r\n").encode("utf-8"))
    return md5.hexdigest()[:8]


def make_dialogue_identifier(label: str | None, code: str) -> str:
    """对应 Restructurer.unique_identifier：label 为空时仅用摘要。"""
    digest = _code_digest(code)
    if not label:
        return digest
    return label.replace(".", "_") + "_" + digest


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class DialogueUnit:
    who: str | None
    what: str
    label: str | None
    filename: str
    line: int
    code: str = ""
    identifier: str = ""
    attributes: tuple[str, ...] | None = None
    temporary_attributes: tuple[str, ...] | None = None
    interact: bool = True
    with_expr: str | None = None
    explicit_id: str | None = None
    arguments: str | None = None
    translation: str | None = None
    source_line: str = ""

    def __post_init__(self):
        if not self.code:
            self.code = self.say_code(self.what)
        if not self.identifier:
            self.identifier = self.explicit_id or make_dialogue_identifier(self.label, self.code)

    def say_code(self, what: str) -> str:
        return make_say_code(
            self.who, what, self.attributes, self.temporary_attributes,
            self.interact, self.with_expr, self.explicit_id, self.arguments,
        )


@dataclass
class StringUnit:
    text: str
    filename: str
    line: int
    context: str = ""
    translation: str | None = None


@dataclass
class ExtractionResult:
    dialogues: list[DialogueUnit] = field(default_factory=list)
    strings: list[StringUnit] = field(default_factory=list)
    files: dict[str, list[DialogueUnit]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def dialogues_by_file(self) -> dict[str, list[DialogueUnit]]:
        if self.files:
            return self.files
        files: dict[str, list[DialogueUnit]] = {}
        for d in self.dialogues:
            files.setdefault(d.filename, []).append(d)
        self.files = files
        return files


# ---------------------------------------------------------------------------
# 字符串字面量解析
# ---------------------------------------------------------------------------

_STRING_START = re.compile(r'("""|\'\'\'|"|\')')
# 注意：不能用 (["']{1,3})——贪婪量词会把 "'abc" 开头的 "' 一起吞成
# “引号对”，吃掉紧贴开引号的撇号（如 `T "'Sunny Fruits, ..."`），
# 导致 what 丢首字符、digest 与引擎不一致。三引号必须排在前面的分支。


def _unescape(body: str, quote: str = '"') -> str:
    """对字符串体做转义还原（与 Python/Ren'Py 语义一致）。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.literal_eval(quote + body + quote)
    except (ValueError, SyntaxError):
        # literal_eval 失败时退化为原样返回（保留转义符）
        return body


def _parse_string_literal(s: str, pos: int, *, spoken: bool = False) -> tuple[str | None, int]:
    """从 s[pos:] 解析 Ren'Py 字符串字面量（支持三引号、转义）。
    返回 (字面量的真实值, 结束位置)；失败返回 (None, pos)。"""
    m = _STRING_START.match(s, pos)
    if not m:
        return None, pos
    quote = m.group(1)
    triple = len(quote) == 3
    body_start = pos + len(quote)
    end = _quoted_end(s, pos, quote)
    if end is not None:
        body = s[body_start:end - len(quote)]
        return (_say_value(body) if spoken and not triple else _unescape(body, quote)), end
    # 未闭合：仅对三引号容忍（可能跨行）
    if triple:
        return _unescape(s[body_start:], quote), len(s)
    return None, pos


def _quoted_end(text: str, pos: int, quote: str) -> int | None:
    i = pos + len(quote)
    while i < len(text):
        if text.startswith(quote, i):
            return i + len(quote)
        i += 2 if text[i] == "\\" else 1
    return None


def _expression_delimiter(text: str, start: int, stops: str, *, say_clauses: bool = False) -> int | None:
    """Find a top-level delimiter without splitting Python strings or nested calls."""
    pos = start
    closing = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    while pos < len(text):
        ch = text[pos]
        if not closing:
            if ch in stops:
                return pos
            if say_clauses and ch.isspace() and re.match(r"\s+(?:nointeract|id|with)\b", text[pos:]):
                return pos
        if ch in ('"', "'", "`"):
            quote = ch * 3 if text.startswith(ch * 3, pos) else ch
            end = _quoted_end(text, pos, quote)
            if end is None:
                return None
            pos = end
            continue
        if ch in pairs:
            closing.append(pairs[ch])
        elif ch in ")]}":
            if not closing or ch != closing.pop():
                return None
        pos += 1
    return pos if not closing else None


def _say_arguments(text: str, start: int) -> tuple[str, int] | None:
    """Mirror ArgumentInfo.get_code(): only argument separators and prefixes change.

    In particular, expressions keep their original quoting, interior spacing
    and even whitespace immediately before a comma; never evaluate game code.
    """
    pos, parts = start + 1, []
    while pos < len(text):
        end = _expression_delimiter(text, pos, ",)")
        if end is None or end == len(text):
            return None
        arg = text[pos:end].lstrip()
        if arg:
            prefix = re.match(r"(?:(\*\*?)|([^\W\d]\w*)\s*=(?!=))\s*", arg)
            if prefix:
                arg = (prefix[1] or prefix[2] + "=") + arg[prefix.end():]
            parts.append(arg)
        elif text[end] != ")":
            return None
        if text[end] == ")":
            return "(" + ", ".join(parts) + ")", end + 1
        pos = end + 1
    return None


@dataclass
class _LogicalLine:
    text: str
    line: int       # 起始行号（1 基）


def _logical_lines(raw: str) -> list[_LogicalLine]:
    """按 Ren'Py 的字符串、括号及反斜杠续行规则分句，保留物理行号。"""
    result: list[_LogicalLine] = []
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 1
    for number, physical in enumerate(raw.removeprefix("\ufeff").split("\n"), 1):
        line = physical.removesuffix("\r")
        i, end = 0, len(line)
        continued = False
        while i < end:
            c = line[i]
            if quote:
                if c == "\\":
                    i += 2
                elif line.startswith(quote, i):
                    i += len(quote)
                    quote = ""
                else:
                    i += 1
            elif c in ('"', "'", "`"):
                quote = c * 3 if line.startswith(c * 3, i) else c
                i += len(quote)
            elif c == "#":
                end = i
            elif c == "\\" and i + 1 == end:
                # Keep the newline for source locations, but leave only
                # whitespace between tokens when parsing the joined statement.
                end = i
                continued = True
            else:
                if c in "([{":
                    depth += 1
                elif depth and c in ")]}":
                    depth -= 1
                i += 1
        parts.append(line[:end])
        if not (quote or depth or continued):
            result.append(_LogicalLine("\n".join(parts), start))
            parts.clear()
            start = number + 1
    if parts:
        result.append(_LogicalLine("\n".join(parts), start))
    return result


# ---------------------------------------------------------------------------
# 文件扫描
# ---------------------------------------------------------------------------

_BLOCK = "block"
_PYTHON = "python"
_MENU = "menu"
_SCREEN = "screen"
_TRANSLATE = "translate"

# 屏幕文本控件：text / textbutton / label 后紧跟字符串字面量
_SCREEN_TEXT_RE = re.compile(r'^(text|textbutton|label)\s+(["\'])')
# screen 内属性文本：tooltip / alt / text / title 等（如 imagebutton 的 tooltip "Save"）
_SCREEN_PROP_RE = re.compile(
    r'\b(?:tooltip|alt|text|title|subtitle)\s+(["\'])', re.I)
# define/default 中的 Character 调用
_CHARACTER_RE = re.compile(r'\b\w*Character\s*\(')
_CHARACTER_NAME_ARG = re.compile(r'\s*(?:name\s*=\s*)?(?:_\(\s*)?')
# These Ren'Py settings contain identifiers, layout choices, or a file name,
# even when a game assigns an ordinary quoted string. Real labels can use the
# very same words; exclude only these assignments, never the words themselves.
_NON_TEXT_SETTINGS = frozenset({
    "gui.language", "gui.unscrollable", "config.language", "config.save_directory",
    "config.version", "config.window", "config.window_icon", "build.name",
})
_DEFINE_ASSIGNMENT = re.compile(r'^(?:define|default)\s+([\w.]+)(?:\[[^\]]*\])?\s*(?:=|\+=)\s*')
_IMAGE_EXPRESSION = re.compile(r"^image\s+[\w.]+(?:\s+[\w.]+)*\s*=(?!=)\s*")
# 判断字符串是否值得翻译时，剥离 {标签} / [插值] / {#...} 特殊标记
_STRIP_TAG_RE = re.compile(r"\{[^{}]*\}")
_STRIP_INSERT_RE = re.compile(r"\[[^\[\]]*\]")


def nontext_source_literals(raw: str) -> set[tuple[int, str, bool]]:
    """Identify only source-proven false translation keys from older releases.

    The last field distinguishes a string key (a direct engine setting) from a
    dialogue ID (a conditional displayable's Python condition). A genuine UI
    translation with the same spelling is never disqualified as a dialogue.
    """
    result = set()
    for line in _logical_lines(raw):
        code = line.text.strip()
        definition = _DEFINE_ASSIGNMENT.match(code)
        if definition and definition[1] in _NON_TEXT_SETTINGS:
            pos = definition.end()
            value, end = _parse_string_literal(code, pos)
            if value is not None and not code[end:].strip():
                result.add((line.line + code.count("\n", 0, pos), value, True))
            continue
        assignment = definition or _IMAGE_EXPRESSION.match(code)
        if not assignment or "ConditionSwitch" not in code[assignment.end():]:
            continue
        rhs = code[assignment.end():]
        try:
            expr = ast.parse(rhs, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            continue  # Without a complete expression, the old entry cannot be verified.
        if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                and expr.func.id == "ConditionSwitch" and len(expr.args) % 2 == 0
                and not any(isinstance(arg, ast.Starred) for arg in expr.args)):
            continue
        rhs_line = line.line + code.count("\n", 0, assignment.end())
        # ConditionSwitch alternates condition, displayable, condition,
        # displayable. A Text(_("...")) used as a displayable is real UI.
        for condition in expr.args[::2]:
            if isinstance(condition, ast.Constant) and isinstance(condition.value, str):
                result.add((rhs_line + condition.lineno - 1, condition.value, False))
    return result


@dataclass
class _Ctx:
    kind: str
    indent: int
    menu_indent: int | None = None   # menu 上下文中，选项行的缩进


class RpyExtractor:
    def __init__(self, filename: str | Path):
        self.filename = str(filename)
        self.dialogues: list[DialogueUnit] = []
        self.strings: list[StringUnit] = []
        self.used_ids: set[str] = set()
        self.seen_strings: set[str] = set()
        self._string_units: dict[str, StringUnit] = {}
        self.skipped: list[str] = []
        self._label: str | None = None
        self._alternate: str | None = None
        # Ren'Py 的 label 名解析基准（lexer.set_global_label）：最近一个
        # 非点开头 label 名的第一段（`a.b` → "a"）。`.sub` 子标签永远挂在
        # 这个基准下（`<global>.sub`），而不是链式叠加——见 _resolve_label_name。
        self._global_label: str | None = None
        self._stack: list[_Ctx] = []

    # -- 通用 --------------------------------------------------------------

    def _add_dialogue(self, who, what, line_no, source_line, **kw):
        if who and _STRING_START.match(who):
            display_name, end = _parse_string_literal(who, 0)
            if display_name and end == len(who) and any(c.isalpha() for c in display_name):
                self._add_string(display_name, line_no, context="character")
        unit = DialogueUnit(
            who=who, what=what, label=self._label, filename=self.filename,
            line=line_no, source_line=source_line, **kw,
        )
        unit.identifier = self._unique_id(unit.identifier)
        # 对话同样过滤纯数字/颜色/资源路径等技术性内容（如旁白 `"#000"`），
        # 避免把代码数据当文本送去翻译并在未翻译报告中制造噪音。
        if unit.what and self._is_translatable_text(unit.what):
            self.dialogues.append(unit)

    @staticmethod
    def _is_translatable_text(value: str) -> bool:
        """判断字符串是否值得送去翻译。

        过滤版本号、颜色、纯插值/标签、单字符等无实际翻译意义的内容，
        避免浪费 API 请求并在未翻译报告中产生噪音。
        """
        if not value:
            return False
        v = value.strip()
        if not v or len(v) <= 1:
            return False
        if re.fullmatch(r"\d+(?:\.\d+)*", v):        # 版本号 / 数字
            return False
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", v):    # 颜色
            return False
        # 资源引用：图片 / 字体 / 音频 / 脚本路径（如 gui/...png、DejaVuSans.ttf）
        if re.search(
                r"(?i)(?:^|[/\\])[^/\\\"]*"
                r"\.(?:png|jpe?g|webp|bmp|gif|ogg|wav|mp3|flac|opus|ttf|otf"
                r"|woff|woff2|rpy|rpyc|webm|mp4|txt|json|json5)$", v):
            return False
        plain = _STRIP_TAG_RE.sub("", v)
        plain = _STRIP_INSERT_RE.sub("", plain)
        plain = plain.strip()
        if not plain:                                 # 纯标签/插值，如 [page]
            return False
        return True

    def _add_string(self, text, line_no, context=""):
        if text in self._string_units:
            if context == "character":
                self._string_units[text].context = context
            return
        if not text:
            return
        if not self._is_translatable_text(text):
            return
        self.seen_strings.add(text)
        unit = StringUnit(text=text, filename=self.filename, line=line_no, context=context)
        self._string_units[text] = unit
        self.strings.append(unit)

    def _unique_id(self, identifier: str) -> str:
        base = identifier
        i = 0
        while identifier in self.used_ids:
            i += 1
            identifier = f"{base}_{i}"
        self.used_ids.add(identifier)
        return identifier

    @staticmethod
    def _indent_of(text: str) -> int:
        return len(text) - len(text.lstrip(" \t"))

    # -- 主流程 ------------------------------------------------------------

    def parse(self, raw: str) -> ExtractionResult:
        lines = _logical_lines(raw)
        for ll in lines:
            self._process_line(ll)
        for unit in _underscore_strings(lines, self.filename):
            self._add_string(unit.text, unit.line, context=unit.context)
        return ExtractionResult(
            dialogues=self.dialogues, strings=self.strings,
            files={self.filename: self.dialogues}, skipped=self.skipped,
        )

    def _process_line(self, ll: _LogicalLine):
        text = ll.text
        if not text.strip():
            return
        indent = self._indent_of(text)
        stripped = text.strip()

        # 根据缩进弹出已结束的块
        while self._stack and indent <= self._stack[-1].indent:
            self._stack.pop()

        top = self._stack[-1] if self._stack else None

        # python / translate 块内：忽略语句（_() 字符串另由全局扫描处理）
        if top and top.kind in (_PYTHON, _TRANSLATE):
            return

        # 屏幕内文本控件：text / textbutton / label "文本"
        # （屏幕内没有 say 对话；其余语句一律跳过）
        if top and top.kind == _SCREEN:
            if re.match(r"python\b.*:\s*$", stripped):
                self._stack.append(_Ctx(_PYTHON, indent))
                return
            sm = _SCREEN_TEXT_RE.match(stripped)
            if sm:
                value, _ = _parse_string_literal(stripped, sm.start(2))
                if value is not None:
                    self._add_string(value, ll.line, context="screen")
            # 同时提取 screen 内属性文本：tooltip / alt / text / title 等
            for pm in _SCREEN_PROP_RE.finditer(stripped):
                value, _ = _parse_string_literal(stripped, pm.start(1))
                if value is not None:
                    self._add_string(value, ll.line, context="screen")
            return

        # 语句关键字
        # `init -10 screen ...` uses the same sublanguage as `screen ...`;
        # `init:` on its own contains regular script statements.
        keyword = self._keyword_of(stripped)
        if keyword == "init":
            stripped = re.sub(r"^init(?:\s+[+-]?\d+)?\s+(?=[A-Za-z_])", "", stripped)
            keyword = self._keyword_of(stripped)
        if keyword == "label":
            self._handle_label(stripped)
            return
        if keyword == "call":
            # `call label from name`：from 子句会在返回点创建一个**真实、
            # 非隐藏**的 Label（parser.py:765 `ast.Label(loc, name, [], None)`，
            # Restructurer 据此把 call 之后整段对话的 identifier 前缀切到该
            # 名）。本作 292+132 条运行时缺失的根因就是漏了这一步——"Hey!"
            # 们被挂到 chapter_T2 前缀下，而引擎在 toast_call_... 下找它们。
            m = re.search(r"\bfrom\s+([^\s:()]+)\s*$", stripped)
            if m:
                full = self._resolve_label_name(m.group(1))
                if full and not full.startswith("_"):
                    self._label = full
                    self._alternate = None
            return
        if keyword == "python":
            self._stack.append(_Ctx(_PYTHON, indent))
            return
        if keyword == "translate":
            self._stack.append(_Ctx(_TRANSLATE, indent))
            return
        if keyword == "menu":
            self._handle_label(stripped)  # A named menu also declares a label.
            self._stack.append(_Ctx(_MENU, indent))
            return
        if keyword == "screen":
            self._stack.append(_Ctx(_SCREEN, indent))
            return
        if (keyword in ("style", "transform", "image", "layeredimage", "show", "scene",
                        "camera", "hide", "testcase") and stripped.endswith(":")):
            # Style, ATL, and testcase bodies have their own grammar.
            self._stack.append(_Ctx(_PYTHON, indent))
            return
        if keyword in ("define", "default"):
            self._handle_define(stripped, ll.line)
            return
        if stripped.startswith("$"):
            return  # 单行 python

        # menu 上下文中的裸字符串 = 菜单选项/标题（仅限菜单体缩进）
        menu_item = False
        if top and top.kind == _MENU:
            if top.menu_indent is None:
                top.menu_indent = indent
            menu_item = indent == top.menu_indent
            if menu_item and self._is_bare_string_start(stripped):
                value, end = _parse_string_literal(stripped, 0, spoken=True)
                rest = stripped[end:].lstrip()
                # A quoted speaker followed by speech is a say menuitem.
                if value is not None and (not rest or stripped.endswith(":")):
                    self._add_string(value, ll.line, context="menu")
                    return

        # 尝试解析为对话（say）
        units = self._parse_say(stripped, ll.line)
        if units is None:
            return
        for who, what, kw in units:
            if what == "{clear}":
                continue
            if menu_item and who:
                kw["interact"] = False
            self._add_dialogue(who, what, ll.line, text, **kw)

    @staticmethod
    def _keyword_of(stripped: str) -> str | None:
        m = re.match(r"(\w+)", stripped)
        return m.group(1) if m else None

    def _resolve_label_name(self, name: str) -> str:
        """按 Ren'Py 语义解析 label 名，返回完整名。

        对应 lexer.py 的 label_name()/set_global_label()（游戏自带引擎
        源码实证）：

        - ``a``     → "a"，解析基准变为 "a"
        - ``a.b``   → "a.b"，解析基准变为 "a"（split(".")[0]）
        - ``.sub``  → "<基准>.sub"，**基准不变**——子标签永远挂在最近一个
          非点开头 label 下，绝不嵌套叠加。旧实现把 `.sub1 .sub2` 叠成
          parent.sub1.sub2，导致 identifier 前缀过长而整块失配。
        """
        if name.startswith("."):
            base = self._global_label or ""
            return (base + name) if base else name[1:]
        self._global_label = name.split(".")[0]
        return name

    def _handle_label(self, stripped: str):
        # label name: / label name(参数): / label name(参数) hide: / menu name:
        # （命名 menu 在 Ren'Py 中等价于一个隐式 label）。
        # 必须带冒号结尾（屏幕里的 `label 变量` / `label 变量:` 控件不走
        # 这里——_SCREEN 上下文已提前返回）。
        if not stripped.endswith(":"):
            return
        body = stripped.rstrip(":").strip()
        hide = False
        if body.endswith(" hide"):
            hide = True
            body = body[:-5].rstrip()
        # 名字 = 第一个裸 token；`label name(a, b):` 带参数时只取 name
        m = re.match(r"^(?:label|menu)\s+([^\s(:]+)", body)
        if not m:
            return
        name = m.group(1)
        if not name or name.startswith(('"', "'", "(", "$", "%", "[", "{")):
            return   # 表达式/字符串形式的屏幕 label 控件等，不是脚本 label
        full = self._resolve_label_name(name)
        if hide:
            return   # 隐藏 label 不更新前缀（Restructurer: not i.hide 才更新）
        if full.startswith("_"):
            self._alternate = full
            return
        self._label = full
        self._alternate = None

    @staticmethod
    def _is_bare_string_start(stripped: str) -> bool:
        return bool(_STRING_START.match(stripped))

    def _handle_define(self, stripped: str, line_no: int):
        """提取直接定义的文本和 Character 的显示名称。"""
        m = _DEFINE_ASSIGNMENT.match(stripped)
        if m and m[1] not in _NON_TEXT_SETTINGS:
            value, _ = _parse_string_literal(stripped, m.end())
            if value is not None and self._is_translatable_plain_string(value):
                # xxxVars.name = '...' 通常是角色名，与 Character 名称一样保留原文
                ctx = "character" if re.search(r"\w+Vars\.name\s*=", stripped) else "define"
                self._add_string(value, line_no, context=ctx)
        self._add_character_names(stripped, line_no)

    def _add_character_names(self, stripped: str, line_no: int):
        for cm in _CHARACTER_RE.finditer(stripped):
            # Character(None, color="..."), DynamicCharacter("player.name")
            # have no literal display name. Only consume an actual name argument.
            if "DynamicCharacter" in cm[0]:
                continue
            start = _CHARACTER_NAME_ARG.match(stripped, cm.end()).end()
            value, _ = _parse_string_literal(stripped, start)
            if value is not None and self._is_translatable_plain_string(value):
                self._add_string(value, line_no + stripped.count("\n", 0, start), context="character")

    @staticmethod
    def _is_translatable_plain_string(value: str) -> bool:
        """普通 define/default 中的字符串：排除明显非文本内容。"""
        if not value:
            return False
        v = value.strip()
        if len(v) > 500:
            return False
        if re.match(r"^#[0-9a-fA-F]{3,8}$", v):          # 颜色
            return False
        if re.search(r"(?i)\.(png|jpe?g|gif|webp|bmp|ogg|wav|mp3|flac|opus|ttf|otf|woff|rpy|rpyc|webm|mp4)$", v):
            return False
        # 形如文件路径
        if re.search(r"[\\/]", v) and "." in re.split(r"[\\/]", v)[-1]:
            return False
        if v.isdigit():
            return False
        return True

    # -- 对话解析 ----------------------------------------------------------

    def _parse_say(self, stripped: str, line_no: int):
        """尝试把一行解析为 say 语句。返回 [(who, what, kwargs)] 或 None。"""
        m = re.match(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)", stripped)
        if m:
            who = m.group(1)
            if who not in NON_WHO_KEYWORDS:
                r = self._parse_say_rest(stripped, m.end())
                if r is not None:
                    return r
        # 字符串 who（内联角色名）：`"Cultist" "What's that noise?"`。
        # Ren'Py 允许 who 是任意表达式；这里是字符串字面量，序列化进
        # translate identifier 的 code 时必须带引号原样保留（ast.py:917
        # `rv.append(self.who)` 直接追加原始表达式文本）。
        if stripped[:1] in ('"', "'"):
            lit, end = _parse_string_literal(stripped, 0)
            if lit is not None:
                r = self._parse_say_rest(stripped, end)
                if r is not None:
                    return r
        # 旁白：`"what" ...`
        return self._parse_say_rest(stripped, 0)

    def _parse_say_rest(self, s: str, pos: int):
        """解析 who 之后的：属性/临时属性/字符串/后续标记。"""
        i = pos
        n = len(s)
        attributes: list[str] = []
        temporary: list[str] = []
        who = None

        # 已给出 who 时解析属性（Ren'Py 官方语法：可选 - 前缀的裸词，
        # 如 `Mikko strained "..."`；unrpyc 反编译输出即此格式）
        if pos > 0:
            who = s[:pos].strip()
            while True:
                j = i
                while j < n and s[j].isspace():
                    j += 1
                m = re.match(r"([+-]?[A-Za-z_]\w*)", s[j:])
                if not m:
                    break
                attributes.append(m.group(1))
                i = j + len(m.group(1))
            # 临时属性：@ -a +b
            j = i
            while j < n and s[j].isspace():
                j += 1
            if j < n and s[j] == "@":
                i = j + 1
                while True:
                    j = i
                    while j < n and s[j].isspace():
                        j += 1
                    m = re.match(r"([+-]?[A-Za-z_]\w*)", s[j:])
                    if not m:
                        break
                    temporary.append(m.group(1))
                    i = j + len(m.group(1))

        # 第一个字符串（必需）
        while i < n and s[i].isspace():
            i += 1
        what, i = _parse_string_literal(s, i, spoken=True)
        if what is None:
            return None

        interact = True
        with_expr = None
        explicit_id = None
        arguments = None
        whats = [what]

        # 字典键等代码形式：`"key" : value` / `'key' : value` → 不是 say
        j = i
        while j < n and s[j].isspace():
            j += 1
        if j < n and s[j] == ":":
            return None

        # 解析尾部标记
        while i < n:
            while i < n and s[i].isspace():
                i += 1
            if i >= n:
                break
            if s.startswith("nointeract", i) and (i + 10 == n or s[i + 10].isspace()):
                interact = False
                i += 10
                continue
            m = re.match(r"with\s+", s[i:])
            if m:
                start = i + m.end()
                end = _expression_delimiter(s, start, "", say_clauses=True)
                if end is None or end <= start or with_expr is not None:
                    return None
                with_expr = s[start:end].rstrip()
                i = end
                continue
            m = re.match(r"id\s+([\w]+)", s[i:])
            if m:
                explicit_id = m.group(1)
                i += len(m.group(0))
                continue
            if s[i] == "(":
                parsed = _say_arguments(s, i)
                if parsed is None or arguments is not None:
                    return None
                arguments, i = parsed
                continue
            # 追加字符串（monologue 模式）
            v, ni = _parse_string_literal(s, i, spoken=True)
            if v is not None:
                whats.append(v)
                i = ni
                continue
            return None  # Never turn a partially parsed instruction into a simpler say.

        kwargs = dict(
            attributes=tuple(attributes) if attributes else None,
            temporary_attributes=tuple(temporary) if temporary else None,
            interact=interact,
            with_expr=with_expr,
            explicit_id=explicit_id,
            arguments=arguments,
        )
        return [(who, w, kwargs) for w in whats]


# ---------------------------------------------------------------------------
# _() 字符串全局扫描
# ---------------------------------------------------------------------------

_CALL_OR_QUOTED = re.compile(r'''\b(?P<marker>_p|___|__|_)\s*\(\s*|["'`]''')


def _paragraph_text(text: str) -> str:
    """Return the key Ren'Py's _p registers after joining wrapped paragraphs."""
    lines = [line.strip() for line in text.split("\n")]
    if lines and not lines[0]:
        lines.pop(0)
    if lines and not lines[-1]:
        lines.pop()
    parts, paragraph = [], []
    for line in lines:
        if line:
            paragraph.append(line)
            if not re.search(r"\{p[^}]*\}$", line):
                continue
        parts.append(" ".join(paragraph) + ("" if line else "\n\n"))
        paragraph.clear()
    return "".join(parts) + " ".join(paragraph)


def scan_underscore_strings(text: str, filename: str) -> list[StringUnit]:
    """扫描 _()/__()/___()/_p() 字符串调用（对应 Ren'Py STRING_RE）。
    基于逻辑行扫描，支持跨行三引号字符串。"""
    return _underscore_strings(_logical_lines(text), filename)


def _underscore_strings(lines: list[_LogicalLine], filename: str) -> list[StringUnit]:
    result: list[StringUnit] = []
    seen: set[str] = set()
    for ll in lines:
        if "_" not in ll.text:
            continue
        i = 0
        while m := _CALL_OR_QUOTED.search(ll.text, i):
            if not m["marker"]:
                quote = m[0] * 3 if ll.text.startswith(m[0] * 3, m.start()) else m[0]
                i = _quoted_end(ll.text, m.start(), quote) or len(ll.text)
                continue
            i = m.end()
            parts = []
            while i < len(ll.text):
                if ll.text[i] in "uU" and i + 1 < len(ll.text) and ll.text[i + 1] in "\"'":
                    i += 1
                part, end = _parse_string_literal(ll.text, i)
                if part is None:
                    break
                parts.append(part)
                i = end
                while i < len(ll.text) and ll.text[i].isspace():
                    i += 1
            if not parts or ll.text[i:i + 1] != ")":
                continue  # A computed expression has no single static translation key.
            i += 1
            value = "".join(parts)
            if m["marker"] == "_p":
                value = _paragraph_text(value)
            if value not in seen and RpyExtractor._is_translatable_text(value):
                seen.add(value)
                result.append(StringUnit(text=value, filename=filename,
                                         line=ll.line + ll.text.count("\n", 0, m.start()), context="_()"))
    return result


# ---------------------------------------------------------------------------
# 文件级入口
# ---------------------------------------------------------------------------

def extract_rpy_file(path: str | Path) -> ExtractionResult:
    """解析单个 .rpy 文件，返回提取结果（含 _() 字符串）。"""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        return ExtractionResult(skipped=[f"无法读取 {p}: {e}"])

    return RpyExtractor(p).parse(raw)


def extract_rpy_files(files: list[Path]) -> ExtractionResult:
    """批量解析多个 .rpy 文件。"""
    all_dialogues: list[DialogueUnit] = []
    all_strings: list[StringUnit] = []
    skipped: list[str] = []
    by_file: dict[str, list[DialogueUnit]] = {}
    seen_strings: dict[str, StringUnit] = {}
    used_ids: set[str] = set()

    for f in files:
        r = extract_rpy_file(f)
        skipped.extend(r.skipped)
        for d in r.dialogues:
            base = d.identifier
            ident = base
            k = 0
            while ident in used_ids:
                k += 1
                ident = f"{base}_{k}"
            used_ids.add(ident)
            d.identifier = ident
            all_dialogues.append(d)
            by_file.setdefault(d.filename, []).append(d)
        for u in r.strings:
            if u.text in seen_strings:
                if u.context == "character":
                    seen_strings[u.text].context = "character"
                continue
            seen_strings[u.text] = u
            all_strings.append(u)

    return ExtractionResult(
        dialogues=all_dialogues, strings=all_strings,
        files=by_file, skipped=skipped,
    )
