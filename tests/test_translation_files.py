import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpytranslator.audit import AuditItem, classify, plan_retry_queue, run_audit
from rpytranslator.extract import (DialogueUnit, RpyExtractor, StringUnit,
                                   extract_rpy_files, make_dialogue_identifier)
from rpytranslator.generator import parse_existing_translations, write_translation_files
from rpytranslator.glossary import save_game_terms
from rpytranslator.reports import parse_external_report, read_retry_entries, read_retry_queue
from rpytranslator.tl_files import TranslationMergeError, index_tl_directory, read_tl_file
from tests.test_sources import write_rpa


class TranslationFilesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.game = Path(temp.name) / "game"
        self.out = self.game / "tl" / "schinese"
        self.out.mkdir(parents=True)

    def write(self, ds=(), ss=(), d=None, s=None, **kwargs):
        return write_translation_files(ds, ss, "schinese", d or {}, s or {},
                                       game_dir=self.game, **kwargs)

    def test_named_menu_and_explicit_say_id_round_trip(self):
        result = RpyExtractor("story.rpy").parse(
            'label start:\n'
            '    menu route:\n'
            '        e happy @ wink "Where next?"\n'
            '        "Return {#route}" if ready:\n'
            '            "The harbor waits."\n'
            '    "Captain May" "We sail." id sail (voice="a") with dissolve\n'
            '    menu:\n'
            '        "Pick a port"\n'
            '        "North":\n'
            '            pass\n')
        self.assertEqual([s.text for s in result.strings if s.context == "menu"],
                         ["Return {#route}", "Pick a port", "North"])
        self.assertEqual([s.text for s in result.strings if s.context == "character"], ["Captain May"])
        ask, narrator, captain = result.dialogues
        self.assertEqual(ask.identifier, make_dialogue_identifier("route", 'e happy @ wink "Where next?" nointeract'))
        self.assertEqual(captain.identifier, "sail")
        self.assertEqual(narrator.what, "The harbor waits.")
        ds = {ask.identifier: "去哪？", narrator.identifier: "港口在等待。", "sail": "出发。"}
        ss = {u.text: "选择" for u in result.strings}
        [path] = self.write(result.dialogues, result.strings, ds, ss)
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn('e happy @ wink "去哪？" nointeract', text)
        self.assertIn('"Captain May" "出发。" id sail (voice="a") with dissolve', text)
        self.assertEqual(parse_existing_translations(self.out, "schinese"), (ds, ss))
        self.assertEqual(self.write(result.dialogues, result.strings, ds, ss), [])

    def test_protected_character_name_survives_ui_deduplication(self):
        sources = [self.game / "screens.rpy", self.game / "names.rpy"]
        sources[0].write_text('screen credits():\n    text "Captain May"\n    text "[page]"\n', encoding="utf-8")
        sources[1].write_text('define n = Character(_("Captain May"))\n'
                              'define m = Character(\n    "Chief Lee",\n    color="#fff")\n'
                              'define p = Character(None, image="portraits")\n'
                              'define pic = _("icons.png")\n', encoding="utf-8")
        result = extract_rpy_files(sources)
        self.assertEqual([(u.text, u.context) for u in result.strings],
                         [("Captain May", "character"), ("Chief Lee", "character")])

    def test_source_say_escapes_and_ui_python_literals_have_different_spacing_rules(self):
        result = RpyExtractor("script.rpy").parse(
            'label start:\n' + r'    e "Two  words. One \ more. \[name]."' + '\n'
            'screen info():\n    text "UI  spaces"\n')
        self.assertEqual(result.dialogues[0].what, 'Two words. One  more. [[name].')
        self.assertEqual(result.strings[0].text, 'UI  spaces')

    @unittest.skipUnless(os.environ.get("RENPY_TEST_LAUNCHER"), "Set RENPY_TEST_LAUNCHER to run the engine test")
    def test_engine_loads_generated_dialogues_and_menu_strings(self):
        launcher = Path(os.environ["RENPY_TEST_LAUNCHER"])
        root = self.game.parent
        src = self.game / "story.rpy"
        src.write_text('define e = Character("Captain May")\n'
                       'label start:\n    menu route:\n'
                       '        e happy "Where next?"\n'
                       '        "Harbor {#route}":\n            e "Set sail." id fixed_name\n'
                       '    return\n', encoding="utf-8")
        options = 'define config.save_directory = "rpytranslator-engine-test"\n'
        (self.game / "options.rpy").write_text(options, encoding="utf-8")
        result = extract_rpy_files([src])
        ds = {u.identifier: "译文" for u in result.dialogues}
        self.write(result.dialogues, result.strings, ds, {"Harbor {#route}": "港口"})
        env = dict(os.environ, RENPY_PATH_TO_SAVES=str(root / "userdata"), RENPY_DISABLE_JOYSTICK="1")
        run = subprocess.run([str(launcher), str(root), "translate", "schinese"],
                             cwd=root, env=env, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        # The engine appends templates for missing identifiers. If either
        # identifier is wrong, the official generator appends another block.
        parsed = read_tl_file(self.out / "story.rpy", "schinese")
        self.assertEqual([entry.identifier for entry in parsed.entries if entry.identifier], list(ds))
        choices = [entry.original for entry in parsed.entries if entry.original == "Harbor {#route}"]
        self.assertEqual(choices, ["Harbor {#route}"])
        # Simulate a release shipping only the engine's compiled translation.
        self.assertTrue(parsed.path.with_suffix(".rpyc").is_file())
        parsed.path.rename(root / "saved_translation.txt")
        existing, strings = parse_existing_translations(self.out, "schinese")
        self.assertTrue(all(existing[key] == value for key, value in ds.items()))
        self.assertEqual(strings["Harbor {#route}"], "港口")
        [supplement] = self.write(ss=[StringUnit("New port", "story.rpy", 9)], s={"New port": "新港口"})
        self.assertNotEqual(supplement, parsed.path)
        self.assertFalse(parsed.path.exists())
        # Release packaging changes the physical location, not registered keys.
        compiled = parsed.path.with_suffix(".rpyc")
        archive = write_rpa(self.game / "translations.rpa", {
            "tl/schinese/story.rpyc": compiled.read_bytes(),
            "tl/schinese/story.rpy": b"unused source, the engine loads compiled translations",
        })
        compiled.unlink()
        archived_bytes = archive.read_bytes()
        archived_d, archived_s = parse_existing_translations(self.out, "schinese")
        self.assertTrue(all(archived_d.get(key) == value for key, value in ds.items()))
        self.assertEqual(archived_s["Harbor {#route}"], "港口")
        added = [StringUnit("Last stop", "story.rpy", 12)]
        self.assertEqual(self.write(ss=added, s={"Last stop": "终点"}), [supplement])
        self.assertEqual(self.write(ss=added, s={"Last stop": "终点"}), [])
        run = subprocess.run([str(launcher), str(root), "translate", "schinese"],
                             cwd=root, env=env, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        self.assertFalse(parsed.path.exists())
        self.assertEqual(archive.read_bytes(), archived_bytes)

    def test_partial_repair_preserves_manual_blocks_assets_and_per_id_translations(self):
        path = self.out / "script.rpy"
        manual = (
            '# maintained by player\ntranslate schinese hand:\n'
            '    # "Dr. Gray" "Come here."\n'
            '    "Dr. Gray" "过来。" with fade # edit "annotation"\n'
            '\ntranslate schinese complex:\n    $ count += 1\n    "手工剧情"\n'
            '\ntranslate schinese strings:\n    old \'Open\'\n    new \'开\'\n'
            '\ntranslate schinese strings:\n    old "Empty"\n    new ""\n'
            '\ntranslate japanese strings:\n    old "Empty"\n    new "日本語"\n'
            '\ninit python:\n    ui_mode = "preserve"\n'
        ).replace("\n", "\r\n")
        path.write_bytes(manual.encode("utf-8"))
        asset = self.out / "custom.otf"
        asset.write_bytes(b"font")
        ds = [DialogueUnit("e", "Come here.", "start", "script.rpy", 2, identifier=i)
              for i in ("hand", "complex", "new_line")]
        ss = [StringUnit(t, "script.rpy", 10) for t in ("Open", "Empty", "Different  spaces")]
        outputs = {u.identifier: "快过来。" for u in ds}
        strings = {"Open": "开启", "Empty": "空", "Different  spaces": "两个  空格"}
        self.assertEqual(self.write(ds, ss, outputs, strings), [path])
        self.assertTrue(path.read_bytes().startswith(manual.replace('new ""', 'new "空"').encode("utf-8")))
        self.assertEqual(asset.read_bytes(), b"font")
        dialogues, old_strings = parse_existing_translations(self.out, "schinese")
        self.assertEqual(dialogues, {"hand": "过来。", "complex": None, "new_line": "快过来。"})
        self.assertEqual(old_strings["Different  spaces"], "两个  空格")
        self.assertEqual(old_strings["Open"], "开")
        self.assertEqual(self.write(ds, ss, outputs, strings), [])
        outputs["hand"] = "Come here."  # A failed retry must keep a usable translation.
        self.assertEqual(self.write(ds, (), outputs, retry_texts={"Come here."}), [])
        outputs["hand"] = "请过来。"
        self.assertEqual(self.write(ds, (), outputs, retry_texts={"Come here."}), [path])
        self.assertIn('"Dr. Gray" "请过来。" with fade # edit "annotation"', path.read_text(encoding="utf-8"))

    def test_compiled_only_owner_preserved_and_new_keys_not_shadowed(self):
        compiled = self.out / "story.rpyc"
        compiled.write_bytes(b"existing translation")
        source = self.game / "decompiled.rpy"
        source.write_text('translate schinese strings:\n    old "Old"\n    new "旧"\n', encoding="utf-8")
        ss = [StringUnit(t, "story.rpy", 1) for t in ("Old", "New")]
        with patch("rpytranslator.rpyc_loader.decompile_rpyc_files", return_value=(None, {compiled: source})):
            [new] = self.write(ss=ss, s={"Old": "不覆盖", "New": "新增"})
            self.assertNotEqual(new, compiled.with_suffix(".rpy"))
            self.assertFalse(compiled.with_suffix(".rpy").exists())
            self.assertEqual(parse_existing_translations(self.out, "schinese")[1], {"Old": "旧", "New": "新增"})
        self.assertEqual(compiled.read_bytes(), b"existing translation")

    def test_unknown_string_key_does_not_publish_any_changes(self):
        custom = self.out / "custom.rpy"
        data = b'translate schinese strings:\n    old namespace.game_title\n    new "title"\n'
        custom.write_bytes(data)
        with self.assertRaisesRegex(TranslationMergeError, "old|字符串键"):
            self.write(ss=[StringUnit("Title", "new.rpy", 1)], s={"Title": "标题"})
        self.assertEqual(list(self.out.iterdir()), [custom])
        self.assertEqual(custom.read_bytes(), data)

    def test_source_only_archive_is_a_donor_until_loose_supplement_is_loaded(self):
        self.out.rmdir()
        def translation(value):
            return f'translate schinese strings:\n    old "Port"\n    new "{value}"\n'.encode()
        base = write_rpa(self.game / "base.rpa", {"tl/schinese/day/scene.rpy": translation("原港")})
        patch_file = write_rpa(self.game / "patch.rpa", {
            "tl/schinese/day/scene.rpy": translation("新港"),
            "tl/english/nested.rpy": b'translate english strings:\n    old "Test"\n    new "EN"\n',
            "tl/schinese/unused.rpy": b'translate schinese strings:\n    old "Dynamic"\n    new game.title\n',
        })
        archive_bytes = [path.read_bytes() for path in (base, patch_file)]
        self.assertFalse(self.out.exists())
        docs = index_tl_directory(self.out, "schinese")
        self.assertEqual(sum(len(doc.entries) for doc in docs.values()), 0)
        self.assertTrue(all(doc.read_only for doc in docs.values()))
        _, existing = parse_existing_translations(self.out, "schinese")
        self.assertEqual(existing, {"Port": "新港"})
        ss = [StringUnit(t, "day/scene.rpy", 3) for t in ("Port", "Starboard")]
        output = {**existing, "Starboard": "右舷"}
        [supplement] = self.write(ss=ss, s=output)
        self.assertNotEqual(supplement, self.out / "day/scene.rpy")
        self.assertEqual(self.write(ss=ss, s=output), [])
        self.assertEqual(parse_existing_translations(self.out, "schinese")[1], output)
        self.assertEqual([path.read_bytes() for path in (base, patch_file)], archive_bytes)

    def test_unknown_archive_location_stops_before_writing(self):
        archive = write_rpa(self.game / "broken.rpa", {"tl/schinese/old.rpyc": b"not an rpyc"})
        with self.assertRaisesRegex(TranslationMergeError, r"broken\.rpa!tl/schinese/old\.rpyc"):
            self.write(ss=[StringUnit("New", "script.rpy", 1)], s={"New": "新"})
        self.assertEqual(list(self.out.iterdir()), [])
        archive.write_bytes(b"not an RPA file")
        with self.assertRaisesRegex(TranslationMergeError, r"broken\.rpa"):
            self.write(ss=[StringUnit("New", "script.rpy", 1)], s={"New": "新"})
        self.assertEqual(list(self.out.iterdir()), [])

    def test_changed_explicit_id_updates_source_only_after_a_successful_translation(self):
        path = self.out / "old.rpy"
        original = ('translate schinese fixed_name:\n'
                    '    # "May" "Lift the lid." id fixed_name\n'
                    '    "May" "揭盖。" with fade # keep\n'
                    'translate schinese manual:\n    "May" "特别翻译。"\n')
        path.write_text(original, encoding="utf-8")
        originals = {}
        parse_existing_translations(self.out, "schinese", originals=originals)
        self.assertEqual(originals, {"fixed_name": "Lift the lid."})
        unit = DialogueUnit('"May"', "Lift a lid.", "start", "new.rpy", 8, identifier="fixed_name")
        self.assertEqual(self.write([unit], d={"fixed_name": unit.what}, changed_ids={"fixed_name"}), [])
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        # The new source wording legitimately has the same translation.
        self.assertEqual(self.write([unit], d={"fixed_name": "揭盖。"}, changed_ids={"fixed_name"}), [path])
        self.assertEqual(path.read_text(encoding="utf-8"), original.replace("Lift the lid.", "Lift a lid."))
        self.assertEqual(self.write([unit], d={"fixed_name": "揭盖。"}, changed_ids={"fixed_name"}), [])

    def test_offline_audit_reads_saved_names_and_only_skips_marked_tool_files(self):
        source = 'translate japanese strings:\n    old "New Detroit City"\n    new "New Detroit City"\n'
        out = self.out.parent / "japanese"
        out.mkdir()
        (out / "zz_language_display.rpy").write_text(
            '# 语言显示名（汉化工具自动生成）\n' + source.replace("New Detroit City", "English"), encoding="utf-8")
        (out / "zz_language_switch_probe.rpy").write_text(
            source + '    old "Open"\n    new "Open"\n', encoding="utf-8")
        save_game_terms(self.game, "japanese", ["New Detroit City"], (), {})
        result = run_audit(self.game, "japanese")
        self.assertEqual((result.total, result.queued), (2, 1))
        self.assertEqual(read_retry_queue(out.parent / "japanese.未翻译报告.txt"), ["Open"])

    def test_audit_preserves_quoted_speaker_suffix_names_and_other_languages(self):
        path = self.out / "manual.rpy"
        script = ('translate schinese start:\n    # "Dr. Gray" "Come now"\n'
                  '    "Dr. Gray" "我們去臺東和[變數]。" (voice="來") # "來"\n'
                  'translate japanese strings:\n    old "Open"\n    new "Open"\n'
                  'translate schinese strings:\n    old "New Detroit City"\n    new "New Detroit City"\n')
        path.write_text(script, encoding="utf-8")
        result = run_audit(self.game, "schinese", protected_terms=("臺東", "New Detroit City"))
        self.assertEqual(result.total, 2)
        self.assertEqual((result.queued, result.fixed_traditional), (0, 1))
        self.assertEqual(path.read_text(encoding="utf-8"), script.replace("我們", "我们"))
        self.assertIsNone(classify(AuditItem("", "", 0, original="开门", translation="Open the door"), "english"))
        self.assertIsNone(classify(AuditItem("", "", 0, original="Open the door", translation="Open the door"), "english"))
        self.assertEqual(classify(AuditItem("", "", 0, original="Open the door", translation="开门"), "english"), "wrong_lang")
        self.assertEqual(read_tl_file(path, "schinese").entries[0].who, '"Dr. Gray"')

    def test_audit_checks_partial_target_sources_and_ambiguous_ui_without_flagging_names(self):
        cases = (
            ("schinese", "风已经停了，please wait here.", None, "mixed"),
            ("schinese", "已经关门。", "", "untranslated"),
            ("schinese", "龍華在 New Detroit City。", None, None),
            ("schinese", "龍華說話。", None, "traditional"),
            ("schinese", "彼が帰った。", None, "untranslated"),
            ("tchinese", "皇后在故里。", None, None),
            ("tchinese", "风已经停了。", None, "wrong_lang"),
            ("japanese", "設定", None, None),
            ("japanese", "Preferences", "設定", None),
        )
        for language, source, result, expected in cases:
            with self.subTest(language=language, source=source, result=result):
                item = AuditItem("", "", 0, original=source,
                                 translation=source if result is None else result, is_string=True)
                self.assertEqual(classify(item, language, ("龍華", "New Detroit City")), expected)

    def test_retry_queue_keeps_source_whitespace_and_deduplicates_exact_keys(self):
        orig = '  A long\nline:200  with a literal \\n, a separator \u2028 and trailing spaces.  \n'
        self.write(ss=[StringUnit(orig, "ui.rpy", 3)], s={orig: orig})
        first = run_audit(self.game, "schinese")
        second = run_audit(self.game, "schinese")
        self.assertEqual((first.queued, second.queued), (1, 0))
        self.assertEqual(read_retry_queue(self.out.parent / "schinese.未翻译报告.txt"), [orig])
        self.assertEqual(read_retry_entries(self.out.parent / "schinese.未翻译报告.txt"), {orig: "ui.rpy:3"})

    def test_legacy_queue_repairs_actual_owners_and_keeps_manual_requests(self):
        doc = self.out / "custom.rpy"
        shared = "Close the door."
        doc.write_text(
            f'translate schinese ok:\n    # "{shared}"\n    "请把门关上。"\n'
            f'translate schinese bad:\n    # "{shared}"\n    "{shared}"\n'
            'translate schinese strings:\n    old "Quick Load"\n    new "Quick Load"\n'
            '    old "Wait"\n    new "请等待"\n    old "Exit"\n    new "离开"\n'
            '    old "Unqueued"\n    new "Unqueued"\n', encoding="utf-8")
        queue = self.out.parent / "schinese.未翻译报告.txt"
        queue.write_text('以下文本未能翻译（回退为原文），请检查翻译服务或手动补充：\n'
                         '== 字符串 ==\nscreen.rpy:2  Wait\n== 翻译错误详情（去重） ==\n'
                         'screen.rpy:2  not a game string\n'
                         f'== 补漏查缺发现的问题（自动排入，重译成功后消失）==\nstory.rpy:1  {shared}\n'
                         'common.rpy:199  Quick Load\n== 人工兜底导入 ==\n外部导入:0  Exit\n', encoding="utf-8")
        units = [DialogueUnit("", shared, "start", "story.rpy", n, identifier=k)
                 for n, k in enumerate(("ok", "bad"), 1)]
        plan = plan_retry_queue(self.out, "schinese", queue, units, [])
        self.assertEqual(plan.manual_texts, {"Exit"})
        self.assertNotIn("not a game string", plan.entries)
        self.assertEqual(set(plan.dialogues), {"bad"})
        self.assertEqual(set(plan.strings), {"Quick Load", "Exit"})
        replacement = doc.read_text(encoding="utf-8").replace(f'    "{shared}"', '    "合上门。"', 1)
        doc.write_text(replacement, encoding="utf-8")
        self.assertFalse(plan_retry_queue(self.out, "schinese", queue, units, []).dialogues)

    def test_repeated_audit_retires_only_resolved_audit_entries(self):
        mixed, fixed = "Read all morning.", "Read all night."
        doc = self.out / "story.rpy"
        doc.write_text(f'translate schinese strings:\n    old "{mixed}"\n    new "{mixed}"\n'
                       f'    old "{fixed}"\n    new "閱讀。"\n', encoding="utf-8")
        queue = self.out.parent / "schinese.未翻译报告.txt"
        queue.write_text('== 人工兜底导入 ==\n外部导入:0  Human request.\n', encoding="utf-8")
        first = run_audit(self.game, "schinese")
        self.assertEqual(first.fixed_traditional, 1)
        self.assertEqual(parse_external_report(first.report_path)[0], [mixed])
        self.assertEqual(set(read_retry_queue(queue)), {"Human request.", mixed})
        doc.write_text(doc.read_text(encoding="utf-8").replace(f'new "{mixed}"', 'new "整个早上都在看书。"'),
                       encoding="utf-8")
        second = run_audit(self.game, "schinese")
        self.assertEqual(second.counts, {})
        self.assertEqual(read_retry_entries(queue), {"Human request.": "外部导入:0"})


if __name__ == "__main__":
    unittest.main()
