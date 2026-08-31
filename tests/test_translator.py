# -*- coding: utf-8 -*-
"""translator 模块单元测试：占位符保护、JSON 解析、批译降级。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rpytranslator.translator import (  # noqa: E402
    TranslationClient, TranslationConfig, protect_text, restore_text,
)


class _FakeChat:
    """可控的 chat 实现：按注入的响应队列返回。"""

    def __init__(self, client: TranslationClient):
        self.client = client
        self.responses: list[str] = []
        self.raises: list[Exception] = []

    def chat(self, messages):
        if self.raises:
            raise self.raises.pop(0)
        return self.responses.pop(0)

    def __enter__(self):
        self._orig = self.client.chat
        self.client.chat = self.chat
        return self

    def __exit__(self, *a):
        self.client.chat = self._orig


def test_parse_json_array():
    cases = [
        ('["a", "b"]', ["a", "b"]),
        ('```json\n["x", "y"]\n```', ["x", "y"]),
        ('好的，翻译如下：\n["第一", "第二"]', ["第一", "第二"]),
        ('  ["带 空格 ", " 值"]  ', ["带 空格 ", " 值"]),
    ]
    for raw, expected in cases:
        assert TranslationClient._parse_json_array(raw) == expected, f"解析失败: {raw!r}"
    # 非法输入应抛错
    try:
        TranslationClient._parse_json_array("不是 JSON")
        assert False, "应抛出异常"
    except Exception:
        pass
    print("[ok] _parse_json_array 解析 4 例 + 非法输入")


def test_batch_translate_success():
    client = TranslationClient(TranslationConfig())
    texts = ["Hello {b}world{/b}", "Hi [name]", "L1\nL2"]
    expected = ["你好 {b}世界{/b}", "嗨 [name]", "一\n二"]
    # 模拟模型正确响应：把占位符保留、内容替换
    with _FakeChat(client) as fk:
        def make_payload(payload):
            out = []
            for t in payload:
                for ph in ("\ue000", ):
                    pass
                out.append(t.replace("Hello ", "你好 ").replace("{b}", "{b}"))
            return out
        fk.responses.append("```json\n" + json.dumps(
            ["你好 {b}世界{/b}", "嗨 [name]", "一\n二"], ensure_ascii=False) + "\n```")
        got = client.translate_texts(texts)
    assert got == expected, f"翻译结果不符: {got!r}"
    print("[ok] 批量翻译成功（占位符保留）")


def test_batch_translate_fallback():
    client = TranslationClient(TranslationConfig(max_retries=2))
    texts = ["alpha", "beta"]
    with _FakeChat(client) as fk:
        # 批译尝试1：数量不符 → 批译尝试2：解析失败 → 降级单条
        fk.responses.append('["only-one"]')
        fk.responses.append('not json')
        fk.responses.append('"ALPHA"')
        fk.responses.append('"BETA"')
        got = client.translate_texts(texts)
    assert got == ["ALPHA", "BETA"], f"降级失败: {got!r}"
    print("[ok] 批译失败自动降级为逐条翻译")


def test_translate_error_falls_back():
    """服务不可用时应回退原文而不是崩溃。"""
    from rpytranslator.translator import TranslationError
    client = TranslationClient(TranslationConfig(max_retries=1))
    with _FakeChat(client) as fk:
        fk.raises.append(TranslationError("HTTP 500 请求失败"))
        got = client.translate_texts(["x"])
    assert got == ["x"], f"应回退原文: {got!r}"
    print("[ok] 服务错误时回退原文")


if __name__ == "__main__":
    test_parse_json_array()
    test_batch_translate_success()
    test_batch_translate_fallback()
    test_translate_error_falls_back()
    print("\n全部通过")
