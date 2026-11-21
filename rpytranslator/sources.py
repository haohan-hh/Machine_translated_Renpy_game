"""Collect one script per Ren'Py path and extract while temporary files still exist."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from . import engine, rpa_loader
from .extract import ExtractionResult, extract_rpy_files
from .rpyc_loader import cleanup, decompile_rpyc_files


def extract_game_scripts(info: engine.GameInfo, log) -> ExtractionResult:
    game = info.game_dir
    assert game is not None
    # Each value retains its origin so that decompiling routes/a/day.rpyc and
    # routes/b/day.rpyc never flattens either route to day.rpy.
    scripts: dict[str, tuple[Path, Path]] = {}

    def collect(paths, root):
        for path in paths:
            rel = path.relative_to(root)
            scripts[rpa_loader.script_key(rel.as_posix())] = (path, root)

    with ExitStack() as stack:
        # A loose script overrides the archived copy. On disk the engine
        # recompiles a changed .rpy; reading the loose source also avoids a
        # redundant decompilation when both copies match.
        collect(info.rpyc_files, game)
        collect(info.rpy_files, game)
        if info.rpa_files:
            archive = Path(stack.enter_context(TemporaryDirectory(prefix="rpy_archive_")))
            paths = rpa_loader.extract_all_scripts_to(
                info.rpa_files, archive, include=lambda name: (
                    engine.is_game_script(name) and rpa_loader.script_key(name) not in scripts
                ))
            collect(paths, archive)

        files: dict[Path, Path] = {}
        compiled: dict[Path, list[Path]] = {}
        for path, root in scripts.values():
            if path.suffix.lower() in engine.COMPILED_EXTS:
                compiled.setdefault(root, []).append(path)
            else:
                files[path.relative_to(root)] = path
        failed: list[str] = []
        for root, paths in compiled.items():
            log(f"正在反编译 {len(paths)} 个脚本…")
            tmp, mapping = decompile_rpyc_files(paths, root)
            stack.callback(cleanup, tmp)
            for path in paths:
                rel = path.relative_to(root)
                if path in mapping:
                    files[rel.with_suffix(rel.suffix.lower()[:-1])] = mapping[path]
                else:
                    failed.append(f"无法反编译 {rel.as_posix()}")
        ordered = sorted(files, key=lambda p: p.as_posix())
        extracted = extract_rpy_files([files[rel] for rel in ordered])
        relative_names = {str(files[rel]): rel.as_posix() for rel in ordered}
        for unit in (*extracted.dialogues, *extracted.strings):
            unit.filename = relative_names[unit.filename]
        extracted.files.clear()
        extracted.skipped.extend(failed)
        return extracted
