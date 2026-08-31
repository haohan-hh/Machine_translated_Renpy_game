# -*- coding: utf-8 -*-
"""
端到端流水线测试：用 the_question 官方示例游戏 + 伪翻译客户端，
验证 扫描→提取→翻译→生成 全流程，并对照官方 tl 的块结构。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rpytranslator.translator import protect_text, restore_text  # noqa: E402
from rpytranslator.generator import (  # noqa: E402
    build_file_script, group_by_source_file)
from rpytranslator.pipeline import run_pipeline  # noqa: E402

GAME = ROOT / "tests" / "the_question" / "game"
OFFICIAL_TL = ROOT / "tests" / "the_question" / "tl"


class MockClient:
    """伪翻译客户端：原文加 [译] 前缀，保留占位符。"""

    def translate_texts(self, texts, target="简体中文"):
        out = []
        for t in texts:
            p, ph = protect_text(t)
            out.append(restore_text("[译]" + p, ph))
        return out


def test_protect_restore():
    samples = [
        "Hello {b}world{/b}!",
        "Value: [player_name] and {color=#ff0000}red{/color}",
        "Line one\nLine two",
        "Plain text",
        "Mix {size=+5}[name]{/size}\n[hp]/{max_hp}",
        "",
    ]
    for s in samples:
        p, ph = protect_text(s)
        r = restore_text(p, ph)
        assert r == s, f"往返不一致: {s!r} -> {p!r} -> {r!r}"
    print(f"[ok] protect/restore 往返 {len(samples)} 例")


def test_generator_format():
    """用官方译文生成 script.rpy，检查块结构关键字。"""
    from rpytranslator.extract import extract_rpy_file

    result = extract_rpy_file(GAME / "script.rpy")
    # 用原文作为译文（仅测结构）
    d_trans = {d.identifier: d.what for d in result.dialogues}
    s_trans = {s.text: s.text for s in result.strings}
    script = build_file_script(
        result.dialogues, result.strings, "schinese",
        d_trans, s_trans, game_dir=GAME)
    assert script.startswith("# TODO: Translation updated at "), "缺少头部注释"
    assert "translate schinese start_" in script, "缺少 translate 块"
    assert re.search(r"translate schinese strings:", script), "缺少 strings 块"
    assert re.search(r"# script\.rpy:\d+", script), "缺少行号注释"
    assert 'old "' in script and 'new "' in script, "缺少 old/new"
    # 注释路径不带 game/ 前缀
    assert "# game/script.rpy" not in script, "注释路径不应含 game/ 前缀"
    print(f"[ok] 生成器结构正确（对话 {len(result.dialogues)} 条）")


def _cleanup_game_artifacts(game=GAME):
    """清理测试可能产生的输出：tl/、zz_*.rpy 补丁、fonts/。"""
    import shutil
    shutil.rmtree(game / "tl", ignore_errors=True)
    shutil.rmtree(game / "fonts", ignore_errors=True)
    for p in game.glob("zz_*.rpy"):
        p.unlink(missing_ok=True)


def test_pipeline():
    # 清理上次测试生成的输出，避免被误判为"已汉化"
    _cleanup_game_artifacts()
    res = run_pipeline(GAME, client=MockClient())
    assert res.ok, f"流水线失败: {res.message}"
    assert res.dialogue_count >= 60, f"对话数异常: {res.dialogue_count}"
    assert res.output_dir is not None and res.output_dir.is_dir()
    files = {p.name for p in res.output_files}
    assert "script.rpy" in files, f"缺少 script.rpy: {files}"
    # 生成文件的语法结构：translate 块、strings 块条目必须缩进、old/new 配对
    for f in res.output_files:
        text = f.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        assert "translate schinese" in text, f"无 translate 块: {f}"
        in_strings = False
        pair = 0
        for ln in lines:
            if ln.startswith("translate schinese strings:"):
                in_strings = True
                continue
            if in_strings and (ln.lstrip().startswith("old ")
                               or ln.lstrip().startswith("new ")):
                assert ln.startswith("    "), f"strings 块未缩进: {ln!r} in {f}"
                pair += 1
        assert pair % 2 == 0 and pair > 0, f"strings 块 old/new 不配对: {f}"
    print(f"[ok] 流水线生成 {len(res.output_files)} 个文件: {sorted(files)}")


def _official_stale_ids(off_text: str) -> set[str]:
    """返回官方 tl 中过时的 ID 集合（从注释重建 code 验证 digest）。"""
    import ast as pyast

    from rpytranslator.extract import _code_digest, make_say_code
    stale = set()
    for m in re.finditer(r"translate schinese (\w+):", off_text):
        mid = m.group(1)
        if mid == "strings":
            continue
        comment = None
        for line in off_text[m.end():].splitlines()[1:8]:
            if line.startswith("    # "):
                comment = line[6:].strip()
                break
        if not comment:
            continue
        cm = re.match(r'^(?:(\w+) )?"((?:[^"\\]|\\.)*)"', comment)
        if not cm:
            continue
        who = cm.group(1)
        what = cm.group(2).replace('\\"', '"').replace("\\\\", "\\")
        rest = comment[cm.end():].strip()
        code = make_say_code(
            who, what, interact="nointeract" not in rest,
            with_expr=(m2.group(1) if (m2 := re.search(r"\bwith\s+(\S+)", rest)) else None),
            explicit_id=(m3.group(1) if (m3 := re.search(r"\bid\s+(\w+)", rest)) else None),
        )
        if _code_digest(code) == mid.split("_", 1)[-1]:
            stale.add(mid)
    return stale


def test_output_vs_official():
    """生成文件与官方 tl 的 translate 块 ID 集合一致（官方过时项除外）。"""
    generated = GAME / "tl" / "schinese" / "script.rpy"
    official = OFFICIAL_TL / "script.rpy"
    if not generated.is_file() or not official.is_file():
        print("[skip] 缺少生成/官方文件，跳过对比")
        return
    gen_ids = set(re.findall(r"translate schinese (\w+):", generated.read_text(encoding="utf-8-sig")))
    off_text = official.read_text(encoding="utf-8")
    off_ids = set(re.findall(r"translate schinese (\w+):", off_text))
    # 官方 ID 应全部出现在生成结果中（官方过时项除外）
    missing = off_ids - gen_ids - _official_stale_ids(off_text)
    assert not missing, f"官方 ID 缺失: {missing}"
    print(f"[ok] 生成 ID 覆盖官方全部 {len(off_ids)} 个 ID")


if __name__ == "__main__":
    test_protect_restore()
    test_generator_format()
    test_pipeline()
    test_output_vs_official()
    # 清理测试产物
    _cleanup_game_artifacts()
    print("\n全部通过")
