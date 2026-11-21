"""Exercise the user's scan → merge → translate → audit workflow on disk."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rpytranslator.audit import run_audit
from rpytranslator.generator import parse_existing_translations
from rpytranslator.pipeline import run_pipeline
from rpytranslator.reports import read_retry_entries
from rpytranslator.translator import PauseRequested
from rpytranslator.verify import FIX_FILE_NAME, runtime_report_path


class RecordingModel:
    """Replace just the model call while leaving extraction, files and reports real."""

    def __init__(self, replies=(), *, failures=(), after_item=None):
        self.replies = dict(replies)
        self.failed_texts = set(failures)
        self.after_item = after_item
        self.calls = []
        self.seen_names = []
        self.targets = []
        self.detected_names = ()
        self.error_count = 0

    @property
    def request_count(self):
        return len(self.calls)

    def translate_texts(self, texts, *, target, names, item_cb, **kwargs):
        self.calls.append(tuple(texts))
        self.seen_names.append(tuple(names))
        self.targets.append(target)
        values = []
        for text in texts:
            if text in self.failed_texts:
                self.error_count += 1
                value = text
            else:
                value = self.replies[text]  # Unexpected requests must fail the test.
            item_cb(text, value)
            if self.after_item:
                self.after_item(self, text)
            values.append(value)
        return values


class PipelineWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.game = Path(temp.name) / "game"
        self.out = self.game / "tl" / "schinese"
        self.out.mkdir(parents=True)
        self.story = self.game / "story.rpy"
        self.queue = self.out.parent / "schinese.未翻译报告.txt"

    def translate(self, model=None, language="schinese", **kwargs):
        return run_pipeline(self.game.parent, client=model, language=language,
                            apply_font_patch=False, apply_language_ui=False,
                            apply_verification=False, **kwargs)

    def test_one_new_line_preserves_handwriting_then_restart_is_offline_and_does_not_rewrite(self):
        self.story.write_text(
            'label start:\n    "The light is on." id lamp\n    "The sea is calm." id sea\n'
            'screen map_screen():\n    text "Map"\n', encoding="utf-8")
        existing = self.out / "story.rpy"
        original = (
            '# 手工校对\r\ntranslate schinese lamp:\r\n'
            '    # "The light is on." id lamp\r\n    "灯已经亮了。" id lamp  # 保留备注\r\n'
            'translate schinese strings:\r\n    old "Map"\r\n    new "航海图"\r\n'
        ).encode("utf-8-sig")
        existing.write_bytes(original)
        client = RecordingModel({"The sea is calm.": "海面平静。"})
        first = self.translate(client)
        self.assertTrue(first.ok, first.message)
        self.assertEqual(client.calls, [("The sea is calm.",)])
        self.assertEqual(first.output_files, [existing])
        data = existing.read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual(parse_existing_translations(self.out, "schinese"),
                         ({"lamp": "灯已经亮了。", "sea": "海面平静。"}, {"Map": "航海图"}))

        os.utime(existing, (946684800, 946684800))
        last_write = existing.stat().st_mtime_ns
        with (patch("rpytranslator.pipeline.prepare_local_model") as prepare,
              patch("rpytranslator.translator.TranslationClient.chat") as chat):
            second = self.translate()
        self.assertTrue(second.ok, second.message)
        prepare.assert_not_called()
        chat.assert_not_called()
        self.assertEqual(second.output_files, [])
        self.assertEqual((existing.read_bytes(), existing.stat().st_mtime_ns), (data, last_write))

    def test_failed_repair_keeps_installed_translation_until_next_success(self):
        source = "Close the gate."
        self.story.write_text(f'label start:\n    "{source}" id gate\n', encoding="utf-8")
        hand = self.out / "custom.rpy"
        original = (f'# 人工翻译\ntranslate schinese gate:\n    # "{source}" id gate\n'
                    '    "把门关好。" id gate  # 人工备注\n').encode("utf-8")
        hand.write_bytes(original)
        self.queue.write_text(f"story.rpy:2  {source}\n", encoding="utf-8")
        failed = RecordingModel(failures=[source])
        with patch("rpytranslator.pipeline.time.sleep"):
            first = self.translate(failed)
        self.assertFalse(first.ok)
        self.assertGreater(failed.error_count, 0)
        self.assertEqual(first.output_files, [])
        self.assertEqual(hand.read_bytes(), original)
        self.assertEqual(read_retry_entries(self.queue), {source: "story.rpy:2"})

        fixed = self.translate(RecordingModel({source: "关上大门。"}))
        self.assertTrue(fixed.ok, fixed.message)
        self.assertEqual(hand.read_bytes(), original.replace("把门关好。".encode(), "关上大门。".encode()))
        self.assertFalse(self.queue.exists())

    def test_file_count_includes_runtime_and_audit_writes_once_then_returns_to_zero(self):
        self.story.write_text(
            'label start:\n    "The wind stopped." id wind\n    "Look outside." id view\n'
            '    "Take the map." id map\n', encoding="utf-8")
        manual = self.out / "custom.rpy"
        manual.write_text(
            'translate schinese wind:\n    # "The wind stopped."\n    "風停了。"\n\n'
            'translate schinese view:\n    # "Look outside."\n    "看看外面。"\n', encoding="utf-8")
        runtime_report_path(self.game, "schinese").write_text(
            "total dialogue ids: 4\nmissing schinese translation: 1\n---- missing list ----\n"
            "story.rpy:9\t[]\tLook outside.\twindow_view\tTranslateSay\n", encoding="utf-8")

        first = self.translate(RecordingModel({"Take the map.": "拿起地圖。"}))
        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.audit.fixed_traditional, 2)
        self.assertCountEqual(first.audit.fixed_files, [manual, self.out / "story.rpy"])
        self.assertCountEqual(first.output_files, [manual, self.out / "story.rpy", self.out / FIX_FILE_NAME])
        self.assertIn("写入 3 个译文文件", first.message)
        self.assertEqual(parse_existing_translations(self.out, "schinese")[0],
                         {"wind": "风停了。", "view": "看看外面。", "map": "拿起地图。", "window_view": "看看外面。"})
        second = self.translate(RecordingModel())
        self.assertTrue(second.ok, second.message)
        self.assertEqual(second.output_files, [])
        self.assertIn("写入 0 个译文文件", second.message)

    def test_resume_after_partial_batch_recovers_only_unfinished_line(self):
        self.story.write_text(
            'label start:\n    "Keep walking." id walk\n    "Turn left." id turn\n', encoding="utf-8")
        pause_event = threading.Event()

        def pause_after_result(_client, _text):
            pause_event.set()
            raise PauseRequested()

        paused = self.translate(RecordingModel({"Keep walking.": "继续走。"}, after_item=pause_after_result),
                                pause_event=pause_event)
        self.assertTrue(paused.paused, paused.message)
        marker = self.out.parent / ".schinese.暂停标记.txt"
        self.assertIn("Turn left.", marker.read_text(encoding="utf-8-sig"))
        self.assertNotIn("Keep walking.", marker.read_text(encoding="utf-8-sig"))
        self.assertFalse((self.out / "story.rpy").exists())
        resumed_model = RecordingModel({"Turn left.": "向左转。"})
        resumed = self.translate(resumed_model)
        self.assertTrue(resumed.ok, resumed.message)
        self.assertEqual(resumed_model.calls, [("Turn left.",)])
        self.assertEqual(parse_existing_translations(self.out, "schinese")[0], {"walk": "继续走。", "turn": "向左转。"})
        self.assertFalse(marker.exists())
        self.assertFalse((self.out.parent / ".schinese.汉化进度.json").exists())

    def test_game_controlled_dynamic_string_is_never_requested_or_replaced(self):
        self.story.write_text('screen forecast():\n    text "Weather forecast"\n', encoding="utf-8")
        manual = self.out / "weather.rpy"
        original = b'translate schinese strings:\n    old "Weather forecast"\n    new current_forecast()\n'
        manual.write_bytes(original)
        client = RecordingModel()
        result = self.translate(client)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(client.calls, [])
        self.assertEqual(result.output_files, [])
        self.assertEqual(manual.read_bytes(), original)

    def test_changed_source_with_same_id_can_legitimately_be_its_own_translation(self):
        self.story.write_text('label start:\n    "Keep moving." id sign\n', encoding="utf-8")
        self.assertTrue(self.translate(RecordingModel({"Keep moving.": "继续走。"})).ok)
        self.story.write_text('label start:\n    "向右走。" id sign\n', encoding="utf-8")
        fixed = self.translate(RecordingModel({"向右走。": "向右走。"}))
        self.assertTrue(fixed.ok, fixed.message)
        originals = {}
        translated, _ = parse_existing_translations(self.out, "schinese", originals=originals)
        self.assertEqual((originals["sign"], translated["sign"]), ("向右走。", "向右走。"))
        self.assertEqual(self.translate(RecordingModel()).output_files, [])

    def test_failed_extraction_retains_the_unseen_queue_entry_and_its_location(self):
        self.story.write_text('label start:\n    "Look here." id first\n', encoding="utf-8")
        (self.game / "lost.rpyc").write_bytes(b"invalid compilation")
        self.queue.write_text("lost.rpy:92  There was another road.\n", encoding="utf-8-sig")
        result = self.translate(RecordingModel({"Look here.": "看这里。"}))
        self.assertFalse(result.ok)
        self.assertTrue(any("lost.rpyc" in error for error in result.errors))
        self.assertEqual(read_retry_entries(self.queue), {"There was another road.": "lost.rpy:92"})
        self.assertEqual(parse_existing_translations(self.out, "schinese")[0], {"first": "看这里。"})

    def test_english_target_accepts_identical_english_and_keeps_real_failures_in_the_queue(self):
        self.story.write_text(
            'label start:\n    "Look inside." id inside\n    "你好。" id greeting\n'
            '    "Wait here." id wait\n', encoding="utf-8")
        client = RecordingModel({"Look inside.": "Look inside.", "你好。": "Hello."}, failures=["Wait here."])
        with patch("rpytranslator.pipeline.time.sleep"):
            partial = self.translate(client, language="english")
        self.assertFalse(partial.ok)
        self.assertEqual(set(client.targets), {"English"})
        self.assertEqual(sum("Look inside." in batch for batch in client.calls), 1)
        queue = self.out.parent / "english.未翻译报告.txt"
        self.assertEqual(read_retry_entries(queue), {"Wait here.": "story.rpy:4"})
        fixed_client = RecordingModel({"Wait here.": "Wait here."})
        fixed = self.translate(fixed_client, language="english")
        self.assertTrue(fixed.ok, fixed.message)
        self.assertEqual(fixed_client.calls, [("Wait here.",)])
        self.assertFalse(queue.exists())
        translations, _ = parse_existing_translations(self.out.parent / "english", "english")
        self.assertEqual(translations, {"inside": "Look inside.", "greeting": "Hello.", "wait": "Wait here."})

    def test_successful_existing_target_language_does_not_schedule_retries(self):
        for language, text in (("schinese", "风已经停了。"), ("korean", "문을 닫으세요."),
                               ("japanese", "はい、待っています。")):
            with self.subTest(language=language), patch("rpytranslator.pipeline.time.sleep"):
                self.story.write_text(f'label start:\n    "{text}" id original\n', encoding="utf-8")
                client = RecordingModel({text: text})
                result = self.translate(client, language=language)
                self.assertTrue(result.ok, result.message)
                self.assertLessEqual(client.request_count, 1)
                self.assertFalse((self.out.parent / f"{language}.未翻译报告.txt").exists())

    def test_new_session_reuses_detected_names_for_translation_and_offline_audit(self):
        name = "New Detroit City"
        first_line = f"We came from {name}."
        second_line = f"Where is {name}?"
        self.story.write_text(f'label start:\n    "{first_line}" id arrival\n', encoding="utf-8")

        def recognize_name(client, _text):
            client.detected_names = (name,)

        first = self.translate(RecordingModel({first_line: f"我们来自{name}。"}, after_item=recognize_name))
        self.assertTrue(first.ok, first.message)
        self.assertEqual(run_audit(self.game, "schinese").queued, 0)
        with self.story.open("a", encoding="utf-8") as script:
            script.write(f'    "{second_line}" id question\nscreen city():\n    text "{name}"\n')
        restarted = RecordingModel({second_line: f"{name}在哪？"})
        second = self.translate(restarted)
        self.assertTrue(second.ok, second.message)
        self.assertEqual(restarted.calls, [(second_line,)])
        self.assertIn(name, restarted.seen_names[0])
        audited = run_audit(self.game, "schinese")
        self.assertEqual((audited.queued, audited.counts), (0, {}))
        self.assertFalse(self.queue.exists())
