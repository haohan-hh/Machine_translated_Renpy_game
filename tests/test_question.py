# -*- coding: utf-8 -*-
"""用官方 the_question 游戏的真实 schinese 汉化对比测试提取器。
真实汉化文件由旧版源生成，master 源若有改动则 ID 必然不一致；
本测试会区分「提取错误」与「官方 tl 过时」两种情况。"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rpytranslator.extract import extract_rpy_files, make_say_code, _code_digest

BASE = Path(__file__).parent / "the_question"
GAME = BASE / "game"
TL = BASE / "tl"

files = [GAME / f for f in ("script.rpy", "options.rpy", "screens.rpy", "gui.rpy")]
res = extract_rpy_files(files)
print(f"提取到对话: {len(res.dialogues)} 条, 字符串: {len(res.strings)} 条")

# 1. 对话 ID 对比
tl_text = (TL / "script.rpy").read_text(encoding="utf-8-sig", errors="replace")
expected_ids = set()
for m in re.finditer(r"^translate schinese (\S+):", tl_text, re.M):
    if m.group(1) != "strings":
        expected_ids.add(m.group(1))
mine_ids = {d.identifier for d in res.dialogues}

matched = expected_ids & mine_ids
missing = expected_ids - mine_ids
extra = mine_ids - expected_ids
print(f"对话 ID 匹配: {len(matched)} / {len(expected_ids)}")

stale = []
real_missing = []
for mid in sorted(missing):
    # 从官方 tl 注释行取出原文 code，验证是否「源文件已改动」
    seg = tl_text[:tl_text.index(f"translate schinese {mid}")]
    line_comment = seg.rstrip().rsplit("\n", 1)[-1]  # 应为 `# script.rpy:NN`
    i = tl_text.index(f"translate schinese {mid}")
    # 注释行在原句位于 translate 头之后：`    # <code>`
    comment = None
    for line in tl_text[i:].splitlines()[1:8]:
        if line.startswith("    # "):
            comment = line[6:].strip()
            break
    if comment:
        # 尝试构造 get_code 验证（含 nointeract / with / id 等尾部标记）
        m = re.match(r'^(?:(\w+) )?"((?:[^"\\]|\\.)*)"', comment)
        if m:
            who = m.group(1)
            what = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
            rest = comment[m.end():].strip()
            interact = "nointeract" not in rest
            with_expr = None
            explicit_id = None
            wm = re.search(r"\bwith\s+(\S+)", rest)
            if wm:
                with_expr = wm.group(1)
            im = re.search(r"\bid\s+(\w+)", rest)
            if im:
                explicit_id = im.group(1)
            code = make_say_code(who, what, interact=interact,
                                 with_expr=with_expr, explicit_id=explicit_id)
            if _code_digest(code) == mid.split("_", 1)[-1]:
                stale.append(mid)
                continue
    real_missing.append(mid)
print("官方 tl 过时（源已改动，算法正确）:", len(stale), stale)
print("真正的缺失 ID:", real_missing)
print("多余 ID（我方多提取）:", sorted(extra))

# 2. 字符串对比（归一化 \n 转义）
def norm(s: str) -> str:
    return s.replace("\\n", "\n")

tl_opt = (TL / "options.rpy").read_text(encoding="utf-8-sig", errors="replace")
expected_strings = set()
for m in re.finditer(r'^\s*old "((?:[^"\\]|\\.)*)"', tl_opt + "\n" + tl_text, re.M):
    expected_strings.add(norm(m.group(1)))
mine_strings = {s.text for s in res.strings}

missing_str = expected_strings - mine_strings
print(f"\n字符串匹配: {len(expected_strings & mine_strings)} / {len(expected_strings)}")
if missing_str:
    print("缺失字符串（可能因 tl 过时）:")
    for s in sorted(missing_str):
        print("   -", repr(s[:60]))

# 结论
ok = not real_missing and not missing_str
print("\n结论:", "全部匹配 ✓" if ok else "存在差异（请人工核对）")
sys.exit(0 if ok else 1)
