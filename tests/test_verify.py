"""Runtime verification patch generation tests."""
from __future__ import annotations

import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rpytranslator import patcher
from rpytranslator.translator import TranslationError
from rpytranslator.verify import (
    FIX_FILE_NAME,
    LANGUAGE_SWITCH_PATCH_NAME,
    LANGUAGE_SWITCH_PROBE_FILE_NAME,
    LANGUAGE_SWITCH_PROBE_TEXT,
    LANGUAGE_SWITCH_PROBE_TRANSLATION,
    RUNTIME_PATCH_NAME,
    build_missing_fix,
    install_language_switch_probe,
    language_switch_report_path,
    parse_runtime_report,
    read_language_switch_report,
    runtime_report_path,
    write_language_switch_probe,
    write_verification_patch,
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
            self.assertIn("button_requested_language", script)
            self.assertIn("_ls_observe_language_switch", script)
            self.assertIn("ui_click_simulated: False", script)
            self.assertIn("_ls_lang + '.语言切换验证报告.txt'", script)
            self.assertIn(LANGUAGE_SWITCH_PROBE_TEXT, probe.read_text(encoding="utf-8-sig"))

    def test_runtime_monitor_reports_ready_then_observed_language_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            ui_patch = game / "zz_language_ui.rpy"
            (game / "screens.rpy").write_text("screen preferences():\n    text 'Settings'\n")
            patcher.apply_language_ui(game)
            result = install_language_switch_probe(game, "schinese")
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
            helper = ui_patch.read_text(encoding="utf-8-sig").split("\ninit python:\n", 1)[1]
            exec(textwrap.dedent(helper), namespace)
            exec(textwrap.dedent(body), namespace)

            report = language_switch_report_path(game, "schinese")
            self.assertEqual(read_language_switch_report(report)["status"], "WAITING")
            namespace["_ls_observe_language_switch"]()
            self.assertIn("status: READY", report.read_text(encoding="utf-8"))
            self.assertEqual(preferences.language, "english")

            namespace["_lang_ui_note_selection"]("schinese", "preferences")
            preferences.language = "schinese"
            namespace["_ls_observe_language_switch"]()
            contents = report.read_text(encoding="utf-8")
            self.assertIn("status: PASS", contents)
            self.assertEqual(read_language_switch_report(report)["status"], "PASS")
            self.assertEqual(read_language_switch_report(report, "japanese"), {})
            self.assertEqual(read_language_switch_report(report, ui_signature="stale"), {})
            self.assertEqual(read_language_switch_report(report, screen_names=["different"]), {})
            self.assertIn("language_after_switch: 'schinese'", contents)
            self.assertIn("probe_translation_active: '语言切换验证成功'", contents)
            self.assertIn("known_languages: ['schinese']", contents)
            self.assertIn("check_scope: records a requested UI language", contents)
            self.assertEqual(preferences.language, "schinese")

            # A later timer tick must keep the successful result.
            namespace["_ls_observe_language_switch"]()
            self.assertIn("status: PASS", report.read_text(encoding="utf-8"))

            namespace["_lang_ui_note_selection"](None, "preferences")
            preferences.language = None
            namespace["_ls_observe_language_switch"]()
            self.assertEqual(read_language_switch_report(report)["status"], "READY")
            namespace["_lang_ui_note_selection"]("schinese", "preferences")
            for _ in range(4):
                namespace["_ls_observe_language_switch"]()
            self.assertEqual(read_language_switch_report(report)["status"], "FAIL")
            # A later external change cannot rescue the failed button press.
            preferences.language = "schinese"
            namespace["_ls_observe_language_switch"]()
            self.assertEqual(read_language_switch_report(report)["status"], "FAIL")
            exec(textwrap.dedent(body), namespace)
            self.assertEqual(read_language_switch_report(report)["status"], "WAITING")

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

    def test_deferred_language_checks_wait_for_loading_and_use_existing_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "zz_language_ui.rpy").write_text("selected screen")
            tl = game / "tl" / "schinese" / "nested"
            tl.mkdir(parents=True)
            owner = tl / "manual.rpy"
            owner.write_text(f"translate schinese strings:\n    old {LANGUAGE_SWITCH_PROBE_TEXT!r}\n"
                             f"    new {LANGUAGE_SWITCH_PROBE_TRANSLATION!r}\n", encoding="utf-8")
            result = write_language_switch_probe(game, "schinese")
            self.assertEqual(result.files, [game / LANGUAGE_SWITCH_PATCH_NAME])
            prefs = types.SimpleNamespace(language=None)
            api = Mock(return_value=LANGUAGE_SWITCH_PROBE_TRANSLATION)
            runtime = types.SimpleNamespace(known_languages=lambda: {"schinese"},
                                            get_screen=lambda name: True, translate_string=api)
            namespace = {"config": types.SimpleNamespace(gamedir=str(game), defer_tl_scripts=True),
                         "_preferences": prefs, "renpy": runtime}
            script = (game / LANGUAGE_SWITCH_PATCH_NAME).read_text(encoding="utf-8-sig")
            exec(textwrap.dedent(script.split("init 999 python:\n", 1)[1]), namespace)
            report = language_switch_report_path(game, "schinese")
            namespace["_ls_observe_language_switch"]()
            self.assertEqual(read_language_switch_report(report)["status"], "READY")
            api.assert_not_called()
            prefs.language = "schinese"
            namespace["_rpytranslator_language_request"] = (1, "preferences", None, "schinese")
            namespace["_ls_observe_language_switch"]()
            self.assertEqual(read_language_switch_report(report)["status"], "PASS")
            owner.write_text(owner.read_text(encoding="utf-8").replace(LANGUAGE_SWITCH_PROBE_TRANSLATION, "manual"),
                             encoding="utf-8")
            self.assertFalse(write_language_switch_probe(game, "schinese").ok)
            self.assertFalse((tl.parent / LANGUAGE_SWITCH_PROBE_FILE_NAME).exists())

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
                "_rpytranslator_language_request": (0, None, None, None),
            }
            exec(textwrap.dedent(body), namespace)
            namespace["_ls_observe_language_switch"]()
            report = language_switch_report_path(game, "schinese")
            self.assertIn("status: READY", report.read_text(encoding="utf-8"))

            namespace["_rpytranslator_language_request"] = (1, "mm_preferences", "english", "schinese")
            for _ in range(4):
                namespace["_ls_observe_language_switch"]()
            self.assertIn("status: FAIL", report.read_text(encoding="utf-8"))
            self.assertIn("visible_settings_screens: ['mm_preferences']",
                          report.read_text(encoding="utf-8"))

    def test_click_before_first_tick_requires_this_screen_and_exact_loaded_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "zz_language_ui.rpy").write_text("generated fixture")
            write_language_switch_probe(game, "schinese", ["mm_preferences", "quick_preferences"])
            script = (game / LANGUAGE_SWITCH_PATCH_NAME).read_text(encoding="utf-8-sig")
            expected = LANGUAGE_SWITCH_PROBE_TRANSLATION
            table = {"value": expected}
            fake = types.SimpleNamespace(
                known_languages=lambda: {"schinese"},
                get_screen=lambda screen: screen == "mm_preferences",
                translate_string=lambda *a, **kw: table["value"])
            namespace = {"config": types.SimpleNamespace(gamedir=str(game)), "renpy": fake,
                         "_preferences": types.SimpleNamespace(language="schinese")}
            exec(textwrap.dedent(script.split("init 999 python:\n", 1)[1]), namespace)
            check = namespace["_ls_observe_language_switch"]
            result = language_switch_report_path(game, "schinese")
            namespace["_rpytranslator_language_request"] = (1, "quick_preferences", None, "schinese")
            check()
            self.assertEqual(read_language_switch_report(result)["status"], "READY")
            namespace["_rpytranslator_language_request"] = (2, "mm_preferences", None, "schinese")
            check()
            self.assertEqual(read_language_switch_report(result)["status"], "PASS")
            table["value"] = "some other translation"
            check()
            self.assertEqual(read_language_switch_report(result)["status"], "FAIL")
            # An installed folder is not proof that Ren'Py registered it.
            fake.known_languages = lambda: set()
            namespace["_ls_state"]["known_languages"] = None
            check()
            self.assertIn("not registered", read_language_switch_report(result)["error"])


class MissingFixTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.game = Path(tmp.name)
        self.lang = "japanese"
        self.tl = self.game / "tl" / self.lang
        self.tl.mkdir(parents=True)
        self.fix = self.tl / FIX_FILE_NAME
        self.report = runtime_report_path(self.game, self.lang)

    def report_items(self, *rows):
        self.report.write_text("\n".join([
            "total dialogue ids: 10", f"missing {self.lang} translation: {len(rows)}",
            "---- missing list (file:line [who] what id type) ----", *[
                f"D:\\some\\game\\story.rpy:19\t[e]\t{text}\t{key}\tTranslateSay"
                for key, text in rows]]), encoding="utf-8")

    def test_old_report_cannot_duplicate_existing_blocks_or_replace_hand_edits(self) -> None:
        main = self.tl / "chapters" / "story.rpy"
        main.parent.mkdir()
        main.write_text("translate japanese existing_id:\n    $ mood = True\n    e 'Manual'\n\n"
                        "translate japanese reusable_id:\n    # e 'Hello!'\n    e 'こんにちは'\n",
                        encoding="utf-8")
        untouched = "# keep my comment\nlabel keep:\n    pass\n\ntranslate japanese manual_id:\n    $ value = 1\n    e value\n"
        self.fix.write_text("translate japanese existing_id:\n    e 'outdated'\n" + untouched, encoding="utf-8")
        self.report_items(("existing_id", "Already owned."), ("new_id", r"\n Hello!"))
        client = Mock(spec=["translate_texts"])
        first = build_missing_fix(self.game, self.lang, client=client)
        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.files, [self.fix])
        with self.fix.open(encoding="utf-8") as saved:
            content = saved.read()
        self.assertTrue(content.startswith(untouched))
        self.assertNotIn("existing_id", content)
        self.assertIn('e "\\n こんにちは"', content)
        self.assertEqual(parse_runtime_report(self.report)[0].line, 19)
        self.assertTrue(build_missing_fix(self.game, self.lang, client=client).skip)
        client.translate_texts.assert_not_called()

    def test_short_responses_save_only_successes_and_remain_retryable(self) -> None:
        self.report_items(("first", "Good evening."), ("again", "Good evening."), ("last", "Good night."))
        client = Mock(spec=["translate_texts"])
        client.translate_texts.return_value = ["こんばんは。"]
        first = build_missing_fix(self.game, self.lang, client=client, names=["Mio"])
        self.assertFalse(first.ok)
        self.assertIn("1 条待补译", first.message)
        self.assertEqual(first.files, [self.fix])
        client.translate_texts.assert_called_once_with(["Good evening.", "Good night."], target="Japanese", names=["Mio"])
        self.assertNotIn("translate japanese last", self.fix.read_text(encoding="utf-8-sig"))
        client.translate_texts.return_value = ["お休み。"]
        self.assertTrue(build_missing_fix(self.game, self.lang, client).ok)
        client.translate_texts.assert_called_with(["Good night."], target="Japanese", names=[])

    def test_mismatched_truncated_and_complex_reports_do_not_overwrite_the_patch(self) -> None:
        self.report_items(("multi", "First of two."))
        fixture = self.report.read_text(encoding="utf-8")
        self.fix.write_text("# retained\n", encoding="utf-8")
        original = self.fix.read_bytes()
        client = Mock(spec=["translate_texts"])
        cases = (fixture.replace("missing japanese", "missing schinese"),
                 fixture.replace("translation: 1", "translation: 2"),
                 fixture.replace("TranslateSay", "TranslateMultipleSay"))
        for content in cases:
            with self.subTest(content=content):
                self.report.write_text(content, encoding="utf-8")
                self.assertFalse(build_missing_fix(self.game, self.lang, client).ok)
                self.assertEqual(self.fix.read_bytes(), original)
        client.translate_texts.assert_not_called()

    def test_compiled_only_fix_and_conflicting_manual_translations(self) -> None:
        from rpytranslator.tl_files import read_tl_file
        compiled = self.fix.with_suffix(".rpyc")
        compiled.write_bytes(b"existing compiled fix")
        source = self.tl / "hand.rpy"
        source.write_text("translate japanese id_one:\n    # e 'Hello'\n    e 'こんにちは'\n\n"
                          "translate japanese id_two:\n    # e 'Hello'\n    e 'おはよう'\n", encoding="utf-8")
        old = read_tl_file(source, self.lang)
        old.read_only = True
        old.path = compiled
        self.report_items(("missing_id", "Hello"))
        client = Mock(spec=["translate_texts"])
        client.translate_texts.return_value = ["もしもし"]
        with patch("rpytranslator.verify.index_tl_directory", return_value={compiled: old}):
            result = build_missing_fix(self.game, self.lang, client)
        self.assertTrue(result.ok, result.message)
        self.assertFalse(self.fix.exists())
        self.assertEqual(compiled.read_bytes(), b"existing compiled fix")
        self.assertIn("もしもし", result.files[0].read_text(encoding="utf-8-sig"))
        client.translate_texts.assert_called_once_with(["Hello"], target="Japanese", names=[])

    def test_names_and_interpolations_register_without_request_and_keep_whitespace(self) -> None:
        self.report_items(("named", r"\n Mio [player]  "), ("var", "[party[hero].name!q]"), ("sign", "..."))
        client = Mock(spec=["translate_texts"])
        result = build_missing_fix(self.game, self.lang, client, names=["Mio"])
        self.assertTrue(result.ok, result.message)
        self.assertIn("原样保留 3 条", result.message)
        from rpytranslator.tl_files import read_tl_file
        entries = read_tl_file(self.fix, self.lang).entries
        self.assertEqual({e.identifier: e.translation for e in entries},
                         {"named": "\n Mio [player]  ", "var": "[party[hero].name!q]", "sign": "..."})
        self.assertTrue(build_missing_fix(self.game, self.lang, client, names=["Mio"]).skip)
        client.translate_texts.assert_not_called()

    def test_successful_english_and_new_names_do_not_mask_api_failures(self) -> None:
        lang = self.lang = "english"
        path = self.report = runtime_report_path(self.game, lang)
        fix = self.game / "tl" / lang / FIX_FILE_NAME
        self.report_items(("match", "Good day."), ("failed", "Please wait."), ("unknown", "ヴェラ"))
        client = Mock(detected_names=(), failed_texts=set())
        def generate(texts, *, target, names):
            self.assertEqual(target, "English")
            client.detected_names = ("ヴェラ",)
            client.failed_texts.add("Please wait.")
            return texts
        client.translate_texts.side_effect = generate
        result = build_missing_fix(self.game, lang, client)
        self.assertFalse(result.ok)
        self.assertIn("原样保留 2 条", result.message)
        self.assertIn("仍有 1 条待补译", result.message)
        saved = fix.read_text(encoding="utf-8-sig")
        self.assertIn("translate english match:", saved)
        self.assertIn("translate english unknown:", saved)
        self.assertNotIn("translate english failed:", saved)
        client.translate_texts.side_effect = TranslationError("Ollama 未就绪")
        self.assertTrue(path.exists())
        result = build_missing_fix(self.game, lang, client)
        self.assertFalse(result.ok)
        self.assertIn("Ollama 未就绪", result.message)
        self.assertEqual(result.files, [])
        self.assertEqual(fix.read_text(encoding="utf-8-sig"), saved)

    def test_deferred_dialogue_registry_preserves_voice_and_character_attributes(self) -> None:
        say = type("Say", (), {"what": "Hello", "who": "e"})
        voiced = type("Translate", (), {"block": [object(), say()]})()
        formula = type("TranslateSay", (), {"what": "Bye", "attributes": ["sad"]})()
        plain = type("Translate", (), {"block": [say()]})()
        registry = types.SimpleNamespace(default_translates={"voiced": voiced, "attr": formula, "plain": plain},
                                         language_translates={})
        prefs = types.SimpleNamespace(language=None)
        config = types.SimpleNamespace(gamedir=str(self.game), defer_tl_scripts=True, change_language_callbacks=[])
        runtime = types.SimpleNamespace(game=types.SimpleNamespace(script=types.SimpleNamespace(translator=registry)))
        write_verification_patch(self.game, self.lang)
        code = (self.game / RUNTIME_PATCH_NAME).read_text(encoding="utf-8-sig").split("init 999 python:\n", 1)[1]
        exec(textwrap.dedent(code), {"config": config, "_preferences": prefs, "renpy": runtime})
        callback, = config.change_language_callbacks
        callback()
        self.assertFalse(self.report.exists())
        prefs.language = self.lang
        callback()
        self.assertEqual([row.is_say for row in parse_runtime_report(self.report)], [False, False, True])


if __name__ == "__main__":
    unittest.main()
