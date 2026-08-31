# -*- coding: utf-8 -*-
"""用 Ren'Py 官方示例游戏 the_question 的真实汉化文件验证翻译 ID 算法。"""
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from rpytranslator.extract import make_say_code, make_dialogue_identifier

# 锚点 1：the_question/game/tl/schinese/script.rpy
# translate schinese start_915cb944:   ->  label "start", 旁白
what1 = ("It's only when I hear the sounds of shuffling feet and supplies being "
         "put away that I realize that the lecture's over.")
code1 = make_say_code(None, what1)
id1 = make_dialogue_identifier("start", code1)
print("id1:", id1, "期望 start_915cb944 ->", "OK" if id1 == "start_915cb944" else "FAIL")

# 锚点 2：character say
# translate schinese rightaway_cf214f74:   ->  label "rightaway", who "s"
code2 = make_say_code("s", "Hi there! How was class?")
id2 = make_dialogue_identifier("rightaway", code2)
print("id2:", id2, "期望 rightaway_cf214f74 ->", "OK" if id2 == "rightaway_cf214f74" else "FAIL")
