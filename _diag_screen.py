# -*- coding: utf-8 -*-
"""诊断 Dawn Chorus screens 样式（临时脚本，用后即删）。"""
import sys

sys.path.insert(0, r"e:\Code_Buddy_作品\翻译")
from rpytranslator import rpa_loader
from rpytranslator.patcher import _decompile_rpyc_source

RPA = r"D:\Dawn Chorus\game\archive.rpa"
text = _decompile_rpyc_source(
    rpa_loader.read_script_data([RPA], "screens.rpyc"), "screens.rpyc"
)
lines = text.splitlines()
print("total lines:", len(lines))

print("\n===== style/font/text_properties 相关 =====")
for i, ln in enumerate(lines, 1):
    low = ln.lower()
    if ("style" in low and ("font" in low or "text_properties" in low or "is" in low)) or "text_properties" in low:
        print(i, ":", ln[:150])

print("\n===== main_menu 屏幕 =====")
start = end = None
for i, ln in enumerate(lines, 1):
    if "screen main_menu" in ln.lower() or "screen main menu" in ln.lower():
        start = i
    if start and i > start and (ln.strip().startswith("screen ") or i - start > 60):
        end = i
        break
if start:
    print("\n".join(f"{j}: {lines[j-1]}" for j in range(start, min(end or start + 60, len(lines) + 1))))
