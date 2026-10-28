"""Regression coverage for post-processing source discovery."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpytranslator import patcher


class PatcherTests(unittest.TestCase):
    def test_find_preferences_source_decompiles_root_screens_rpyc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp)
            compiled = game_dir / "screens.rpyc"
            compiled.write_bytes(b"compiled screens fixture")
            source = "screen preferences():\n    pass\n"

            with patch.object(
                patcher, "_decompile_rpyc_source", return_value=source
            ) as decompile:
                found_text, found_path = patcher._find_preferences_source(game_dir)

            self.assertEqual(found_text, source)
            self.assertEqual(found_path, compiled)
            decompile.assert_called_once_with(compiled, "screens.rpyc")


if __name__ == "__main__":
    unittest.main()
