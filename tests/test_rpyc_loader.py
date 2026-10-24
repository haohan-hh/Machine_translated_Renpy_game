"""Regression coverage for the bundled unrpyc import/decompile path."""
from __future__ import annotations

import unittest
from pathlib import Path

from rpytranslator.rpyc_loader import cleanup, decompile_rpyc_files


class RpycLoaderTests(unittest.TestCase):
    def test_bundled_unrpyc_imports_and_decompiles_fixture(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        fixture = (
            project_root
            / "unrpyc-master"
            / "testcases"
            / "compiled"
            / "tutorial-8.2"
            / "script.rpyc"
        )
        self.assertTrue(fixture.is_file())

        temp_dir, mapping = decompile_rpyc_files([fixture])
        try:
            self.assertIn(fixture, mapping)
            self.assertTrue(mapping[fixture].is_file())
        finally:
            cleanup(temp_dir)
