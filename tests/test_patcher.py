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
        self.assertIn("Function(_lang_ui_note_selection, _lang_ui_i, 'preferences')", patched)
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

    def test_only_fixed_settings_reuse_the_stock_spacer_for_the_added_row(self) -> None:
        source = (
            "screen quick_preferences():\n"
            "    viewport xpos 400 ypos 210:\n"
            "{scroll}"
            "        has vbox\n"
            "        hbox:\n"
            "            null height (4 * gui.pref_spacing)\n"
            "        null height (4 * gui.pref_spacing)  # between groups\n"
            "        bar value Preference('voice volume')\n"
        )
        fixed = patcher._build_language_ui(source.format(scroll=""), screen_name="quick_preferences")
        self.assertIn("        null height gui.pref_spacing  # between groups", fixed)
        self.assertIn("            null height (4 * gui.pref_spacing)", fixed)
        scrollable = patcher._build_language_ui(
            source.format(scroll="        mousewheel True\n"), screen_name="quick_preferences")
        self.assertIn("        null height (4 * gui.pref_spacing)  # between groups", scrollable)

    def test_custom_font_health_check_allows_decompiler_blank_lines(self) -> None:
        text = (
            "init 999 screen mm_preferences():\n"
            "    viewport:\n"
            "        has vbox\n"
            f"        {patcher._LANG_UI_MARKER}\n"
            "        hbox:\n"
            f"            {patcher._FONT_UI_MARKER}\n\n"
            "            if _preferences.language == 'schinese':\n\n"
            "                if _zz_font_choices:\n\n"
            "                    textbutton \"系统字体\" action NullAction()\n"
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
            # Both settings screens must fit the system button alongside the
            # language loop, without adding two rows above their audio sliders.
            for name in ("preferences", "mm_preferences", "quick_preferences"):
                block = patcher._extract_screen_block(generated, name)
                rows = block.splitlines()
                loop = next(line for line in rows if "for _lang_ui_i in " in line)
                marker = next(line for line in rows if line.strip() == patcher._FONT_UI_MARKER)
                self.assertEqual(len(loop) - len(loop.lstrip()), len(marker) - len(marker.lstrip()))
                self.assertNotIn('label _("Font")', block)

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

    def test_only_the_target_action_in_the_settings_screen_counts_as_native(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "screens.rpy").write_text(
                "screen preferences():\n"
                "    vbox:\n"
                "        textbutton 'Other' action Language('chinese')\n"
                "        $ metadata = renpy.known_languages()\n"
                "screen gallery():\n"
                "    textbutton 'Chinese' action Language('schinese')\n", encoding="utf-8")
            self.assertTrue(patcher.apply_language_ui(game, "schinese").ok)
            source = (game / "zz_language_ui.rpy").read_text(encoding="utf-8-sig")
            self.assertEqual(patcher.monitored_settings_screens(source), ["preferences"])

    def test_display_names_respect_nested_owners_and_all_covered_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            lang_dir = game / "tl" / "schinese"
            chapters = lang_dir / "chapters"
            chapters.mkdir(parents=True)
            nested = chapters / "dialogue.rpy"
            keys = tuple(patcher._LANGUAGE_DISPLAY['schinese']) + ('Language',)
            def setup_pairs(words):
                nested.write_text("translate schinese strings:\n" + "".join(
                    f"    old {s!r}\n    new {s!r}\n" for s in words), encoding="utf-8")
            setup_pairs(["Language", "Font"])
            script = lang_dir / "zz_language_display.rpy"
            self.assertTrue(patcher.ensure_language_name(game, 'schinese').ok)
            text = script.read_text(encoding="utf-8-sig")
            self.assertNotIn("'Font'", text)
            self.assertNotIn("'Language'", text)
            setup_pairs(keys)
            self.assertTrue(patcher.ensure_language_name(game, 'schinese').ok)
            self.assertNotIn("translate schinese strings:", script.read_text(encoding="utf-8-sig"))
            self.assertTrue(patcher.ensure_language_name(game, 'schinese').skip)

    def test_disabling_font_and_non_chinese_do_not_inject_chinese_font_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "screens.rpy").write_text("screen preferences():\n    text 'Options'\n")
            (game / "zz_cn_font.rpy").write_text("_zz_font_choices = {}\ndef _zz_font_select(c): pass\n")
            for language, with_font in (("schinese", False), ("japanese", True)):
                with self.subTest(language=language), patch.object(patcher, "apply_font_patch") as font:
                    tl = game / "tl" / language
                    tl.mkdir(parents=True)
                    results = patcher.apply_all(game, language, with_font=with_font)
                    self.assertTrue(all(r.ok for r in results))
                    font.assert_not_called()
                    source = (game / "zz_language_ui.rpy").read_text(encoding="utf-8-sig")
                    self.assertNotIn('textbutton "系统字体"', source)
                    self.assertIn("Language(_lang_ui_i)", source)
                    self.assertEqual((tl / "zz_language_display.rpy").is_file(), language == "schinese")

    def test_archived_ui_uses_effective_patch_and_respects_loose_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            archived = (game / "patch.rpa", "ui/screens.rpy", object())
            menu = b"screen preferences():\n    text 'Archive menu'\n"
            with patch.object(patcher.rpa_loader, "iter_script_entries", return_value=[archived]), \
                    patch.object(patcher.rpa_loader, "read_entry", return_value=menu) as read_entry:
                rows = patcher._settings_screens(game)
                self.assertEqual(rows, [("preferences", menu.decode(), "patch.rpa::ui/screens.rpy")])
                (game / "ui").mkdir()
                (game / "ui" / "screens.rpy").write_text("screen help():\n    pass\n")
                self.assertEqual(patcher._settings_screens(game), [])
                read_entry.assert_called_once_with(archived[0], archived[2])

    def test_override_tracks_source_updates_and_changed_font_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            original = game / "screens.rpy"
            source = "screen preferences():\n    use game_menu('Settings'):\n        vbox:\n            text 'Sound'\n"
            original.write_text(source, encoding="utf-8")
            (game / "zz_cn_font.rpy").write_text("_zz_font_choices = {}\ndef _zz_font_select(x): pass\n")
            patcher.apply_language_ui(game, "schinese", with_font=True)
            original.write_text(source.replace("Sound", "Music"), encoding="utf-8")
            second = patcher.apply_language_ui(game, "tchinese", with_font=True)
            self.assertTrue(second.ok)
            self.assertFalse(second.skip)
            generated = (game / "zz_language_ui.rpy").read_text(encoding="utf-8-sig")
            self.assertIn("text 'Music'", generated)
            self.assertNotIn("text 'Sound'", generated)
            self.assertIn("if _preferences.language == 'tchinese':", generated)
            self.assertNotIn("if _preferences.language == 'schinese':", generated)

    def test_new_native_action_retires_only_the_generated_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            original = game / "screens.rpy"
            original.write_text("screen preferences():\n    text 'Options'\n", encoding="utf-8")
            patcher.apply_language_ui(game)
            override = game / "zz_language_ui.rpy"
            cache = override.with_suffix(".rpyc")
            cache.write_bytes(b"cached previous override")
            keep = game / "screens.rpyc"
            keep.write_bytes(b"original")
            original.write_text("screen preferences():\n    textbutton 'CN' action Language('schinese')\n",
                                encoding="utf-8")
            self.assertTrue(patcher.apply_language_ui(game).ok)
            self.assertFalse(override.exists())
            self.assertFalse(cache.exists())
            self.assertTrue(keep.exists())

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

    def test_menu_inherits_each_block_indent_without_reparenting_game_controls(self) -> None:
        # Roads has an eight-space indent jump after `use` and four after `vbox`.
        # Four spaces are conventional in Ren'Py but are not required at every level.
        for use_indent, box_indent, contents_indent in ((4, 12, 16), (2, 4, 6), (4, 8, 16)):
            with self.subTest(indents=(use_indent, box_indent, contents_indent)):
                src = ("screen preferences():\n" + ' ' * use_indent + "use game_menu('Options'): # settings\n"
                       + '\n' + ' ' * box_indent + "vbox:\n" + ' ' * contents_indent + "xalign .5\n"
                       + ' ' * contents_indent + "hbox: # original game options\n"
                       + ' ' * (contents_indent + 4) + "textbutton 'Window' action NullAction()\n")
                result = patcher._build_language_ui(src, with_font=True)
                box = result.index(' ' * box_indent + 'vbox:')
                row = result.index(' ' * contents_indent + patcher._LANG_UI_MARKER)
                game_controls = result.index(' ' * contents_indent + 'hbox: # original game options')
                self.assertLess(box, row)
                self.assertLess(row, game_controls)
                self.assertIn('\n' + ' ' * contents_indent + 'timer 0.25', result)
                self.assertTrue(patcher._language_ui_is_top_aligned(result))

    def test_font_stays_inside_viewport_before_dedented_screen_siblings(self) -> None:
        src = ("screen preferences():\n  viewport:\n       mousewheel True\n       has vbox\n"
               "       textbutton 'Chinese' action Language('schinese')\n"
               "  textbutton 'Back' action Return()\n")
        generated = patcher._apply_font_ui(src, 'schinese')
        self.assertIn('\n       ' + patcher._FONT_UI_MARKER + '\n', generated)
        self.assertLess(generated.index(patcher._FONT_UI_MARKER), generated.index("textbutton 'Back'"))
        src = src.replace('viewport:\n       mousewheel True\n       has vbox\n',
                          "use game_menu('Options'):\n     vbox:\n")
        generated = patcher._apply_font_ui(src, 'schinese')
        self.assertIn('\n       ' + patcher._FONT_UI_MARKER + '\n', generated)
        self.assertLess(generated.index(patcher._FONT_UI_MARKER), generated.index("textbutton 'Back'"))

    def test_flush_left_comment_between_controls_does_not_drop_remainder_of_menu(self) -> None:
        src = ("screen preferences():\n    vbox:\n        text 'Sound'\n"
               "# Controls below the sound section.\n        text 'Music'\n"
               "# This introduces the next screen.\nscreen help():\n    pass\n")
        generated = patcher._build_language_ui(src)
        self.assertIn("text 'Sound'", generated)
        self.assertIn("text 'Music'", generated)
        self.assertNotIn('introduces the next screen', generated)
        self.assertNotIn('screen help()', generated)

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

    def test_picker_wrapping_original_controls_in_its_hbox_is_unhealthy(self) -> None:
        text = ("screen preferences():\n    use game_menu('Preferences'):\n"
                f"        {patcher._LANG_UI_MARKER}\n        hbox:\n"
                "            style_prefix 'radio'\n            vbox:\n"
                "                text 'Window'\n")
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
