"""不启动 WinUI 的 GUI 状态回归测试。"""
from __future__ import annotations

import ast
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from rpytranslator.gui_state import (
    group_log_entries, has_recoverable_translation, pending_matches,
    should_follow_log,
)


class GuiStateTests(unittest.TestCase):
    def test_xaml_is_well_formed_and_new_handlers_exist(self) -> None:
        source = Path(__file__).resolve().parent.parent / "rpytranslator" / "gui_main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        xaml = next(
            node.value.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "XAML"
                    for target in node.targets)
        )
        root = ET.fromstring(xaml)
        names = {item.get("{http://schemas.microsoft.com/winfx/2006/xaml}Name")
                 for item in root.iter()}
        self.assertTrue({"PageScroll", "LogScroll", "RecoveryPanel"} <= names)
        gui_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "GuiApp")
        handlers = {node.name for node in gui_class.body
                    if isinstance(node, ast.FunctionDef)}
        self.assertTrue({"OnResume", "OnLangChanged"} <= handlers)

    def test_logs_are_grouped_without_losing_lines_or_levels(self) -> None:
        entries = [(f"日志 {i}", "info") for i in range(501)]
        entries += [("失败", "err"), ("恢复", "ok")]
        groups = group_log_entries(entries)
        self.assertEqual([level for level, _ in groups], ["info", "err", "ok"])
        self.assertEqual(groups[0][1].splitlines(), [f"日志 {i}" for i in range(501)])
        self.assertEqual(groups[1][1], "失败\n")
        self.assertEqual(groups[2][1], "恢复\n")

    def test_only_follow_log_near_the_bottom(self) -> None:
        self.assertTrue(should_follow_log(0, 0))
        self.assertTrue(should_follow_log(477, 500))
        self.assertFalse(should_follow_log(300, 500))

    def test_pending_task_is_bound_to_game_and_language(self) -> None:
        task = {"game_path": "sample/game", "language": "schinese"}
        self.assertTrue(pending_matches(task, "sample/game", "schinese"))
        self.assertFalse(pending_matches(task, "other/game", "schinese"))
        self.assertFalse(pending_matches(task, "sample/game", "japanese"))
        self.assertFalse(pending_matches(None, "sample/game", "schinese"))

    def test_recovery_is_manual_and_depends_on_existing_checkpoint(self) -> None:
        root = Path("test-game")
        game = root / "game"
        present: set[str] = set()
        with (patch.object(Path, "is_dir", new=lambda path: True),
              patch("rpytranslator.gui_state.engine._find_game_dir",
                    return_value=game),
              patch.object(Path, "is_file", new=lambda path: path.name in present)):
            self.assertFalse(has_recoverable_translation(root, "schinese"))
            present.add(".schinese.汉化进度.json")
            self.assertTrue(has_recoverable_translation(root, "schinese"))
            self.assertFalse(has_recoverable_translation(root, "japanese"))
            present.clear()
            present.add(".schinese.暂停标记.txt")
            self.assertTrue(has_recoverable_translation(game, "schinese"))


if __name__ == "__main__":
    unittest.main()
