"""Reproduce damaged installed translations without touching a real game."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpytranslator.audit import AuditItem, classify, plan_retry_queue, run_audit
from rpytranslator.extract import StringUnit
from rpytranslator.generator import parse_existing_translations
from rpytranslator.glossary import read_game_terms, read_name_groups, required_game_names, save_game_terms
from rpytranslator.pipeline import run_pipeline
from rpytranslator.reports import read_retry_queue, write_retry_entries
from rpytranslator.translator import TranslationClient, TranslationConfig, _ProtectedLine


class GameQualityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.game = Path(tmp.name) / "game"
        self.out = self.game / "tl" / "schinese"
        self.out.mkdir(parents=True)
        self.terms = self.out.parent / ".schinese.专名.json"

    def test_old_and_new_name_suggestions_filter_pronouns_but_not_real_short_names(self):
        source = {"characters": ["You", "Captain Jo", "Scientist A"],
                  "detected": ["He", "I", "IT", "You-", "Jo", "Zoë", "小明"]}
        self.terms.write_text(json.dumps(source), encoding="utf-8")
        groups = read_name_groups(self.game, "schinese")
        self.assertEqual(set(read_game_terms(self.game, "schinese")),
                         {"IT", "Jo", "Zoë", "小明"})
        self.assertEqual(required_game_names(groups["characters"], explicit=("I",)), ("I", "Jo"))
        self.assertEqual(json.loads(self.terms.read_text(encoding="utf-8")), source)
        save_game_terms(self.game, "schinese", source["characters"], source["detected"], groups)
        self.assertEqual(json.loads(self.terms.read_text(encoding="utf-8")), groups)
        source_text = "I met Captain Jo, Zoë and 小明. He came too."
        client = TranslationClient(TranslationConfig(model="qwen3:8b"))
        with patch.object(client, "_chat_with_schema", return_value='["I","He","Captain","Captain Jo","Zoë","小明"]'):
            self.assertEqual(client._extract_qwen3_names([source_text]), ["Jo", "Zoë", "小明"])
        for protected, rendering in [([], "I遇见了他。"), (["I"], "@@n0@@遇见了他。")]:
            line = _ProtectedLine.from_source("I met him.", protected)
            self.assertEqual(line.translation(rendering, "schinese"), "I遇见了他。" if protected else None)

    def test_manual_custom_tags_variables_numerals_and_names_have_distinct_checks(self):
        def category(source, translated, names=()):
            return classify(AuditItem("", "", 0, original=source, translation=translated), "schinese", names)

        self.assertEqual(category("Why?{w} Really?", "{为什么？}{w}{真的吗？}"), "invalid_markup")
        self.assertEqual(category("Hello.", "怎么了{未结束"), "invalid_markup")
        self.assertIsNone(category("See you.", "{wave=slow, twice}再见{/wave}{w}。"))
        self.assertIsNone(category("Look!", "{{看这里！}{#another option?!}"))
        self.assertIsNone(category("Call [Jo].", "呼叫[Jo]。", ("Jo",)))
        self.assertEqual(category("Jo, Jo!", "Jo，等等。", ("Jo",)), "missing_name")
        self.assertIsNone(category("The room in Act I is quiet.", "第 I 幕的房间很静。"))
        self.assertIsNone(category("I arrived in Area I.", "我到了Area I。", ("Area I",)))
        self.assertIsNone(category("Press I to open the bag.", "按 I 打开背包。", ("I",)))
        self.assertEqual(category("I found the door.", "I找到门了。"), "mixed")
        self.assertEqual(category("No, I-{w}", "不，I-{w}"), "mixed")
        self.assertIsNone(category("I think you should press I.", "我想你应该按 I。"))

    def test_historical_keyboard_labels_can_have_stale_lines_without_hiding_other_ui(self):
        (self.game / "screens.rpy").write_text(
            'screen keyboard_help():\n    label _("Enter")\n    label "Ctrl"\n'
            '    label "Shift+A"\n    label "Tab"\nscreen place():\n    textbutton "Enter"\n', encoding="utf-8")
        keys = ("Enter", "Ctrl", "Shift+A", "Tab")
        (self.out / "menu.rpy").write_text('translate schinese strings:\n' + ''.join(
            f'    # game/screens.rpy:33\n    old "{key}"\n    new "{key}"\n' for key in keys), encoding="utf-8")
        result = run_audit(self.game, "schinese")
        self.assertEqual(result.counts, {"untranslated": 1})
        queue = self.out.parent / "schinese.未翻译报告.txt"
        self.assertEqual(read_retry_queue(queue), ["Enter"])
        write_retry_entries(queue, dict.fromkeys(keys, "screens.rpy:33"), {}, manual_texts=["Tab"])
        units = [StringUnit(key, "screens.rpy", 33) for key in keys]
        retry = plan_retry_queue(self.out, "schinese", queue, [], units)
        self.assertEqual(set(retry.strings), {"Enter", "Tab"})

    def test_audit_then_resume_repairs_only_bad_dialogue_ids_and_ignores_bad_checkpoint(self):
        records = {"good": ("I saw Jo.", "我见过Jo。"), "bad": ("I saw Jo.", "I遇见乔。"),
                   "bad_tag": ("Why?{w} How?", "{为什么？}{w}{怎么会？}"),
                   "pure": ("Jo", "乔"), "common": ("You can wait.", "你可以等。"),
                   "pronoun": ("I went home.", "I回家了。")}
        (self.game / "script.rpy").write_text('define j = Character("Jo")\ndefine p = Character("You")\n'
            + "label start:\n" + "".join(f'    "{source}" id {key}\n'
                                            for key, (source, _) in records.items()), encoding="utf-8")
        installed = self.out / "manual.rpy"
        installed.write_text("".join(f'translate schinese {key}:\n    # "{source}"\n    "{translated}"\n'
                                     for key, (source, translated) in records.items())
                             + 'translate schinese strings:\n    old "Jo"\n    new "乔"\n', encoding="utf-8")
        self.terms.write_text(json.dumps({"characters": ["Jo", "You"], "detected": ["I"]}), encoding="utf-8")
        before = installed.read_bytes()
        audited = run_audit(self.game, "schinese")
        self.assertEqual(audited.counts, {"missing_name": 3, "invalid_markup": 1, "mixed": 1})
        self.assertEqual(audited.queued, 4)
        self.assertEqual(installed.read_bytes(), before)
        cache = {"dialogues": {source: translated for source, translated in records.values()},
                 "strings": {"Jo": "乔"}, "protected_terms": ["I", "Jo"]}
        (self.out.parent / ".schinese.汉化进度.json").write_text(json.dumps(cache), encoding="utf-8")
        replies = {"Why?{w} How?": "为什么？{w}怎么会？", "I saw Jo.": "我看见了Jo。", "I went home.": "我回家了。",
                   "You": "你"}

        class Model:
            detected_names, failed_texts, error_count, request_count = (), set(), 0, 0
            seen = []

            def translate_texts(self, texts, *, names, **kwargs):
                self.seen.extend(texts)
                if "I" in names or "You" in names:
                    raise AssertionError(names)
                return [replies[t] for t in texts]

        model = Model()
        result = run_pipeline(self.game.parent, client=model, apply_verification=False,
                              apply_language_ui=False, apply_font_patch=False)
        self.assertTrue(result.ok, result.message)
        self.assertCountEqual(model.seen, replies)
        dialogues, ui = parse_existing_translations(self.out, "schinese")
        self.assertEqual(dialogues, {"good": "我见过Jo。", "bad": replies["I saw Jo."], "pure": "Jo",
                                     "bad_tag": replies["Why?{w} How?"], "common": "你可以等。",
                                     "pronoun": replies["I went home."]})
        self.assertEqual(ui["Jo"], "Jo")
        self.assertEqual(ui["You"], "你")
        self.assertFalse(read_retry_queue(self.out.parent / "schinese.未翻译报告.txt"))
        self.assertNotIn("I", json.loads(self.terms.read_text(encoding="utf-8"))["detected"])


if __name__ == "__main__":
    unittest.main()
