"""Verify that archive lookup, source discovery and translation extract the same scripts."""
from __future__ import annotations

import pickle
import tempfile
import unittest
import zlib
from pathlib import Path

from rpytranslator import engine, rpa_loader
from rpytranslator.sources import extract_game_scripts


def write_rpa(path, files, version=3):
    """A bytes member is one block; a list of (prefix or None, bytes) can split a member."""
    key = 0x12345678 if version == 3 else 0

    def header(offset):
        key_part = f" {key:08x}" if version == 3 else ""
        return f"RPA-{version}.0 {offset:016x}{key_part}\n".encode("ascii")

    archive = bytearray(header(0))
    index = {}
    for name, chunks in files.items():
        if isinstance(chunks, bytes):
            chunks = [(None, chunks)]
        entries = index[name] = []
        for prefix, data in chunks:
            entry = (len(archive) ^ key, len(data) ^ key)
            entries.append(entry if prefix is None else entry + (prefix,))
            archive.extend(data + b"\x00")
    start = len(archive)
    archive[:len(header(0))] = header(start)
    archive.extend(zlib.compress(pickle.dumps(index, protocol=2)))
    path.write_bytes(archive)
    return path


class SourceDiscoveryTests(unittest.TestCase):
    def test_prefix_and_legacy_chunks_are_fully_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            for version in (2, 3):
                with self.subTest(version=version):
                    rpa = write_rpa(Path(tmp) / "game.rpa", {
                        "single.rpyc": [(b"\x80\xff", b"payload")],
                        "split.rpy": [(None, b"one"), (None, b"two"), (None, b"three")],
                    }, version)
                    self.assertEqual(rpa_loader.read_script_data([rpa], "single.rpyc"), b"\x80\xffpayload")
                    self.assertEqual(rpa_loader.read_script_data([rpa], "split.rpy"), b"onetwothree")

    def test_archive_priority_and_precise_extension_apply_across_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = write_rpa(root / "base.rpa", {
                "scenes/intro.rpyc": b"old", "screens.rpyc": b"compiled",
                "source_only.rpy": b"source only",
            })
            patch = write_rpa(root / "patch.rpa", {
                "scenes/intro.rpyc": b"new", "screens.rpy": b"source",
                "scenes/intro.rpymc": b"module", "other/intro.rpyc": b"other route",
            })
            rpas = rpa_loader.find_archives(root)
            self.assertEqual(rpas, [patch, base])
            self.assertEqual(rpa_loader.read_script_data(rpas, "SCENES\\Intro.rpyc"), b"new")
            self.assertEqual(rpa_loader.read_script_data(rpas, "screens.rpyc"), b"compiled")
            self.assertIsNone(rpa_loader.read_script_data(rpas, "source_only.rpyc", allow_alternate=False))
            self.assertEqual(rpa_loader.read_script_data(rpas, "source_only.rpyc", allow_alternate=True),
                             b"source only")
            dest = root / "out"
            files = rpa_loader.extract_all_scripts_to(rpas, dest)
            self.assertEqual({p.relative_to(dest).as_posix(): p.read_bytes() for p in files}, {
                "scenes/intro.rpyc": b"new", "scenes/intro.rpymc": b"module",
                "other/intro.rpyc": b"other route", "screens.rpyc": b"compiled",
                "source_only.rpy": b"source only",
            })
            self.assertEqual(len(files), 5)

    def test_extraction_keeps_routes_modules_and_loose_overrides(self):
        fixture = (Path(__file__).resolve().parents[1] / "unrpyc-master" / "testcases" /
                   "compiled" / "tutorial-8.2" / "tutorial_nvlmode.rpyc").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "game"
            game.mkdir()
            write_rpa(game / "base.rpa", {
                "routes/a/day.rpyc": b"shadowed by a loose source",
                "routes/b/day.rpyc": fixture,
                "routes/c/day.rpyc": fixture,
                "routes/a/day.rpym": b'label module:\n    "Module dialogue."\n',
                "tl/schinese/script.rpyc": b"archived translation",
                "cache/ghost.rpyc": b"cache", "zz_language_ui.rpyc": b"injected patch",
            })
            loose = game / "routes" / "a" / "day.rpy"
            loose.parent.mkdir(parents=True)
            loose.write_text('label day:\n    "Loose dialogue."\n', encoding="utf-8")
            info = engine.scan_game(game)
            self.assertEqual(info.text_count, 4)
            self.assertTrue(info.has_chinese)
            result = extract_game_scripts(info, lambda _: None)
            by_file = result.dialogues_by_file()
            self.assertEqual(set(by_file), {
                "routes/a/day.rpy", "routes/b/day.rpy", "routes/c/day.rpy", "routes/a/day.rpym",
            })
            self.assertEqual([u.what for u in by_file["routes/a/day.rpy"]], ["Loose dialogue."])
            self.assertEqual([u.what for u in by_file["routes/a/day.rpym"]], ["Module dialogue."])
            for route in ("b", "c"):
                self.assertTrue(any("NVL-style games" in u.what for u in by_file[f"routes/{route}/day.rpy"]))
            self.assertFalse(any("无法反编译" in reason for reason in result.skipped))
