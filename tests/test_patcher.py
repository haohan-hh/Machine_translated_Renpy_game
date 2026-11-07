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
    def test_font_patch_uses_system_font_choice_only(self) -> None:
        generated = patcher._build_font_patch(
            "fonts/cn_font.ttf", "font/original.ttf", [])
        self.assertIn("_zz_font_choices = {'system': _zh_cn_font}", generated)
        self.assertIn("'_zz_font_choice', 'system'", generated)
        self.assertNotIn("微软雅黑", generated)
        self.assertNotIn("汉化默认字体", generated)

    def test_language_list_unions_runtime_api_with_installed_tl_languages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp)
            (game_dir / "tl" / "schinese").mkdir(parents=True)
            (game_dir / "tl" / "None").mkdir()
            code = patcher._LANG_UI_HELPER
            start = code.index("    def _lang_ui_languages():")
            namespace = {
                "config": types.SimpleNamespace(gamedir=str(game_dir)),
                "renpy": types.SimpleNamespace(
                    translation=types.SimpleNamespace(
                        known_languages=lambda: ["english"])),
            }
            exec(textwrap.dedent(code[start:]), namespace)

            self.assertEqual(namespace["_lang_ui_languages"](),
                             [None, "english", "schinese"])

    def test_language_list_falls_back_when_runtime_api_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp)
            (game_dir / "tl" / "schinese").mkdir(parents=True)
            code = patcher._LANG_UI_HELPER
            start = code.index("    def _lang_ui_languages():")
            namespace = {
                "config": types.SimpleNamespace(gamedir=str(game_dir)),
                "renpy": types.SimpleNamespace(
                    translation=types.SimpleNamespace(
                        known_languages=lambda: [])),
            }
            exec(textwrap.dedent(code[start:]), namespace)

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
        self.assertIn('Language(_lang_ui_i)', patched)
        self.assertIn('Function(_lang_ui_note_selection, _lang_ui_i)', patched)
        self.assertIn('timer 0.25 repeat True action Function(_ls_observe_language_switch)', patched)
        self.assertIn(patcher._FONT_UI_MARKER, patched)
        self.assertIn('textbutton "系统字体"', patched)
        self.assertNotIn('微软雅黑', patched)
        self.assertNotIn('汉化默认字体', patched)
        self.assertTrue(patcher._font_ui_is_current(patched))
        self.assertTrue(patcher._language_ui_is_current(patched))
        font_marker_line = next(
            line for line in patched.splitlines()
            if line.strip() == patcher._FONT_UI_MARKER)
        self.assertTrue(font_marker_line.startswith("            "))

    def test_old_font_ui_is_marked_for_rebuild(self) -> None:
        old = (
            "screen preferences():\n"
            "    vbox:\n"
            f"        {patcher._FONT_UI_MARKER}\n"
            "        if _zz_font_choices:\n"
            '            textbutton "汉化默认字体" action NullAction()\n'
            "        if 'yahei' in _zz_font_choices:\n"
            '            textbutton "微软雅黑" action NullAction()\n'
        )
        self.assertFalse(patcher._font_ui_is_current(old))

    def test_old_language_function_action_is_marked_for_rebuild(self) -> None:
        old = (
            "screen zz_language_ui_overlay():\n"
            "    textbutton action Function(_lang_ui_switch, _lang_ui_i)\n"
        )
        self.assertFalse(patcher._language_ui_is_current(old))

    def test_language_picker_inserts_into_custom_viewport_layout(self) -> None:
        source = (
            "screen mm_preferences():\n"
            "    tag menu\n"
            "    viewport xpos 400 ypos 210:\n"
            "        has vbox\n"
            "        hbox:\n"
            "            textbutton 'Rollback Side' action NullAction()\n"
        )
        patched = patcher._build_language_ui(
            source, screen_name="mm_preferences")
        self.assertIsNotNone(patched)
        assert patched is not None
        self.assertIn("screen mm_preferences():", patched)
        self.assertLess(patched.index(patcher._LANG_UI_MARKER),
                        patched.index("textbutton 'Rollback Side'"))
        self.assertIn("        timer 0.25 repeat True", patched)
        self.assertIn("        hbox:\n            style_prefix", patched)

    def test_custom_viewport_properties_do_not_break_layout_health_check(self) -> None:
        source = (
            "screen mm_preferences():\n"
            "    viewport xpos 400 ypos 210:\n"
            "        xysize (1000, 700)\n"
            "        scrollbars \"vertical\"\n"
            "        has vbox\n"
            "        hbox:\n"
            "            text 'Rollback Side'\n"
        )
        patched = patcher._build_language_ui(source, screen_name="mm_preferences")
        self.assertIsNotNone(patched)
        assert patched is not None
        self.assertTrue(patcher._language_ui_is_top_aligned(
            patched, "mm_preferences"))

    def test_custom_font_health_check_allows_decompiler_blank_lines(self) -> None:
        text = (
            "init 999 screen mm_preferences():\n"
            "    viewport:\n"
            "        has vbox\n"
            f"        {patcher._LANG_UI_MARKER}\n"
            f"        {patcher._FONT_UI_MARKER}\n\n"
            "        if _preferences.language == 'schinese':\n\n"
            "            label _(\"Font\")\n\n"
            "            if _zz_font_choices:\n\n"
            "                textbutton \"系统字体\" action NullAction()\n"
        )
        self.assertTrue(patcher._font_ui_is_current(text, "mm_preferences"))

    def test_custom_settings_screens_reached_by_showmenu_are_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "screens.rpy").write_text(
                "screen preferences():\n"
                "    use game_menu('Preferences'):\n"
                "        vbox:\n"
                "            text 'Stock preferences'\n",
                encoding="utf-8")
            (game / "mm_screen.rpy").write_text(
                "screen main_menu():\n"
                "    textbutton 'Options' action ShowMenu('mm_preferences')\n",
                encoding="utf-8")
            (game / "mm_Sections.rpy").write_text(
                "screen mm_preferences():\n"
                "    viewport xpos 400 ypos 210:\n"
                "        has vbox\n"
                "        hbox:\n"
                "            text 'Rollback Side'\n",
                encoding="utf-8")
            (game / "screens_InGame.rpy").write_text(
                "screen quick_menu():\n"
                "    textbutton 'Prefs' action ShowMenu('quick_preferences')\n"
                "screen quick_preferences():\n"
                "    viewport:\n"
                "        has vbox\n"
                "        hbox:\n"
                "            text 'In-game option'\n",
                encoding="utf-8")
            (game / "zz_cn_font.rpy").write_text(
                "_zz_font_choices = {'system': 'fonts/cn.ttf'}\n"
                "def _zz_font_select(choice): pass\n",
                encoding="utf-8")

            result = patcher.apply_language_ui(game, with_font=True)

            self.assertTrue(result.ok, result.message)
            generated = (game / "zz_language_ui.rpy").read_text(
                encoding="utf-8-sig")
            for name in ("preferences", "mm_preferences", "quick_preferences"):
                self.assertIn(f"screen {name}(", generated)
                block = patcher._extract_screen_block(generated, name)
                self.assertIsNotNone(block)
                assert block is not None
                self.assertTrue(patcher._language_ui_is_current(
                    generated, name))
            mm_block = patcher._extract_screen_block(generated, "mm_preferences")
            assert mm_block is not None
            self.assertLess(mm_block.index(patcher._LANG_UI_MARKER),
                            mm_block.index("text 'Rollback Side'"))
            self.assertLess(mm_block.index(patcher._FONT_UI_MARKER),
                            mm_block.index("text 'Rollback Side'"))
            self.assertTrue(patcher._font_ui_is_current(mm_block))

    def test_native_preferences_language_does_not_rebuild_custom_only_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "screens.rpy").write_text(
                "screen preferences():\n"
                "    use game_menu('Preferences'):\n"
                "        vbox:\n"
                "            hbox:\n"
                "                label 'Language'\n"
                "                textbutton '简体中文' action Language('schinese')\n",
                encoding="utf-8")
            (game / "mm_screen.rpy").write_text(
                "screen main_menu():\n"
                "    textbutton 'Options' action ShowMenu('mm_preferences')\n",
                encoding="utf-8")
            (game / "mm_Sections.rpy").write_text(
                "screen mm_preferences():\n"
                "    viewport xpos 400 ypos 210:\n"
                "        has vbox\n"
                "        hbox:\n"
                "            text 'Rollback Side'\n",
                encoding="utf-8")

            first = patcher.apply_language_ui(game, with_font=False)
            self.assertTrue(first.ok, first.message)
            generated = (game / "zz_language_ui.rpy").read_text(
                encoding="utf-8-sig")
            self.assertEqual(
                patcher._count_screen_defs(generated, "preferences"), 0)
            self.assertEqual(
                patcher._count_screen_defs(generated, "mm_preferences"), 1)

            second = patcher.apply_language_ui(game, with_font=False)
            self.assertTrue(second.ok, second.message)
            self.assertTrue(second.skip)

    def test_native_language_ui_font_injection_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "screens.rpy").write_text(
                "screen preferences():\n"
                "    use game_menu('Preferences'):\n"
                "        vbox:\n"
                "            hbox:\n"
                "                label 'Language'\n"
                "                textbutton '简体中文' action Language('schinese')\n",
                encoding="utf-8")
            (game / "zz_cn_font.rpy").write_text(
                "_zz_font_choices = {'system': 'fonts/cn.ttf'}\n"
                "def _zz_font_select(choice): pass\n",
                encoding="utf-8")

            first = patcher.apply_language_ui(game, with_font=True)
            self.assertTrue(first.ok, first.message)
            generated = game / "zz_language_ui.rpy"
            initial_content = generated.read_text(encoding="utf-8-sig")
            self.assertIn("系统字体", initial_content)

            second = patcher.apply_language_ui(game, with_font=True)
            self.assertTrue(second.ok, second.message)
            self.assertTrue(second.skip)
            self.assertEqual(
                generated.read_text(encoding="utf-8-sig"), initial_content)

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
