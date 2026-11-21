"""A script speaker and a screen widget may legally have the same spelling."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rpytranslator.extract import RpyExtractor, extract_rpy_files
from rpytranslator.generator import parse_existing_translations, write_translation_files
from rpytranslator.tl_files import read_tl_file


class DialogueContextTests(unittest.TestCase):
    def test_screen_word_is_a_speaker_in_script_and_an_editable_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp) / "game"
            game.mkdir()
            names = game / "speakers.rpy"
            names.write_text('define text = Character(None)\n', encoding="utf-8")
            route = game / "a_route.rpy"
            route.write_text('''init -2 screen radio():
    vbox:
        text "Signal strength"
        python:
            sample = 'a tooltip "inside Python"'
init 3 style phone_text:
    layout "subtitle"
label signal:
    show receiver:
        blend "multiply"
    text "Message received."
''', encoding="utf-8")
            extracted = extract_rpy_files([route, names])
            [line] = extracted.dialogues
            self.assertEqual((line.who, line.what), ("text", "Message received."))
            self.assertEqual([(s.text, s.context) for s in extracted.strings], [("Signal strength", "screen")])

            def write(value, retry=False):
                return write_translation_files([line], [], "schinese", {line.identifier: value}, {},
                                               game, retry_texts={line.what} if retry else None)

            [path] = write("已收到。")
            self.assertEqual(parse_existing_translations(path.parent, "schinese"), ({line.identifier: "已收到。"}, {}))
            self.assertEqual(write("不应覆盖。"), [])
            self.assertEqual(write("消息已收悉。", retry=True), [path])
            [entry] = read_tl_file(path, "schinese").entries
            self.assertEqual((entry.identifier, entry.who, entry.original, entry.translation),
                             (line.identifier, "text", "Message received.", "消息已收悉。"))

    def test_compact_call_has_the_engine_id_and_preserves_all_behavior(self):
        # Expected digest from a game running Ren'Py 8.3.4, independently of
        # the tool's own hash function. The two quotes are speaker styling.
        src = r'''label rhoun15:
    bot "{color=#c5d5d8}''You!''{/color}\n{color=#addf6e}''You!''" (what_prefix='',what_suffix='')
'''
        [line] = RpyExtractor("rreal.rpy").parse(src).dialogues
        self.assertEqual(line.identifier, "rhoun15_f5f1e931")
        self.assertEqual(line.say_code("你！"), '''bot "你！" (what_prefix='', what_suffix='')''')

        source = r'''label exchange:
    e calm @ smile "Come in!" (* extras,tone = pick(")", options[2,3]),** flags,) with Fade(0.1, 0, 0.2) nointeract
'''
        [line] = RpyExtractor("dialogue.rpy").parse(source).dialogues
        self.assertEqual(line.say_code("请进！"), 'e calm @ smile "请进！" nointeract '
                         '(*extras, tone=pick(")", options[2,3]), **flags) with Fade(0.1, 0, 0.2)')

    @unittest.skipUnless(os.environ.get("RENPY_TEST_LAUNCHER"), "Set RENPY_TEST_LAUNCHER to verify game identifiers")
    def test_official_engine_finds_both_types_of_say_without_adding_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            game = project / "game"
            game.mkdir()
            (game / "names.rpy").write_text('define text = Character(None)\n', encoding="utf-8")
            route = game / "route.rpy"
            route.write_text('''init -2 screen inbox():
    text "Inbox history"
label start:
    text "Incoming message."
    text happy @ smile "Second alert." (* extras,tone = pick(")", options[2,3]),** flags,) with Fade(0.1, 0, 0.2) nointeract
    return
''', encoding="utf-8")
            units = extract_rpy_files([route, game / "names.rpy"])
            self.assertEqual(len(units.dialogues), 2)
            translations = {u.identifier: "已收到。" for u in units.dialogues}
            [script] = write_translation_files(units.dialogues, [], "schinese", translations, {}, game)
            env = dict(os.environ, RENPY_PATH_TO_SAVES=str(project / "saves"), RENPY_MULTIPERSISTENT=str(project / "mp"),
                       SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
            result = subprocess.run([os.environ["RENPY_TEST_LAUNCHER"], str(project), "translate", "schinese"],
                                    cwd=project, env=env, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            # Ren'Py would append a new untranslated template for any incorrect ID.
            self.assertEqual({e.identifier: e.translation for e in read_tl_file(script, "schinese").entries if e.identifier},
                             translations)
