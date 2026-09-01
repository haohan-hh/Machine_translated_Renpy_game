# -*- coding: utf-8 -*-
"""
AI 翻译模块：
- 文本保护（Ren'Py 文本标签 {…} / 变量插值 […] / 换行 \n）
- OpenAI 兼容 Chat Completions 客户端（urllib 标准库实现，无第三方依赖）
- 分块批量翻译（JSON 数组模式），失败自动重试，最终降级为逐条翻译
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 占位符保护
# ---------------------------------------------------------------------------

# 占位符使用可见 ASCII 标记（@@p0@@ / @@n0@@）：
# 私有区字符（\ue000 等）在很多模型的 tokenizer 中不可见，翻译时容易被
# 直接丢弃，导致占位符校验失败、整批回退。可见符号可显著降低丢失概率。
_PH_PREFIX = "@@p"            # 标签/插值/换行占位符
_PH_RE = re.compile("@@p(\\d+)@@")
_NAMES_PREFIX = "@@n"         # 人名/专有名词占位符
_NAMES_RE = re.compile("@@n(\\d+)@@")

# Ren'Py 文本标签 {color=#fff} / {b} / {size=+2} 等（不嵌套）
_TAG_RE = re.compile(r"\{[^{}]*\}")
# Ren'Py 变量插值 [var] / [var!q] 等（不嵌套）
_INSERT_RE = re.compile(r"\[[^\[\]]*\]")


def _placeholder(i: int) -> str:
    return f"{_PH_PREFIX}{i}@@"

def _name_placeholder(i: int) -> str:
    return f"{_NAMES_PREFIX}{i}@@"


def protect_text(text: str) -> tuple[str, list[str]]:
    """保护文本中的 {标签} / [插值] / 换行，返回 (保护后文本, 占位符列表)。"""
    placeholders: list[str] = []

    def repl(m: re.Match) -> str:
        i = len(placeholders)
        placeholders.append(m.group(0))
        return _placeholder(i)

    # 1. 变量插值（先保护，避免标签内的插值被拆散）
    text = _INSERT_RE.sub(repl, text)
    # 2. 文本标签
    text = _TAG_RE.sub(repl, text)
    # 3. 换行
    if "\n" in text:
        buf: list[str] = []
        for ch in text:
            if ch == "\n":
                i = len(placeholders)
                buf.append(_placeholder(i))
                placeholders.append("\n")
            else:
                buf.append(ch)
        text = "".join(buf)
    return text, placeholders


def restore_text(text: str, placeholders: list[str]) -> str:
    """把保护后文本中的占位符还原为原始片段。"""
    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(placeholders):
            return placeholders[idx]
        return m.group(0)
    return _PH_RE.sub(repl, text)


def _placeholders_preserved(placeholders: list[str], restored: str) -> bool:
    """校验占位符是否完整保留（顺序无关，数量与内容一致）。"""
    return all(ph in restored for ph in placeholders)


def protect_names(text: str, names: list[str]) -> tuple[str, list[str]]:
    """把名单中的人名/专有名词整体替换为占位符，防止被 AI 翻译。

    返回 (保护后文本, 占位符列表)。长名优先匹配，避免短名先匹配长名的一部分。
    """
    if not names:
        return text, []
    placeholders: list[str] = []

    def repl(m: re.Match) -> str:
        i = len(placeholders)
        placeholders.append(m.group(0))
        return _name_placeholder(i)

    for n in sorted(names, key=len, reverse=True):
        if not n:
            continue
        # 前后不能是字母/数字/下划线/中文，保证整词匹配（Alex 不影响 Alexandra）
        pat = r"(?<![\w\u4e00-\u9fff])" + re.escape(n) + r"(?![\w\u4e00-\u9fff])"
        text = re.sub(pat, repl, text)
    return text, placeholders


def restore_names(text: str, placeholders: list[str]) -> str:
    """把人名占位符还原为原始名字。"""
    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(placeholders):
            return placeholders[idx]
        return m.group(0)
    return _NAMES_RE.sub(repl, text)


def check_braces(text: str) -> bool:
    """检查 { } 是否配对（Ren'Py 要求成对，{{ 表示字面 {）。"""
    # 去掉转义对
    stripped = text.replace("{{", "").replace("}}", "")
    return stripped.count("{") == stripped.count("}")


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是一位专业的游戏本地化翻译。你的任务是把 Ren'Py 游戏文本翻译成{target}。\n"
    "严格要求：\n"
    "1. 逐条翻译，不遗漏、不合并、不自由发挥。\n"
    "2. 翻译结果中必须原样保留所有形如 \"{ph}\" 的占位符序列（它们代表游戏内标签/变量/换行/人名/专有名词），不得删除、修改或移动。\n"
    "3. 保留原有的换行结构。\n"
    "4. 所有人物名称、角色名、人名一律保留原文，绝不翻译成中文（例如 Eileen 保持 Eileen，Mako 保持 Mako）。\n"
    "5. 地名、组织名等专有名词尽量保留原文，若确需翻译应使用通用译名。\n"
    "6. 语气自然，符合角色口吻，译文长度尽量贴近原文。\n"
    "7. 只输出 JSON 数组，不要输出任何解释或标记。"
)

_SINGLE_SYSTEM_PROMPT = (
    "你是一位专业的游戏本地化翻译。把下面的 Ren'Py 游戏文本翻译成{target}。\n"
    "严格要求：\n"
    "1. 只输出翻译结果，不要输出解释。\n"
    "2. 原样保留所有形如 \"{ph}\" 的占位符序列（游戏内标签/变量/换行/人名）。\n"
    "3. 所有人物名称、角色名、人名一律保留原文，绝不翻译成中文。\n"
    "4. 语气自然，符合角色口吻。"
)

DEFAULT_TARGET = "简体中文"


@dataclass
class TranslationConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    timeout: int = 180
    max_retries: int = 3
    chunk_chars: int = 1200          # 每个批量请求的字符上限
    chunk_items: int = 40            # 每个批量请求的条目上限


class TranslationError(Exception):
    """翻译过程中不可恢复的错误。"""


class TranslationClient:
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(self, config: TranslationConfig):
        self.config = config
        self.request_count = 0       # 实际发出的 API 请求次数
        self.error_count = 0         # 失败的请求次数
        self.error_messages: list[str] = []  # 去重后的错误详情

    # -- 底层请求 ----------------------------------------------------------

    def _endpoint(self) -> str:
        url = self.config.base_url.strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        return url + "/chat/completions"

    def _record_error(self, msg: str, error_cb=None) -> None:
        """记录去重的错误消息；首次出现时通过 error_cb 实时上报。"""
        if msg in self.error_messages:
            return
        self.error_messages.append(msg)
        if error_cb:
            error_cb(msg)

    def chat(self, messages: list[dict], error_cb=None) -> str:
        """发起一次对话请求，返回 assistant 的文本内容。"""
        self.request_count += 1
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": False,
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        req = urllib.request.Request(
            self._endpoint(), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self.error_count += 1
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            msg = (f"HTTP {e.code} 请求失败: {e.reason}"
                   f"{(' - ' + detail) if detail else ''}")
            self._record_error(msg, error_cb)
            raise TranslationError(msg) from None
        except urllib.error.URLError as e:
            self.error_count += 1
            msg = f"无法连接翻译服务（{self.config.base_url}）: {e.reason}"
            self._record_error(msg, error_cb)
            raise TranslationError(msg) from None
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise TranslationError(f"翻译服务响应格式异常: {str(payload)[:300]}") from e

    # -- 批量翻译 ----------------------------------------------------------

    def translate_texts(
        self, texts: list[str], target: str = DEFAULT_TARGET,
        progress_cb=None, offset: int = 0, total: int | None = None,
        error_cb=None, names: list[str] | None = None,
    ) -> list[str]:
        """批量翻译文本，返回与输入等长的译文列表（失败项回退原文）。

        names: 需整体保留原文的人名/专有名词列表（翻译前保护为占位符）。
        progress_cb(done, total) 在每完成一个批次时回调，用于实时进度显示。
        error_cb(msg) 在每次请求失败时回调（去重），用于实时错误提示。
        """
        results: list[str] = list(texts)
        indices = [i for i, t in enumerate(texts) if t.strip()]
        if not indices:
            return results
        if total is None:
            total = len(indices)
        payload_texts = [texts[i] for i in indices]

        chunks = self._chunk(payload_texts)
        translated: list[str] = []
        done = 0
        for chunk in chunks:
            translated.extend(self._translate_chunk(chunk, target, error_cb, names))
            done += len(chunk)
            if progress_cb:
                progress_cb(offset + done, total)

        for idx, val in zip(indices, translated):
            results[idx] = val
        return results

    def _chunk(self, texts: list[str]) -> list[list[str]]:
        cfg = self.config
        chunks: list[list[str]] = []
        cur: list[str] = []
        cur_chars = 0
        for t in texts:
            if cur and (cur_chars + len(t) > cfg.chunk_chars
                        or len(cur) >= cfg.chunk_items):
                chunks.append(cur)
                cur, cur_chars = [], 0
            cur.append(t)
            cur_chars += len(t)
        if cur:
            chunks.append(cur)
        return chunks

    def _translate_chunk(self, texts: list[str], target: str,
                         error_cb=None, names: list[str] | None = None) -> list[str]:
        """翻译一个批次：人名保护 → 标签保护 → JSON 批译 → 逐条校验还原。

        部分条目校验失败时只重试失败条目，避免“一条失败拖垮整批”导致
        请求量爆炸（整批降级为逐条会放大 40 倍请求数，极易触发限流）。
        """
        names = names or []
        named = [protect_names(t, names) for t in texts]
        protected = [protect_text(nt) for nt, _ in named]
        payload = [p for p, _ in protected]
        ph_list = [ph for _, ph in protected]
        name_ph_list = [nph for _, nph in named]

        out: list[str] = [""] * len(texts)
        pending = list(range(len(texts)))     # 尚未翻译成功的原索引

        for attempt in range(self.config.max_retries):
            if not pending:
                break
            sub_payload = [payload[i] for i in pending]
            try:
                raw = self._request_json_array(sub_payload, target, error_cb)
                if len(raw) != len(sub_payload):
                    raise TranslationError(
                        f"返回条目数不符（期望 {len(sub_payload)}，实际 {len(raw)}）")
            except TranslationError as e:
                self._record_error(str(e), error_cb)
                if attempt < self.config.max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue
            except Exception as e:  # JSON 解析等
                self._record_error(str(e), error_cb)
                if attempt < self.config.max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue

            still_failed: list[int] = []
            for si, orig_i in enumerate(pending):
                ph, nph = ph_list[orig_i], name_ph_list[orig_i]
                restored = restore_text(raw[si], ph)
                restored = restore_names(restored, nph)
                if not _placeholders_preserved(ph, restored):
                    still_failed.append(orig_i)
                elif not _placeholders_preserved(nph, restored):
                    still_failed.append(orig_i)
                else:
                    out[orig_i] = restored
            if still_failed and len(still_failed) < len(pending):
                self._record_error(
                    f"本批 {len(pending)} 条中有 {len(still_failed)} 条占位符校验失败"
                    f"（已单独重试），示例: {texts[still_failed[0]][:50]!r}", error_cb)
            pending = still_failed
            if pending and attempt < self.config.max_retries - 1:
                time.sleep(1.2 * (attempt + 1))

        # 剩余失败条目：逐条翻译（单条请求，失败回退原文）
        for i in pending:
            out[i] = self._translate_single(texts[i], target, error_cb, names)
        return out

    def _request_json_array(self, payload: list[str], target: str,
                            error_cb=None) -> list[str]:
        system = _SYSTEM_PROMPT.format(
            target=target, ph=_PH_PREFIX + "0" + _PH_PREFIX)
        user = json.dumps(payload, ensure_ascii=False)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        resp = self.chat(messages, error_cb)
        return self._parse_json_array(resp)

    @staticmethod
    def _parse_json_array(text: str) -> list[str]:
        text = text.strip()
        # 去掉可能的 ```json 代码块
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
        if m:
            text = m.group(1).strip()
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            text = text[start:end + 1]
        arr = json.loads(text)
        if not isinstance(arr, list):
            raise TranslationError("翻译服务未返回 JSON 数组")
        return [str(x) for x in arr]

    # -- 单条翻译 ----------------------------------------------------------

    def _translate_single(self, text: str, target: str, error_cb=None,
                          names: list[str] | None = None) -> str:
        ntext, nph = protect_names(text, names or [])
        ptext, ph = protect_text(ntext)
        if not ptext.strip():
            return text
        system = _SINGLE_SYSTEM_PROMPT.format(
            target=target, ph=_PH_PREFIX + "0" + _PH_PREFIX)
        for attempt in range(self.config.max_retries):
            try:
                resp = self.chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": ptext},
                ], error_cb).strip()
                # 模型可能返回带引号的字符串
                if len(resp) >= 2 and resp[0] == '"' and resp[-1] == '"':
                    try:
                        resp = json.loads(resp)
                    except Exception:
                        pass
                restored = restore_text(resp, ph)
                restored = restore_names(restored, nph)
                if _placeholders_preserved(ph, restored) and _placeholders_preserved(nph, restored):
                    return restored
            except TranslationError as e:
                self._record_error(str(e), error_cb)
            except Exception as e:
                self._record_error(str(e), error_cb)
            if attempt < self.config.max_retries - 1:
                time.sleep(1.5 * (attempt + 1))
        return text  # 回退原文


def build_client(config: TranslationConfig | None = None) -> TranslationClient:
    return TranslationClient(config or TranslationConfig())
