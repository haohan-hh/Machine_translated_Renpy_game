# -*- coding: utf-8 -*-
"""
RPA 归档（Ren'Py archive）脚本抽取器。

部分游戏发行时把脚本打进 .rpa 归档（game 目录下没有松散的 .rpy/.rpyc），
工具需要先解析归档索引、把脚本文件物化到临时目录才能继续汉化。

格式解析复刻 Ren'Py 官方 renpy/loader.py：
- RPA-3.0 头: `RPA-3.0 <offset16hex> <key8hex>\n`，offset 为明文文件偏移，
  key 用于异或还原索引条目的 offset/length。
- RPA-2.0 头: `RPA-2.0 <offset16hex>\n`，无 key。
- 索引位于 offset 处，两种格式均为 zlib 压缩的 pickle dict，
  形如 {文件名: [(offset, length, prefix), ...]}。
- 每段内容 = prefix + 归档[offset : offset+length]。多段按索引顺序拼接。
"""
from __future__ import annotations

import codecs
import io
import pickle
import unicodedata
import zlib
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PureWindowsPath

# 与 engine.ALL_EXTS 保持一致
_SCRIPT_EXTS = (".rpy", ".rpym", ".rpyc", ".rpymc")
ArchiveEntry = tuple[tuple[int, int, bytes], ...]
ScriptEntry = tuple[Path, str, ArchiveEntry]


def is_script_name(name: str) -> bool:
    return name.lower().endswith(_SCRIPT_EXTS)


def _path_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.replace("\\", "/").lower())


def script_key(name: str) -> str:
    """同一路径的源码与编译版共享键，.rpy 与 .rpym 仍是不同脚本。"""
    return _path_key(name).removesuffix("c")


def find_archives(game_dir: Path) -> list[Path]:
    """Ren'Py 的默认 config.archives 顺序：文件名逆序，首个同路径条目优先。"""
    return sorted((p for p in game_dir.iterdir() if p.suffix.lower() == ".rpa" and p.is_file()),
                  key=lambda p: p.name, reverse=True)


class _IndexReader(pickle.Unpickler):
    """Archive indexes contain data only; older pickle protocols encode bytes via codecs."""

    def find_class(self, module, name):
        if (module, name) == ("_codecs", "encode"):
            return codecs.encode
        raise pickle.UnpicklingError("归档索引含不支持的对象类型")


def parse_rpa_index(rpa_path: str | Path) -> dict[str, ArchiveEntry]:
    """返回归档内各相对路径对应的全体 (offset, length, prefix) 数据段。"""
    rpa = Path(rpa_path)
    with open(rpa, "rb") as f:
        header = f.read(40)
        if not header.startswith((b"RPA-2.0 ", b"RPA-3.0 ")):
            raise ValueError(f"不支持的归档格式: {rpa.name}")
        offset = int(header[8:24], 16)
        key = int(header[25:33], 16) if header.startswith(b"RPA-3.0 ") else 0
        f.seek(offset)
        try:
            raw = zlib.decompress(f.read())
            index = _IndexReader(io.BytesIO(raw), encoding="latin-1").load()
            if not isinstance(index, dict):
                raise ValueError("索引不是文件表")
        except Exception as exc:  # 兼容反序列化差异
            raise ValueError(f"无法解析归档索引 {rpa.name}: {exc}") from exc

    result: dict[str, ArchiveEntry] = {}
    for name, entries in index.items():
        if not isinstance(name, str) or not entries:
            continue
        name = name.replace("\\", "/")
        member = PureWindowsPath(name)
        if member.anchor or ".." in member.parts or ":" in name:
            raise ValueError(f"归档内路径无效: {name!r}")
        chunks = []
        try:
            for item in entries:
                off, length, *starts = item
                if len(starts) > 1:
                    raise ValueError("数据段字段数无效")
                off, length = off ^ key, length ^ key
                if off < 0 or length < 0 or off + length > offset:
                    raise ValueError("数据段超出归档数据区")
                prefix = (starts[0] or b"") if starts else b""
                if isinstance(prefix, str):
                    prefix = prefix.encode("latin-1")
                if not isinstance(prefix, bytes):
                    raise ValueError("数据段前缀类型无效")
                chunks.append((off, length, prefix))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{rpa.name} 中 {name!r} 的归档索引无效: {exc}") from exc
        result[name] = tuple(chunks)
    return result


def read_entry(rpa: Path, entry: ArchiveEntry) -> bytes:
    """按索引顺序拼接所有前缀和数据段，包括旧式多段文件。"""
    parts = []
    with rpa.open("rb") as f:
        for offset, length, prefix in entry:
            f.seek(offset)
            data = f.read(length)
            if len(data) != length:
                raise ValueError(f"归档数据不完整: {rpa.name}")
            parts.extend((prefix, data))
    return b"".join(parts)


# Retain the old name for code using indexes returned by parse_rpa_index.
_read_entry = read_entry


def list_script_names(rpa_path: str | Path) -> list[str]:
    """返回归档中全部脚本文件的相对路径名（保持归档内顺序）。"""
    return [n for n in parse_rpa_index(rpa_path) if is_script_name(n)]


def _archive_indices(
    rpa_files: Iterable[str | Path], strict: bool = False,
) -> Iterator[tuple[Path, dict[str, ArchiveEntry]]]:
    for rpa in map(Path, rpa_files):
        try:
            index = parse_rpa_index(rpa)
        except (OSError, ValueError) as exc:
            if strict:
                raise ValueError(f"无法读取归档 {rpa.name}: {exc}") from exc
            continue
        yield rpa, index


def _select_scripts(indices: Iterable[tuple[Path, dict[str, ArchiveEntry]]],
                    prefer: str) -> Iterator[ScriptEntry]:
    selected: dict[str, ScriptEntry] = {}
    prefer_compiled = prefer == "rpyc"
    for rpa, index in indices:
        for name, entry in index.items():
            if not is_script_name(name):
                continue
            logical = script_key(name)
            previous = selected.get(logical)
            if previous is None:
                selected[logical] = (rpa, name, entry)
                continue
            # Earlier archives win for the same extension. Replace a fallback
            # only when an archive contains the preferred extension.
            if (name.lower().endswith("c") == prefer_compiled
                    and previous[1].lower().endswith("c") != prefer_compiled):
                selected[logical] = (rpa, name, entry)
    yield from selected.values()


def iter_script_entries(rpa_files: Iterable[str | Path], prefer: str = "rpyc", *,
                        strict: bool = False) -> Iterator[ScriptEntry]:
    """按传入的归档搜索顺序选择唯一脚本。运行时使用归档编译版；若只存放
    源码，工具仍允许提取它，兼容源码分发包。"""
    return _select_scripts(_archive_indices(rpa_files, strict), prefer)


def _write_scripts(entries: Iterable[ScriptEntry], dest_dir: str | Path) -> list[Path]:
    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for rpa, name, entry in entries:
        out = (dest / name).resolve()
        if not out.is_relative_to(dest):
            raise ValueError(f"归档内路径无效: {name!r}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(read_entry(rpa, entry))
        written.append(out)
    return written


def extract_scripts_to(rpa_path: str | Path, dest_dir: str | Path, prefer: str = "rpyc") -> list[Path]:
    """物化单个归档的脚本；无效归档会抛出异常。"""
    rpa = Path(rpa_path)
    return _write_scripts(_select_scripts([(rpa, parse_rpa_index(rpa))], prefer), dest_dir)


def extract_all_scripts_to(
    rpa_files: Iterable[str | Path],
    dest_dir: str | Path,
    prefer: str = "rpyc",
    *,
    include: Callable[[str], bool] | None = None,
) -> list[Path]:
    """按高到低的归档优先级处理，每个脚本只写出实际选中的一份。"""
    entries = iter_script_entries(rpa_files, prefer)
    return _write_scripts((row for row in entries if include is None or include(row[1])), dest_dir)


def read_script_data(
    rpa_files: Iterable[str | Path],
    name: str,
    *,
    allow_alternate: bool = True,
) -> bytes | None:
    """优先查找准确扩展名，全部归档均缺失才回退到同源编译对。
    需要按请求的扩展名解码时传 allow_alternate=False。"""
    wanted = _path_key(name)
    alternate = None
    if allow_alternate and is_script_name(wanted):
        alternate = wanted[:-1] if wanted.endswith("c") else wanted + "c"
    fallback: tuple[Path, ArchiveEntry] | None = None
    for rpa, index in _archive_indices(rpa_files):
        for idx_name, entry in index.items():
            key = _path_key(idx_name)
            if key == wanted:
                return read_entry(rpa, entry)
            if key == alternate and fallback is None:
                fallback = (rpa, entry)
    return read_entry(*fallback) if fallback is not None else None
