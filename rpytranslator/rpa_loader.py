# -*- coding: utf-8 -*-
"""
RPA 归档（Ren'Py archive）脚本抽取器。

部分游戏发行时把脚本打进 .rpa 归档（game 目录下没有松散的 .rpy/.rpyc），
工具需要先解析归档索引、把脚本文件物化到临时目录才能继续汉化。

格式解析复刻 Ren'Py 官方 renpy/loader.py：
- RPA-3.0 头: `RPA-3.0 <offset16hex> <key8hex>\n`，offset 为明文文件偏移，
  key 用于异或还原索引条目的 offset/length。
- RPA-2.0 头: `RPA-2.0 <offset16hex>\n`，无 key。
- 索引位于 offset 处，为 zlib 压缩的 pickle（3.0）/ pickle（2.0）dict，
  形如 {文件名: [(offset, length, prefix), ...]}。
- 条目内容 = prefix + 归档[offset : offset+length]。
"""
from __future__ import annotations

import pickle
import zlib
from pathlib import Path

# 与 engine.ALL_EXTS 保持一致
_SCRIPT_EXTS = (".rpy", ".rpym", ".rpyc", ".rpymc")


def is_script_name(name: str) -> bool:
    return name.lower().endswith(_SCRIPT_EXTS)


def parse_rpa_index(rpa_path: str | Path) -> dict[str, tuple[int, int, bytes]]:
    """解析 .rpa 归档索引，返回 {归档内相对路径: (offset, length, prefix)}。

    offset 为文件数据在归档中的绝对偏移；length 为该文件压缩块长度；
    prefix 为文件实际内容的起始前缀（完整内容 = prefix + 归档[offset:offset+length]）。
    """
    rpa = Path(rpa_path)
    with open(rpa, "rb") as f:
        header = f.read(40)
        if not header.startswith(b"RPA-3.0 "):
            # RPA-2.0：读 24 字节，offset 明文且无需异或
            with open(rpa, "rb") as f2:
                h2 = f2.read(24)
            if h2.startswith(b"RPA-2.0 "):
                offset = int(h2[8:24], 16)
                key = 0
                f.seek(offset)
                raw = f.read()
            else:
                raise ValueError(f"不支持的归档格式: {rpa.name}")
        else:
            offset = int(header[8:24], 16)
            key = int(header[25:33], 16)
            f.seek(offset)
            raw = f.read()
        try:
            index = pickle.loads(zlib.decompress(raw))
        except Exception as exc:  # 兼容反序列化差异
            raise ValueError(f"无法解析归档索引 {rpa.name}: {exc}") from exc

    result: dict[str, tuple[int, int, bytes]] = {}
    for name, entries in index.items():
        if not entries:
            continue
        # 同名多条目取最后一条（与 Ren'Py 加载顺序一致：后写覆盖先写）
        e = entries[-1]
        if len(e) == 2:
            off, dlen = e
            prefix = b""
        else:
            off, dlen, prefix = e
            if not isinstance(prefix, bytes):
                prefix = (prefix or "").encode("latin-1")
        result[name] = (off ^ key, dlen ^ key, prefix)
    return result


def _read_entry(rpa: Path, entry: tuple[int, int, bytes]) -> bytes:
    """读取归档中单条文件内容（prefix + 数据块）。"""
    offset, length, prefix = entry
    data = bytearray(prefix)
    if length:
        with open(rpa, "rb") as f:
            f.seek(offset)
            data += f.read(length)
    return bytes(data)


def list_script_names(rpa_path: str | Path) -> list[str]:
    """返回归档中全部脚本文件的相对路径名（保持归档内顺序）。"""
    return [n for n in parse_rpa_index(rpa_path) if is_script_name(n)]


def extract_scripts_to(
    rpa_path: str | Path,
    dest_dir: str | Path,
    prefer: str = "rpyc",
) -> list[Path]:
    """把归档内脚本物化到 dest_dir，返回写出的文件路径列表。

    prefer 决定 .rpy 与 .rpyc 同名共存时物化哪一份（Ren'Py 运行时优先加载
    .rpyc，翻译 identifier 必须与之对齐，故默认 "rpyc"）；
    为 "rpy" 时优先 .rpy，两者皆缺时才取另一种。
    资源文件（图片/音频等）不解出。保留归档内子目录结构。
    """
    index = parse_rpa_index(rpa_path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    # 收集编译对（foo.rpyc↔foo.rpy、bar.rpymc↔bar.rpym 同组），
    # .rpym 模块与 .rpy 脚本互不干扰。按 prefer 决定取舍。
    groups: dict[tuple[str, str], dict[str, tuple[str, tuple[int, int, bytes]]]] = {}
    for name, entry in index.items():
        if not is_script_name(name):
            continue
        stem, ext = name.rsplit(".", 1)
        stem, ext = stem.lower(), ext.lower()
        family = "rpym" if ext in ("rpym", "rpymc") else "rpy"
        groups.setdefault((stem, family), {})[ext] = (name, entry)

    want = ("rpyc", "rpy") if prefer == "rpyc" else ("rpy", "rpyc")
    want_rpym = ("rpymc", "rpym") if prefer == "rpyc" else ("rpym", "rpymc")
    written: list[Path] = []
    rpa = Path(rpa_path)
    for (stem, family), group in groups.items():
        seq = want if family == "rpy" else want_rpym
        picked = next((group[ext] for ext in seq if ext in group), None)
        if picked is None:
            continue
        name, entry = picked
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_read_entry(rpa, entry))
        written.append(out)
    return written


def extract_all_scripts_to(
    rpa_files: list[str | Path],
    dest_dir: str | Path,
    prefer: str = "rpyc",
) -> list[Path]:
    """多个 .rpa 依次物化脚本到同一目录（后处理的归档同名覆盖先前的，
    与 Ren'Py 按 config.archives 顺序覆盖一致）。返回全部写出路径。"""
    all_files: list[Path] = []
    for rpa in rpa_files:
        try:
            all_files.extend(extract_scripts_to(rpa, dest_dir, prefer=prefer))
        except (OSError, ValueError):
            continue
    return all_files


def read_script_data(
    rpa_files: list[str | Path],
    name: str,
) -> bytes | None:
    """从 .rpa 归档中读取单个脚本的原始数据（供补丁等只读场景使用）。

    - name 为脚本相对名（如 "screens.rpyc"）；与归档内路径匹配不区分大小写。
    - 若指定名字在归档中不存在，自动回退到同源编译对
      （screens.rpyc ↔ screens.rpy），方便拿任一形式做分析。
    - 多个归档按顺序取第一个命中者。
    - 不物化到磁盘、不解出资源。
    """
    base, ext = name.rsplit(".", 1)
    candidates = {name.lower(), base.lower() + "." + ext.lower()}
    pairs = {"rpyc": "rpy", "rpy": "rpyc", "rpymc": "rpym", "rpym": "rpymc"}
    if ext.lower() in pairs:
        candidates.add(base.lower() + "." + pairs[ext.lower()])
    for rpa in rpa_files:
        try:
            index = parse_rpa_index(rpa)
        except (OSError, ValueError):
            continue
        hit: tuple[int, int, bytes] | None = None
        for idx_name, entry in index.items():
            if idx_name.lower() in candidates:
                hit = entry  # 归档内同名后者覆盖前者
        if hit is not None:
            return _read_entry(Path(rpa), hit)
    return None
