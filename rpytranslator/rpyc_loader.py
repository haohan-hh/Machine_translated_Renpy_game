# -*- coding: utf-8 -*-
"""
.rpyc 编译文件反编译入口：包装捆绑的 unrpyc（v2.0.3），
把 .rpyc 反编译为 .rpy 临时文件供提取器使用，用完即删。

unrpyc 源码随工具一起分发，无需 pip 安装（PyPI 无此包）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# unrpyc-master 位于工具根目录
_UNRPYC_DIR = Path(__file__).resolve().parent.parent / "unrpyc-master"


def _load_unrpyc():
    """把 unrpyc 加入 sys.path 并导入。"""
    if not (_UNRPYC_DIR / "unrpyc.py").is_file():
        raise RuntimeError(f"未找到捆绑的 unrpyc 反编译器: {_UNRPYC_DIR}")
    if str(_UNRPYC_DIR) not in sys.path:
        sys.path.insert(0, str(_UNRPYC_DIR))
    import unrpyc  # noqa: PLC0415
    return unrpyc


def decompile_rpyc_files(
    rpyc_files: list[Path],
    game_dir: Path | None = None,
) -> tuple[Path | None, dict[Path, Path]]:
    """把一组 .rpyc 反编译为临时 .rpy。

    返回 (临时目录, {原始.rpyc路径: 反编译出的.rpy路径})。
    调用方负责在结束后删除临时目录。反编译失败的文件不在映射中。
    """
    if not rpyc_files:
        return None, {}
    unrpyc = _load_unrpyc()
    base = Path(tempfile.mkdtemp(prefix="rpy_decomp_"))
    mapping: dict[Path, Path] = {}
    for f in rpyc_files:
        try:
            if game_dir is not None:
                try:
                    rel = f.relative_to(game_dir)
                except ValueError:
                    rel = Path(f.name)
            else:
                rel = Path(f.name)
            # .rpyc -> .rpymc 对应 .rpym，其余 .rpy
            if f.suffix.lower() == ".rpymc":
                target = rel.with_suffix(".rpym")
            else:
                target = rel.with_suffix(".rpy")
            tmp_in = base / rel
            tmp_in.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, tmp_in)

            ctx = unrpyc.Context()
            unrpyc.decompile_rpyc(tmp_in, ctx)
            out = base / target
            if out.is_file():
                mapping[f] = out
        except Exception:
            # 单个文件失败不影响整体
            continue
    return base, mapping


def cleanup(tmp_dir: Path | None):
    """删除反编译临时目录。"""
    if tmp_dir is not None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
