"""Index Ren'Py translations and locate editable literals without executing scripts."""
from __future__ import annotations

import ast
import codecs
import io
import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from .extract import NON_WHO_KEYWORDS, decode_say_string, encode_say_string

_BLOCK = re.compile(r"^([ \t]*)translate\s+(\w+)\s+(\w+)\s*:\s*(?:#.*)?$")
_LOC = re.compile(r"^\s*#\s*(.+):(\d+)\s*$")
_LITERAL = re.compile(r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
_SPEAKER = re.compile(r"[\w.]+")
_ATTRIBUTES = re.compile(r"(?:[ \t]+(?:@|[+-]?[\w.]+))*[ \t]+")
_PAIR = re.compile(r"^([ \t]*)(old|new)[ \t]+")


class TranslationMergeError(ValueError):
    """Existing translation definitions cannot be merged without risking a conflict."""


def encode_string(value: str) -> str:
    # old/new operands are Python expressions. Unlike say literals they do not
    # collapse spaces, so Ren'Py's say-specific `\ ` escape is incorrect here.
    return json.dumps(value, ensure_ascii=False)


@dataclass
class TLEntry:
    identifier: str | None
    original: str | None
    translation: str | None
    who: str
    src_file: str
    src_line: int
    block_start: int
    block_end: int
    line: int = -1
    span: tuple[int, int] | None = None
    original_line: int = -1
    original_span: tuple[int, int] | None = None

    def with_translation(self, line: str, value: str) -> str:
        if self.span is None:
            raise TranslationMergeError("该译文不是可安全替换的字符串")
        start, end = self.span
        literal = encode_string(value) if self.identifier is None else encode_say_string(value)
        return line[:start] + literal + line[end:]


@dataclass
class TLFile:
    path: Path
    lines: list[str]
    entries: list[TLEntry]
    bom: bool
    read_only: bool = False
    origin: str = ""
    # Ren'Py skips .rpy/.rpym sources in archives. Their static translations
    # can be reused in a loose script, but they do not occupy any runtime ID.
    reference_entries: list[TLEntry] = field(default_factory=list)

    @property
    def newline(self) -> str:
        for line in self.lines:
            if line.endswith("\r\n"):
                return "\r\n"
            if line.endswith(("\r", "\n")):
                return line[-1]
        return "\n"


def _literal(line: str, offset: int) -> re.Match | None:
    if line.startswith(('"""', "'''"), offset):
        return None
    return _LITERAL.match(line, offset)


def _say(line: str) -> tuple[str, str, tuple[int, int]] | None:
    start = len(line) - len(line.lstrip())
    first = _literal(line, start)
    speaker = first or _SPEAKER.match(line, start)
    spoken = None
    who = ""
    if speaker and speaker[0] not in NON_WHO_KEYWORDS:
        attr = _ATTRIBUTES.match(line, speaker.end())
        if attr:
            spoken = _literal(line, attr.end())
        if spoken:
            who = speaker[0]
    spoken = spoken or first
    if spoken is None:
        return None
    tail = line[spoken.end():].strip()
    if tail and not (tail.startswith(("#", "(")) or
                     re.match(r"(?:nointeract|with|id)\b", tail)):
        return None
    return who, decode_say_string(spoken[0][1:-1]), spoken.span()


def _strings(path: Path, lines: list[str], start: int, end: int,
             loc: tuple[str, int]) -> list[TLEntry]:
    entries = []
    old = None
    for i in range(start + 1, end):
        line = lines[i]
        match = _LOC.match(line)
        if match:
            loc = match[1], int(match[2])
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pair = _PAIR.match(line)
        if not pair or (pair[2] == "old") != (old is None):
            raise TranslationMergeError(f"{path}:{i + 1} 的 old/new 结构无法安全合并")
        literal = _literal(line, pair.end())
        span = None
        if literal:
            tail = line[literal.end():].strip()
            if not tail or tail.startswith("#"):
                span = literal.span()
        try:
            # Older generators wrote say-style escaped spaces in old/new.
            # Read their actual Python value, without printing a warning per key.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                value = ast.literal_eval(line[pair.end():].strip())
        except (ValueError, SyntaxError):
            value = None
        if pair[2] == "old":
            if not isinstance(value, str):
                raise TranslationMergeError(f"{path}:{i + 1} 的旧译使用动态或跨行字符串键，无法排除重复")
            old = value
        else:
            entries.append(TLEntry(None, old, value if isinstance(value, str) else None,
                                   "", *loc, start, end, i, span))
            old = None
    if old is not None:
        raise TranslationMergeError(f"{path}:{end} 的 old 缺少对应 new")
    return entries


def read_tl_file(path: Path, language: str | None = None, *, raw: bytes | None = None) -> TLFile:
    try:
        if raw is None:
            raw = path.read_bytes()
        lines = io.StringIO(raw.decode("utf-8-sig"), newline="").readlines()
    except (OSError, UnicodeError) as exc:
        raise TranslationMergeError(f"不能读取已有翻译 {path}：{exc}") from exc
    entries: list[TLEntry] = []
    loc = path.name, 0
    i = 0
    while i < len(lines):
        line = lines[i]
        position = _LOC.match(line)
        if position:
            loc = position[1], int(position[2])
        header = _BLOCK.match(line.rstrip("\r\n"))
        if not header:
            i += 1
            continue
        end = i + 1
        while end < len(lines):
            nxt = lines[end]
            if (nxt.strip() and not nxt.lstrip().startswith("#") and
                    len(nxt) - len(nxt.lstrip()) <= len(header[1])):
                break
            end += 1
        # An unindented location comment following a block belongs to the next
        # definition. Unindented comments between statements do not end a block.
        while end > i + 1 and (not lines[end - 1].strip() or
                               lines[end - 1].startswith("#")):
            end -= 1
        identifier = header[3]
        if language is None or header[2] == language:
            if identifier == "strings":
                entries.extend(_strings(path, lines, i, end, loc))
            elif identifier not in ("style", "python"):
                body = [(j, lines[j]) for j in range(i + 1, end) if lines[j].strip()]
                statements = [(j, ln) for j, ln in body if not ln.lstrip().startswith("#")]
                entry = TLEntry(identifier, None, None, "", *loc, i, end)
                for j, ln in body:
                    if ln.lstrip().startswith("#"):
                        offset = ln.index("#") + 1
                        source = _say(ln[offset:])
                        if source:
                            entry.who, entry.original, span = source
                            entry.original_line = j
                            entry.original_span = span[0] + offset, span[1] + offset
                            break
                if len(statements) == 1:
                    j, ln = statements[0]
                    parsed = _say(ln)
                    if parsed:
                        entry.who, entry.translation, entry.span = parsed
                        entry.line = j
                entries.append(entry)
        i = end
    return TLFile(path, lines, entries, raw.startswith(codecs.BOM_UTF8))


def write_if_unchanged(path: Path, content: str, original: TLFile | None) -> None:
    """Preserve encoding and avoid publishing an incomplete script after interruption."""
    if original is not None and original.read_only:
        raise TranslationMergeError(f"已有译文只读，无法写入：{original.origin or original.path}")
    encoding = "utf-8-sig" if original is None or original.bom else "utf-8"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".rpytranslator-",
                                     suffix=".tmp", delete=False) as pending:
        temp = Path(pending.name)
        try:
            pending.write(content.encode(encoding))
        except BaseException:
            pending.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        if original is None:
            if path.exists():
                raise TranslationMergeError(f"翻译过程中出现了同名文件，请重新扫描：{path}")
        elif path.read_bytes() != "".join(original.lines).encode(encoding):
            raise TranslationMergeError(f"翻译过程中已有文件发生更改，请重新扫描：{path}")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def editable_output_path(path: Path, docs: dict[Path, TLFile]) -> Path:
    """Choose a loose script that will not mask a compiled or archived one."""
    while True:
        doc = docs.get(path)
        if doc is not None:
            if not doc.read_only:
                return path
        elif path.with_suffix(path.suffix + "c") not in docs:
            return path
        path = path.with_name(path.stem + "_rpytranslator" + path.suffix)


def unambiguous_translations(pairs):
    """Keep a translation only when every nonempty candidate agrees on it."""
    result = {}
    for key, value in pairs:
        if key and value:
            if key in result and result[key] != value:
                result[key] = None
            else:
                result[key] = value
    return result


def _compiled_docs(files: dict[Path, tuple[Path, str]], base: Path,
                   language: str) -> dict[Path, TLFile]:
    if not files:
        return {}
    from .rpyc_loader import cleanup, decompile_rpyc_files
    temp = None
    docs = {}
    try:
        temp, sources = decompile_rpyc_files(list(files), base)
        for physical, (path, origin) in files.items():
            if physical not in sources:
                raise TranslationMergeError(f"无法反编译已有译文 {origin}，无法排除重复定义")
            try:
                doc = read_tl_file(sources[physical], language)
            except TranslationMergeError as exc:
                raise TranslationMergeError(f"{origin}：{exc}") from exc
            doc.path, doc.origin, doc.read_only = path, origin, True
            docs[path] = doc
    except (ImportError, RuntimeError, OSError) as exc:
        raise TranslationMergeError(f"无法读取已有编译译文 {next(iter(files.values()))[1]}：{exc}") from exc
    finally:
        cleanup(temp)
    return docs


def index_tl_directory(root: Path, language: str) -> dict[Path, TLFile]:
    """Read loose and archived translations before adding a definition to the game."""
    from . import rpa_loader
    files = sorted(p for p in root.rglob("*") if rpa_loader.is_script_name(p.name) and p.is_file())
    docs = {p: read_tl_file(p, language) for p in files if not p.suffix.lower().endswith("c")}
    compiled = {p: (p, str(p)) for p in files if p.suffix.lower().endswith("c")
                and p.with_suffix(p.suffix.lower()[:-1]) not in docs}
    docs.update(_compiled_docs(compiled, root, language))
    game = root.parent.parent
    if root.parent.name.lower() != "tl" or not game.is_dir():
        return docs

    loose = {rpa_loader.script_key(p.relative_to(game).as_posix()) for p in files}
    prefix = f"tl/{language}/".casefold()
    archives = rpa_loader.find_archives(game)
    if not archives:
        return docs
    try:
        members = rpa_loader.iter_script_entries(archives, strict=True)
        with tempfile.TemporaryDirectory(prefix="rpy_archived_tl_") as tmp:
            base = Path(tmp)
            compiled = {}
            for archive, name, entry in members:
                if not name.casefold().startswith(prefix) or rpa_loader.script_key(name) in loose:
                    continue
                relative = Path(*name.split("/")[2:])
                path = root / relative
                origin = f"{archive}!{name}"
                try:
                    data = rpa_loader.read_entry(archive, entry)
                    if name.lower().endswith("c"):
                        physical = base / relative
                        physical.parent.mkdir(parents=True, exist_ok=True)
                        physical.write_bytes(data)
                        compiled[physical] = path, origin
                    else:
                        doc = read_tl_file(path, language, raw=data)
                        doc.origin, doc.read_only = origin, True
                        doc.reference_entries, doc.entries = doc.entries, []
                        docs[path] = doc
                except (OSError, ValueError) as exc:
                    raise TranslationMergeError(f"{origin}：{exc}") from exc
            docs.update(_compiled_docs(compiled, base, language))
    except (OSError, ValueError) as exc:
        if isinstance(exc, TranslationMergeError):
            raise
        raise TranslationMergeError(f"无法检查已有归档译文：{exc}") from exc
    return docs
