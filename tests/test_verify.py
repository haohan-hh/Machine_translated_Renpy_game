"""Runtime verification patch generation tests."""
from __future__ import annotations

import tempfile
import textwrap
import types
import unittest
from pathlib import Path

from rpytranslator.verify import (
    LANGUAGE_SWITCH_PATCH_NAME,
    LANGUAGE_SWITCH_PROBE_FILE_NAME,
    LANGUAGE_SWITCH_PROBE_TEXT,
    LANGUAGE_SWITCH_REPORT_TEMPLATE,
    language_switch_report_path,
    write_language_switch_probe,
)


class LanguageSwitchProbeTests(unittest.TestCase):
    def test_probe_writes_runtime_script_and_translation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            result = write_language_switch_probe(game, "schinese")

            self.assertTrue(result.ok)
            self.assertFalse(result.skip)
            patch = game / LANGUAGE_SWITCH_PATCH_NAME
            probe = game / "tl" / "schinese" / LANGUAGE_SWITCH_PROBE_FILE_NAME
            self.assertTrue(patch.is_file())
            self.assertTrue(probe.is_file())
            script = patch.read_text(encoding="utf-8-sig")
            self.assertNotIn("renpy.change_language", script)
            self.assertIn("renpy.translate_string", script)
            self.assertIn("known_languages", script)
            self.assertIn("renpy.get_screen(_ls_name)", script)
            self.assertIn("Language(_lang_ui_i)", script)
            self.assertIn("button_requested_language", script)
            self.assertIn("_ls_observe_language_switch", script)
            self.assertIn("ui_click_simulated: False", script)
            self.assertIn("_ls_lang + '.语言切换验证报告.txt'", script)
            self.assertIn(LANGUAGE_SWITCH_PROBE_TEXT, probe.read_text(encoding="utf-8-sig"))

    def test_runtime_monitor_reports_ready_then_observed_language_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            ui_patch = game / "zz_language_ui.rpy"
            ui_patch.write_text(
                "screen preferences():\n"
                "    textbutton action [Function(_lang_ui_note_selection, _lang_ui_i), Language(_lang_ui_i)]\n"
                "    timer 0.25 repeat True action Function(_ls_observe_language_switch)\n",
                encoding="utf-8",
            )
            result = write_language_switch_probe(game, "schinese")
            self.assertTrue(result.ok)
            (game / "tl" / "schinese.未翻译报告.txt").write_text(
                "report metadata, not a language directory", encoding="utf-8")
            script = (game / LANGUAGE_SWITCH_PATCH_NAME).read_text(
                encoding="utf-8-sig")
            body = script.split("init 999 python:\n", 1)[1]

            class FakeRenPy:
                def __init__(self, preferences):
                    self.preferences = preferences

                def known_languages(self):
                    return {"schinese", None}

                def get_screen(self, name):
                    return object() if name == "preferences" else None

                def translate_string(self, value, language=None):
                    active = language or self.preferences.language
                    return "语言切换验证成功" if active == "schinese" else value

            preferences = types.SimpleNamespace(language="english")
            fake_renpy = FakeRenPy(preferences)
            namespace = {
                "config": types.SimpleNamespace(
                    gamedir=str(game),
                    overlay_screens=["zz_language_ui_overlay"],
                ),
                "renpy": fake_renpy,
                "_preferences": preferences,
            }
            exec(textwrap.dedent(body), namespace)

            namespace["_ls_observe_language_switch"]()
            report = language_switch_report_path(game, "schinese")
            self.assertIn("status: READY", report.read_text(encoding="utf-8"))
            self.assertEqual(preferences.language, "english")

            preferences.language = "schinese"
            namespace["_rpytranslator_requested_language"] = "schinese"
            namespace["_ls_observe_language_switch"]()
            contents = report.read_text(encoding="utf-8")
            self.assertIn("status: PASS", contents)
            self.assertIn("language_after_switch: 'schinese'", contents)
            self.assertIn("probe_translation_active: '语言切换验证成功'", contents)
            self.assertIn("known_languages: ['schinese']", contents)
            self.assertIn("check_scope: records a requested UI language", contents)
            self.assertEqual(preferences.language, "schinese")

            # A later timer tick must keep the successful result.
            namespace["_ls_observe_language_switch"]()
            self.assertIn("status: PASS", report.read_text(encoding="utf-8"))

    def test_probe_generation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            first = write_language_switch_probe(game, "schinese")
            second = write_language_switch_probe(game, "schinese")

            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertTrue(second.skip)
            self.assertEqual(second.files, [])
            self.assertEqual(
                language_switch_report_path(game, "schinese"),
                game / "tl" / "schinese.语言切换验证报告.txt",
            )

    def test_probe_uses_translation_api_fallback_and_custom_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "zz_language_ui.rpy").write_text(
                "screen mm_preferences():\n"
                "    textbutton action [Function(_lang_ui_note_selection, _lang_ui_i), Language(_lang_ui_i)]\n"
                "    timer 0.25 repeat True action Function(_ls_observe_language_switch)\n",
                encoding="utf-8")
            write_language_switch_probe(
                game, "schinese", screen_names=["mm_preferences"])
            script = (game / LANGUAGE_SWITCH_PATCH_NAME).read_text(
                encoding="utf-8-sig")
            body = script.split("init 999 python:\n", 1)[1]

            class LegacyRenPy:
                translation = types.SimpleNamespace(
                    get_languages=lambda: ["schinese"])
                preferences = types.SimpleNamespace(language="english")

                @staticmethod
                def get_screen(name):
                    return object() if name == "mm_preferences" else None

                @staticmethod
                def translate_string(value, language=None):
                    return "语言切换验证成功" if language == "schinese" else value

            prefs = LegacyRenPy.preferences
            namespace = {
                "config": types.SimpleNamespace(gamedir=str(game)),
                "renpy": LegacyRenPy,
                "_preferences": prefs,
                "_rpytranslator_requested_language": None,
            }
            exec(textwrap.dedent(body), namespace)
            namespace["_ls_observe_language_switch"]()
            report = language_switch_report_path(game, "schinese")
            self.assertIn("status: READY", report.read_text(encoding="utf-8"))

            namespace["_rpytranslator_requested_language"] = "schinese"
            for _ in range(4):
                namespace["_ls_observe_language_switch"]()
            self.assertIn("status: FAIL", report.read_text(encoding="utf-8"))
            self.assertIn("visible_settings_screens: ['mm_preferences']",
                          report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
