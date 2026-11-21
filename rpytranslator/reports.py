"""Read and import the same retry queue used by the pipeline and audit."""
from __future__ import annotations

import re
from pathlib import Path

from .storage import write_text_atomic

_LOCATION = re.compile(r"^(.+?:\d+)  (.+)$")
_ORIGINAL = re.compile(r"^\s*原文[:：] ?(.*)$")
_TRANSLATION = re.compile(r"^\s*译文[:：]")
AUDIT_EXPORT_SECTION = "== 可导入重译的原文 =="
_AUDIT_SECTION = "== 补漏查缺发现的问题（自动排入，重译成功后消失）=="
_MANUAL_SECTION = "== 人工兜底导入 =="


def _escape_report_text(text: str) -> str:
    """Encode an exact source string into a single queue line."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def _unescape_report_text(text: str) -> str:
    return re.sub(r"\\([\\nr])", lambda m: {"n": "\n", "r": "\r", "\\": "\\"}[m[1]], text)


def _lines(path: Path) -> list[str]:
    # Unicode line/paragraph separators are valid inside a game string.
    return path.read_text(encoding="utf-8-sig").split("\n")


def _entries(lines: list[str]) -> dict[str, str]:
    entries = {}
    for line in lines:
        match = _LOCATION.match(line.lstrip())
        if match:
            entries.setdefault(_unescape_report_text(match[2]), match[1])
    return entries


def read_retry_sections(path: Path) -> tuple[dict[str, str], set[str], set[str]]:
    """Read both current and older queues, retaining manual requests and failed requests.

    Texts that belong to neither set came from a previous offline audit.
    """
    entries, manual, failed = {}, set(), set()
    if not path.is_file():
        return entries, manual, failed
    section = "manual"  # a plain hand-written list is an explicit request
    for line in _lines(path):
        if line.startswith("以下文本"):
            section = "failed"
        if line.startswith("=="):
            if "补漏查缺发现的问题" in line:
                section = "audit"
            elif "人工" in line:
                section = "manual"
            elif "错误详情" in line:
                section = "details"
            else:
                section = "failed"
            continue
        match = _LOCATION.match(line.lstrip())
        if not match or section == "details":
            continue
        text = _unescape_report_text(match[2])
        entries.setdefault(text, match[1])
        if section == "manual":
            manual.add(text)
        elif section == "failed":
            failed.add(text)
    return entries, manual, failed


def read_retry_entries(path: Path) -> dict[str, str]:
    """Return exact source text and its first file:line location in queue order."""
    return read_retry_sections(path)[0]


def write_retry_entries(path: Path, pending: dict[str, str], previous: dict[str, str],
                        manual_texts=(), *, audit_texts=()) -> None:
    if not pending:
        path.unlink(missing_ok=True)
        return
    if pending == previous:
        return
    manual = set(manual_texts)
    audit = set(audit_texts) - manual
    lines = ["以下文本仍待翻译，下次开始汉化时会继续处理："]
    for heading, keys in (("== 自动重试 ==", pending.keys() - (manual | audit)),
                          (_MANUAL_SECTION, manual), (_AUDIT_SECTION, audit)):
        group = [f"{loc}  {_escape_report_text(text)}" for text, loc in pending.items() if text in keys]
        if group:
            lines.extend(("", heading, *group))
    write_text_atomic(path, "\n".join(lines) + "\n", "utf-8-sig")


def sync_audit_queue(path: Path, problems: dict[str, str]) -> int:
    previous, manual, failed = read_retry_sections(path)
    pending = {text: loc for text, loc in previous.items() if text in manual or text in failed}
    audited = problems.keys() - pending.keys()
    pending.update((text, loc) for text, loc in problems.items() if text in audited)
    write_retry_entries(path, pending, previous, manual, audit_texts=audited)
    return len(problems.keys() - previous.keys())


def read_retry_queue(path: Path) -> list[str]:
    return list(read_retry_entries(path))


def parse_external_report(path: str | Path) -> tuple[list[str], str]:
    try:
        lines = _lines(Path(path))
    except (OSError, UnicodeError):
        return [], "读取失败"
    if AUDIT_EXPORT_SECTION in lines:
        start = len(lines) - lines[::-1].index(AUDIT_EXPORT_SECTION)
        texts = list(_entries(lines[start:]))
        return texts, f"补漏查缺报告（{len(texts)} 条待重译）"
    originals: list[str] = []
    current: list[str] | None = None
    for line in lines:
        match = _ORIGINAL.match(line)
        if match:
            current = [match[1]]
        elif current is not None and _TRANSLATION.match(line):
            text = "\n".join(current)
            if text.strip():
                originals.append(text)
            current = None
        elif current is not None:
            current.append(line)
    if originals:
        return originals, f"补漏查缺报告（{len(originals)} 条）"
    texts = list(_entries(lines))
    return (texts, f"未翻译报告（{len(texts)} 条）" if texts else "格式不支持（未识别到报告条目）")


def import_report_texts(texts: list[str], game_path: str | Path, language: str,
                        log=None) -> tuple[int, int, str]:
    game = Path(game_path)
    if (game / "game").is_dir():
        game /= "game"
    lang = language.split("（")[0].split("(")[0].strip()
    if not lang.isidentifier() or lang.lower() == "none":
        raise ValueError(f"目标语言标识无效: {lang}")
    report = game / "tl" / f"{lang}.未翻译报告.txt"
    existing = read_retry_queue(report)
    seen = set(existing)
    added = list(dict.fromkeys(t for t in texts if t and t.strip() and t not in seen))
    if added:
        report.parent.mkdir(parents=True, exist_ok=True)
        with open(report, "a", encoding="utf-8" if report.is_file() else "utf-8-sig") as f:
            f.write("\n\n== 人工兜底导入 ==\n")
            for text in added:
                f.write(f"\n外部导入:0  {_escape_report_text(text)}")
    if log:
        log(f"已导入 {len(added)} 条到重译队列，原有 {len(existing)} 条保留")
    return len(added), len(existing), str(report)
