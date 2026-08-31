# -*- coding: utf-8 -*-
"""
.rpy 脚本解析与文本提取。

严格遵循 Ren'Py 官方实现（renpy/ast.py 的 Say.get_code、
renpy/translation/__init__.py 的 Restructurer.create_translate）：

  翻译 ID = label.replace(".", "_") + "_" + md5(code + "\\r\\n").hexdigest()[:8]
  其中 code = Say.get_code() 的规范化输出（who 可选 + encode_say_string(what)）

对话（say）              → translate <lang> <id> 块
菜单选项 / _() 字符串 /
Character 名称 / 屏幕文本 → translate <lang> strings 块
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础：与 Ren'Py 一致的代码规范化
# ---------------------------------------------------------------------------

# 不能作为说话人标识符的 Ren'Py 语句关键字
NON_WHO_KEYWORDS = {
    "play", "stop", "queue", "show", "hide", "scene", "with", "window",
    "voice", "pause", "call", "jump", "return", "label", "menu", "define",
    "default", "init", "image", "transform", "style", "screen", "translate",
    "python", "if", "elif", "else", "while", "for", "pass", "break",
    "continue", "renpy", "not", "and", "or", "in", "is", "id",
    # 屏幕/样式语言关键字与属性（`thumb "..."` 等不能被当作对话）
    "add", "text", "textbutton", "imagebutton", "button", "bar", "vbar",
    "hbar", "frame", "window", "fixed", "hbox", "vbox", "grid", "viewport",
    "use", "key", "input", "style_prefix", "size_group", "properties",
    "tile", "side", "scrollbar", "vscrollbar", "slider", "vslider", "label",
}


def encode_say_string(s: str) -> str:
    """对应 renpy.translation.encode_say_string。"""
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    s = re.sub(r"(?<= ) ", "\\ ", s)
    return '"' + s + '"'


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
            self.code = make_say_code(
                self.who, self.what, self.attributes, self.temporary_attributes,
                self.interact, self.with_expr, self.explicit_id, self.arguments,
            )
        if not self.identifier:
            self.identifier = make_dialogue_identifier(self.label, self.code)


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

_STRING_START = re.compile(r'(["\']{1,3})')


def _unescape(body: str, quote: str = '"') -> str:
    """对字符串体做转义还原（与 Python/Ren'Py 语义一致）。"""
    try:
        return ast.literal_eval(quote + body + quote)
    except Exception:
        # literal_eval 失败时退化为原样返回（保留转义符）
        return body


def _parse_string_literal(s: str, pos: int) -> tuple[str | None, int]:
    """从 s[pos:] 解析 Ren'Py 字符串字面量（支持三引号、转义）。
    返回 (字面量的真实值, 结束位置)；失败返回 (None, pos)。"""
    m = _STRING_START.match(s, pos)
    if not m:
        return None, pos
    quote = m.group(1)
    triple = len(quote) == 3
    body_start = pos + len(quote)
    i = body_start
    n = len(s)
    while i < n:
        if triple and s.startswith(quote, i):
            body = s[body_start:i]
            return _unescape(body, quote), i + 3
        if not triple and s[i] == quote[0]:
            body = s[body_start:i]
            return _unescape(body, quote), i + 1
        if s[i] == "\\":
            i += 2
            continue
        i += 1
    # 未闭合：仅对三引号容忍（可能跨行）
    if triple:
        return _unescape(s[body_start:], quote), n
    return None, pos


def strip_comment(line: str) -> str:
    """去掉行内注释（# 后为注释），保留字符串内的 #。"""
    in_str: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if line.startswith(in_str * 3, i):
                i += 3
                in_str = None
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            if line.startswith(ch * 3, i):
                in_str = ch
                i += 3
            else:
                in_str = ch
                i += 1
            continue
        if ch == "#":
            return line[:i]
        i += 1
    return line


def _opens_triple(code: str) -> str | None:
    """返回本行是否开启了未闭合的三引号字符串及其引号字符。"""
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ('"', "'") and code.startswith(ch * 3, i):
            rest = code[i + 3:]
            if ch * 3 in rest:
                i = rest.index(ch * 3) + i + 6
                continue
            return ch
        i += 1
    return None


@dataclass
class _LogicalLine:
    text: str
    line: int       # 起始行号（1 基）


def _logical_lines(raw: str) -> list[_LogicalLine]:
    """把物理行合并为逻辑行：未闭合的三引号字符串会吸收后续行。
    同时去除注释（跨行字符串内的 # 除外）。"""
    lines = raw.replace("\ufeff", "").split("\n")
    result: list[_LogicalLine] = []
    buf: list[str] = []
    start_line = 0
    open_quote: str | None = None

    def flush():
        nonlocal buf
        if buf:
            result.append(_LogicalLine("\n".join(buf), start_line))
            buf = []

    for idx, line in enumerate(lines, 1):
        if open_quote:
            # 跨行字符串内部：保留原文（不做注释剥离）
            buf.append(line)
            if open_quote * 3 in line:
                open_quote = None
            continue

        code = strip_comment(line)
        op = _opens_triple(code)
        if op:
            if not buf:
                start_line = idx
            buf.append(code)
            open_quote = op
        else:
            if buf:
                buf.append(code)
                open_quote = None
                flush()
            else:
                result.append(_LogicalLine(code, idx))
    if buf:
        flush()
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
# define/default 中的 Character 调用
_CHARACTER_RE = re.compile(r'\b\w*Character\s*\(')


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
        self.skipped: list[str] = []
        self._label: str | None = None
        self._alternate: str | None = None
        self._stack: list[_Ctx] = []
        self._open_define_parens = 0   # 跨行 define 未闭合的括号数

    # -- 通用 --------------------------------------------------------------

    def _add_dialogue(self, who, what, line_no, source_line, **kw):
        unit = DialogueUnit(
            who=who, what=what, label=self._label, filename=self.filename,
            line=line_no, source_line=source_line, **kw,
        )
        unit.identifier = self._unique_id(unit.identifier)
        if unit.what:
            self.dialogues.append(unit)

    def _add_string(self, text, line_no, context=""):
        if not text or text in self.seen_strings:
            return
        self.seen_strings.add(text)
        self.strings.append(StringUnit(text=text, filename=self.filename,
                                       line=line_no, context=context))

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

    @staticmethod
    def _paren_net(text: str) -> int:
        """统计一行中未闭合的 ( ) 数（忽略字符串与注释内的括号）。"""
        depth = 0
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch in ('"', "'"):
                quote = ch
                triple = text.startswith(quote * 3, i)
                if triple:
                    end = text.find(quote * 3, i + 3)
                    i = n if end == -1 else end + 3
                else:
                    i += 1
                    while i < n:
                        if text[i] == "\\":
                            i += 2
                            continue
                        if text[i] == quote:
                            break
                        i += 1
                    i += 1
                continue
            if ch == "#":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        return depth

    # -- 主流程 ------------------------------------------------------------

    def parse(self, raw: str) -> ExtractionResult:
        for ll in _logical_lines(raw):
            self._process_line(ll)
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

        # 跨行 define 的延续行（ConditionSwitch / 字典 / 列表参数等），忽略
        if self._open_define_parens:
            self._open_define_parens += self._paren_net(text)
            return

        # python / translate 块内：忽略语句（_() 字符串另由全局扫描处理）
        if top and top.kind in (_PYTHON, _TRANSLATE):
            return

        # 屏幕内文本控件：text / textbutton / label "文本"
        # （屏幕内没有 say 对话；其余语句一律跳过）
        if top and top.kind == _SCREEN:
            sm = _SCREEN_TEXT_RE.match(stripped)
            if sm:
                value, _ = _parse_string_literal(stripped, sm.start(2))
                if value is not None:
                    self._add_string(value, ll.line, context="screen")
            return

        # 语句关键字
        keyword = self._keyword_of(stripped)
        if keyword == "label":
            self._handle_label(stripped)
            return
        if keyword in ("python", "init") and self._is_python_stmt(stripped):
            self._stack.append(_Ctx(_PYTHON, indent))
            return
        if keyword == "translate":
            self._stack.append(_Ctx(_TRANSLATE, indent))
            return
        if keyword == "menu":
            self._stack.append(_Ctx(_MENU, indent))
            return
        if keyword == "screen":
            self._stack.append(_Ctx(_SCREEN, indent))
            return
        if keyword in ("style", "transform", "image", "layeredimage") and stripped.endswith(":"):
            # style / transform / image (ATL) 块内只有属性语句，无对话
            self._stack.append(_Ctx(_PYTHON, indent))
            return
        if keyword in ("define", "default"):
            self._handle_define(stripped, ll.line)
            return
        if keyword == "$":
            return  # 单行 python

        # menu 上下文中的裸字符串 = 菜单选项/标题（仅限菜单体缩进）
        if top and top.kind == _MENU and self._is_bare_string_start(stripped):
            if top.menu_indent is None:
                top.menu_indent = indent
            if indent == top.menu_indent:
                self._handle_menu_string(stripped, ll.line)
                return
            # 更深缩进的裸字符串 → 旁白对话，继续走 say 解析

        # 尝试解析为对话（say）
        units = self._parse_say(stripped, ll.line)
        if units is None:
            return
        for who, what, kw in units:
            if what == "{clear}":
                continue
            self._add_dialogue(who, what, ll.line, text, **kw)

    @staticmethod
    def _keyword_of(stripped: str) -> str | None:
        m = re.match(r"(\w+)", stripped)
        return m.group(1) if m else None

    @staticmethod
    def _is_python_stmt(stripped: str) -> bool:
        if stripped.startswith("python"):
            return True
        return bool(re.match(r"^init(?:\s+offset\s+\d+)?(?:\s+-\w+)*\s+python\b", stripped))

    def _handle_label(self, stripped: str):
        # label name:  或  label name(参数):
        # 必须带冒号（屏幕里的 `label 变量` / `label 变量:` 控件不在此列）
        if not stripped.endswith(":"):
            return
        m = re.match(r"^label\s+([^\s:()]+)", stripped)
        if not m:
            return
        name = m.group(1)
        if name.startswith("_"):
            self._alternate = name
            return
        if "." in name or name.startswith("."):
            return  # 局部标签（含点）不改变当前 label
        self._label = name
        self._alternate = None

    @staticmethod
    def _is_bare_string_start(stripped: str) -> bool:
        return bool(_STRING_START.match(stripped))

    def _handle_menu_string(self, stripped: str, line_no: int):
        value, _ = _parse_string_literal(stripped, 0)
        if value is None or not value or len(value) > 2000:
            return
        self._add_string(value, line_no, context="menu")

    def _handle_define(self, stripped: str, line_no: int):
        """define/default 语句：
        1) 值直接是字符串（跳过明显非文本内容）
        2) Character("名称") / DynamicCharacter(...) 的首个字符串参数
        3) 跨行表达式（括号未闭合）时忽略后续参数行
        """
        self._open_define_parens += self._paren_net(stripped)
        m = re.match(r'^(?:define|default)\s+[\w.]+(?:\[[^\]]*\])?\s*(?:=|\+=)\s*', stripped)
        if m:
            value, _ = _parse_string_literal(stripped, m.end())
            if value is not None and self._is_translatable_plain_string(value):
                self._add_string(value, line_no, context="define")
        for cm in _CHARACTER_RE.finditer(stripped):
            sm = _STRING_START.search(stripped, cm.end())
            if sm:
                value, _ = _parse_string_literal(stripped, sm.start())
                if value is not None and self._is_translatable_plain_string(value):
                    self._add_string(value, line_no, context="character")

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
        m = re.match(r"^([A-Za-z_]\w*)", stripped)
        if m:
            who = m.group(1)
            if who not in NON_WHO_KEYWORDS or who == "extend":
                r = self._parse_say_rest(stripped, m.end())
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

        # 已给出 who 时解析属性
        if pos > 0:
            who = s[:pos].strip()
            while True:
                j = i
                while j < n and s[j] in " \t":
                    j += 1
                m = re.match(r"([+-][A-Za-z_]\w*)", s[j:])
                if not m:
                    break
                attributes.append(m.group(1))
                i = j + len(m.group(1))
            # 临时属性：@ -a +b
            j = i
            while j < n and s[j] in " \t":
                j += 1
            if j < n and s[j] == "@":
                i = j + 1
                while True:
                    j = i
                    while j < n and s[j] in " \t":
                        j += 1
                    m = re.match(r"([+-][A-Za-z_]\w*)", s[j:])
                    if not m:
                        break
                    temporary.append(m.group(1))
                    i = j + len(m.group(1))

        # 第一个字符串（必需）
        while i < n and s[i] in " \t":
            i += 1
        what, i = _parse_string_literal(s, i)
        if what is None:
            return None

        interact = True
        with_expr = None
        explicit_id = None
        arguments = None
        whats = [what]

        # 解析尾部标记
        while i < n:
            while i < n and s[i] in " \t":
                i += 1
            if i >= n:
                break
            if s.startswith("nointeract", i) and (i + 10 == n or s[i + 10] in " \t"):
                interact = False
                i += 10
                continue
            m = re.match(r"with\s+([A-Za-z_][\w.]*|\([^)]*\))", s[i:])
            if m:
                with_expr = m.group(1)
                i += len(m.group(0))
                continue
            m = re.match(r"id\s+([\w]+)", s[i:])
            if m:
                explicit_id = m.group(1)
                i += len(m.group(0))
                continue
            if s[i] == "(":
                depth = 0
                j = i
                while j < n:
                    if s[j] == "(":
                        depth += 1
                    elif s[j] == ")":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                arguments = s[i:j]
                i = j
                continue
            # 追加字符串（monologue 模式）
            v, ni = _parse_string_literal(s, i)
            if v is not None:
                whats.append(v)
                i = ni
                continue
            break

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

_UNDERSCORE_RE = re.compile(
    r'\b(_p|___|__|_)\s*\(\s*'
    r'(?P<quote>"""|\'\'\'|"|\')'
)


def scan_underscore_strings(text: str, filename: str) -> list[StringUnit]:
    """扫描 _()/__()/___()/_p() 字符串调用（对应 Ren'Py STRING_RE）。
    基于逻辑行扫描，支持跨行三引号字符串。"""
    result: list[StringUnit] = []
    seen: set[str] = set()
    for ll in _logical_lines(text):
        i = 0
        while True:
            m = _UNDERSCORE_RE.search(ll.text, i)
            if not m:
                break
            value, end = _parse_string_literal(ll.text, m.start("quote"))
            if value is not None and value not in seen and 0 < len(value) <= 2000:
                seen.add(value)
                result.append(StringUnit(text=value, filename=filename,
                                         line=ll.line, context="_()"))
            i = end if end > m.start() else m.start() + 1
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

    extractor = RpyExtractor(p)
    result = extractor.parse(raw)

    for u in scan_underscore_strings(raw, str(p)):
        if u.text not in extractor.seen_strings:
            extractor.seen_strings.add(u.text)
            result.strings.append(u)
    return result


def extract_rpy_files(files: list[Path]) -> ExtractionResult:
    """批量解析多个 .rpy 文件。"""
    all_dialogues: list[DialogueUnit] = []
    all_strings: list[StringUnit] = []
    skipped: list[str] = []
    by_file: dict[str, list[DialogueUnit]] = {}
    seen_strings: set[str] = set()
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
                continue
            seen_strings.add(u.text)
            all_strings.append(u)

    return ExtractionResult(
        dialogues=all_dialogues, strings=all_strings,
        files=by_file, skipped=skipped,
    )
