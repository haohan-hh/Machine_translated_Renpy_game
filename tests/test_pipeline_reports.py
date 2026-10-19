"""External report parsing and import regression tests."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from rpytranslator.pipeline import import_report_texts, parse_external_report


class ExternalReportTests(unittest.TestCase):
    def test_audit_report_preserves_multiline_source_text(self) -> None:
        report = Path("schinese.补漏查缺报告.txt")
        content = (
            "补漏查缺报告\n== mixed ==\n[file.rpy:10]\n"
            "  原文: First line\nsecond line\n  译文: 第一行\n")
        with patch.object(Path, "read_text", return_value=content):
            texts, kind = parse_external_report(report)

        self.assertEqual(texts, ["First line\nsecond line"])
        self.assertIn("补漏查缺报告", kind)

    def test_imported_texts_are_escaped_and_deduplicated(self) -> None:
        game = Path("game")
        writer = mock_open()
        with (patch.object(Path, "is_dir", return_value=False),
              patch.object(Path, "mkdir"),
              patch.object(Path, "is_file", return_value=False),
              patch("builtins.open", writer)):
            added, existing, path = import_report_texts(
                ["First line\nsecond line", "First line\nsecond line"],
                game, "schinese",
            )
        serialized = "".join(call.args[0] for call in writer().write.call_args_list)
        with patch.object(Path, "read_text", return_value=serialized):
            texts, kind = parse_external_report(Path(path))

        self.assertEqual((added, existing), (1, 0))
        self.assertEqual(texts, ["First line\nsecond line"])
        self.assertIn("未翻译报告", kind)


if __name__ == "__main__":
    unittest.main()
