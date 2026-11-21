"""A hand translation stays in its own scene; copying it to another must be safe."""
import tempfile
import unittest
from pathlib import Path

from rpytranslator.audit import reusable_dialogue
from rpytranslator.generator import parse_existing_translations
from rpytranslator.pipeline import run_pipeline
from rpytranslator.verify import build_missing_fix, runtime_report_path


class DialogueReuseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.game = Path(tmp.name) / "game"
        self.out = self.game / "tl" / "schinese"
        self.out.mkdir(parents=True)
        self.bad, self.styled = "Are you okay?", "Take care."
        self.bad_answer, self.good_answer = "{你还好吗？}", "{wave=slow, twice}保重。{/wave}"
        self.owner = self.out / "manual.rpy"
        self.hand = (f'translate schinese old_12345678:\n    # "{self.bad}"\n    "{self.bad_answer}"\n'
                     f'translate schinese old_styled:\n    # "{self.styled}"\n    "{self.good_answer}"\n').encode()
        self.owner.write_bytes(self.hand)

    def test_static_text_and_digest_fallbacks_do_not_spread_broken_hand_translations(self):
        (self.game / "story.rpy").write_text('label start:\n' + ''.join(
            f'    "{text}" id {key}\n' for text, key in (
                (self.bad, "old_12345678"), (self.styled, "old_styled"),
                (self.bad, "new_text"), (self.bad, "new_12345678"), (self.styled, "new_styled"))), encoding="utf-8")

        class Model:
            detected_names, failed_texts, request_count, error_count = (), set(), 0, 0
            seen = []

            def translate_texts(self, texts, **_):
                self.seen.extend(texts)
                return ["你还好吗？" for _ in texts]

        model = Model()
        result = run_pipeline(self.game.parent, client=model, apply_font_patch=False,
                              apply_language_ui=False, apply_verification=False)
        self.assertEqual(result.audit.counts, {"invalid_markup": 1})  # original hand edit only
        self.assertEqual(model.seen, [self.bad])
        written, _ = parse_existing_translations(self.out, "schinese")
        self.assertEqual((written['new_text'], written['new_12345678'], written['new_styled']),
                         ("你还好吗？", "你还好吗？", self.good_answer))
        self.assertEqual(self.owner.read_bytes(), self.hand)

    def test_runtime_misses_reuse_custom_tags_but_wait_for_a_valid_sentence(self):
        report = runtime_report_path(self.game, "schinese")
        report.write_text(f"total dialogue ids: 4\nmissing schinese translation: 2\n---- missing list ----\n"
                          f"story.rpy:4\t[]\t{self.bad}\tnew_question\tTranslateSay\n"
                          f"story.rpy:5\t[]\t{self.styled}\tnew_farewell\tTranslateSay\n", encoding="utf-8")
        result = build_missing_fix(self.game)
        self.assertFalse(result.ok)
        self.assertIn("仍有 1 条待补译", result.message)
        written, _ = parse_existing_translations(self.out, "schinese")
        self.assertEqual(written['new_farewell'], self.good_answer)
        self.assertNotIn('new_question', written)

        class Model:
            def translate_texts(_, texts, **kwargs):
                self.assertEqual(texts, [self.bad])
                return ["你还好吗？"]

        resumed = build_missing_fix(self.game, client=Model())
        self.assertTrue(resumed.ok, resumed.message)
        self.assertEqual(parse_existing_translations(self.out, "schinese")[0]['new_question'], "你还好吗？")
        self.assertEqual(self.owner.read_bytes(), self.hand)

    def test_missing_variables_waits_names_and_new_substitutions_are_never_borrowed(self):
        source = "Jo, wait{w} for [who]."
        for candidate in ("Jo，等[who]。", "Jo，{w}等她。", "乔，{w}等[who]。", "Jo，{w}等[who]，[invented]。",
                          "Jo，等[who]{w}。", "Jo，{w}等[who][who]。"):
            with self.subTest(candidate=candidate):
                self.assertFalse(reusable_dialogue(source, candidate, 'schinese', ['Jo']))
        self.assertTrue(reusable_dialogue(source, "Jo，{wave}{w}等等{/wave}[who]。", 'schinese', ['Jo']))


if __name__ == "__main__":
    unittest.main()
