"""Regression coverage for post-processing source discovery."""
from __future__ import annotations

import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from rpytranslator import patcher


class PatcherTests(unittest.TestCase):
    def test_language_list_unions_runtime_api_with_installed_tl_languages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp)
            (game_dir / "tl" / "schinese").mkdir(parents=True)
            (game_dir / "tl" / "None").mkdir()
            code = patcher._LANG_UI_HELPER
            start = code.index("    def _lang_ui_languages():")
            end = code.index("\n    def _lang_ui_switch", start)
            namespace = {
                "config": types.SimpleNamespace(gamedir=str(game_dir)),
                "renpy": types.SimpleNamespace(
                    translation=types.SimpleNamespace(
                        known_languages=lambda: ["english"])),
            }
            exec(textwrap.dedent(code[start:end]), namespace)

            self.assertEqual(namespace["_lang_ui_languages"](),
                             [None, "english", "schinese"])

    def test_language_list_falls_back_when_runtime_api_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp)
            (game_dir / "tl" / "schinese").mkdir(parents=True)
            code = patcher._LANG_UI_HELPER
            start = code.index("    def _lang_ui_languages():")
            end = code.index("\n    def _lang_ui_switch", start)
            namespace = {
                "config": types.SimpleNamespace(gamedir=str(game_dir)),
                "renpy": types.SimpleNamespace(
                    translation=types.SimpleNamespace(
                        known_languages=lambda: [])),
            }
            exec(textwrap.dedent(code[start:end]), namespace)

            self.assertEqual(namespace["_lang_ui_languages"](),
                             [None, "schinese"])

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

    def test_raise_preferences_priority_overrides_archived_screen(self) -> None:
        source = "init -501 screen preferences():\n    tag menu\n"
        patched = patcher._raise_preferences_priority(source)
        self.assertTrue(patched.startswith(
            f"init {patcher._LANGUAGE_UI_PRIORITY} screen preferences():"))

    def test_raise_preferences_priority_handles_plain_screen(self) -> None:
        source = "screen preferences():\n    tag menu\n"
        patched = patcher._raise_preferences_priority(source)
        self.assertTrue(patched.startswith(
            f"init {patcher._LANGUAGE_UI_PRIORITY} screen preferences():"))

    def test_language_picker_priority_uses_supported_renpy_range(self) -> None:
        self.assertGreaterEqual(patcher._LANGUAGE_UI_PRIORITY, -999)
        self.assertLessEqual(patcher._LANGUAGE_UI_PRIORITY, 999)

    def test_language_picker_is_inserted_before_fixed_height_settings_content(self) -> None:
        source = (
            "screen preferences():\n"
            "    tag menu\n"
            '    use game_menu(_("Preferences"), scroll="viewport"):\n'
            "        vbox:\n"
            '            textbutton _("Window")\n'
        )

        patched = patcher._build_language_ui(source, with_font=True)

        self.assertIsNotNone(patched)
        root_vbox = patched.index("        vbox:")
        marker = patched.index(patcher._LANG_UI_MARKER)
        existing_content = patched.index('            textbutton _("Window")')
        self.assertLess(root_vbox, marker)
        self.assertLess(marker, existing_content)
        self.assertIn("            hbox:\n                style_prefix \"radio\"", patched)
        self.assertIn('_lang_ui_display_name(_lang_ui_i)', patched)
        self.assertIn(patcher._FONT_UI_MARKER, patched)
        font_marker_line = next(
            line for line in patched.splitlines()
            if line.strip() == patcher._FONT_UI_MARKER)
        self.assertTrue(font_marker_line.startswith("            "))

    def test_language_picker_finds_root_vbox_after_comments(self) -> None:
        source = (
            "screen preferences():\n"
            "    tag menu\n"
            '    use game_menu(_("Preferences"), scroll="viewport"):\n'
            "        # The game menu transcludes its content below.\n"
            "        vbox:\n"
            '            textbutton _("Window")\n'
        )

        patched = patcher._build_language_ui(source)

        self.assertIsNotNone(patched)
        lines = patched.splitlines()
        root_vbox = next(i for i, line in enumerate(lines)
                         if line.strip() == "vbox:")
        marker = next(i for i, line in enumerate(lines)
                      if line.strip() == patcher._LANG_UI_MARKER)
        self.assertLess(root_vbox, marker)
        self.assertTrue(lines[marker].startswith("            "))
        self.assertLess(marker, next(i for i, line in enumerate(lines)
                                     if 'textbutton _("Window")' in line))
        self.assertTrue(patcher._language_ui_is_top_aligned(patched))

    def test_language_picker_sibling_to_existing_root_vbox_is_unhealthy(self) -> None:
        text = (
            f"init {patcher._LANGUAGE_UI_PRIORITY} screen preferences():\n"
            "    tag menu\n"
            '    use game_menu(_("Preferences"), scroll="viewport"):\n'
            "        # ===== 语言切换（汉化工具自动生成） =====\n"
            "        hbox:\n"
            '            textbutton _lang_ui_display_name(_lang_ui_i)\n'
            "        vbox:\n"
            '            textbutton _("Window")\n'
        )
        self.assertFalse(patcher._language_ui_is_top_aligned(text))

    def test_old_bottom_aligned_language_picker_is_not_considered_healthy(self) -> None:
        text = (
            f"init {patcher._LANGUAGE_UI_PRIORITY} screen preferences():\n"
            "    tag menu\n"
            '    use game_menu(_("Preferences"), scroll="viewport"):\n'
            "        vbox:\n"
            '            textbutton _("Window")\n'
            "        # ===== 语言切换（汉化工具自动生成） =====\n"
            "        vbox:\n"
            '            textbutton _lang_ui_display_name(_lang_ui_i)\n'
        )
        self.assertFalse(patcher._language_ui_is_top_aligned(text))


if __name__ == "__main__":
    unittest.main()
