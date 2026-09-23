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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

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


# 还原后仍残留的占位符痕迹：模型自行编造标记（原文没有）时会留下，写进
# 游戏就是乱码。匹配放宽到 "@+[pn]数字"，实测模型会把系统提示词里的示例
# "@@p0@@" 抄进译文，且抄错成 "@p0@@p" / "<｠@p0@@p｠>" 等变体。
_PH_RESIDUE_RE = re.compile(r"@+[pn]\d+")
# 模型把自家对话模板的特殊 token 泄漏进译文（如 "<｜hy_User｜>" 及其
# 各种残缺变体）。正常译文不可能包含这类标记。
_TEMPLATE_LEAK_RE = re.compile(r"<｜|｜>|<\|hy|hy_[A-Za-z]+")
# 代码块围栏
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
# 模型「以聊天方式回应」而非翻译时的典型措辞。
# 只收「几乎不可能出现在正常对白里」的说法，避免误伤正常译文
# （误判代价仅是这条回退原文并计入未翻译报告，不会污染游戏文本）。
_META_REPLY_MARKERS = (
    "请提供", "需要翻译", "请问有什么可以帮", "我已收到", "请随时告诉我",
    "以下为您", "以下是您", "我已准备", "我明白您的",
)


def _placeholder_residue(text: str) -> bool:
    """译文是否残留未还原的占位符标记（模型自行编造标记的迹象）。"""
    return bool(_PH_RESIDUE_RE.search(text))


def _unwrap_single_response(resp: str) -> str:
    """剥掉模型套在译文外面的各种外壳，取出纯译文。

    不同模型对「只输出译文」的理解不同，实测至少会出现：
    裸文本、带引号、``["…"]``、``{"response": "…"}``、```json 代码块。
    这些外壳若原样写进游戏，玩家就会看到 ``["译文"]``。
    """
    s = resp.strip()
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    if s[:1] in ("[", "{"):
        try:
            obj = json.loads(s)
        except ValueError:
            return s          # 不是合法 JSON，按原样返回
        if isinstance(obj, list):
            if len(obj) == 1 and isinstance(obj[0], str):
                return obj[0]
            if obj and all(isinstance(x, str) for x in obj):
                return "\n".join(obj)   # 模型把多行拆成了数组
        elif isinstance(obj, dict):
            for key in ("translation", "translated", "text", "result",
                        "response", "output", "译文"):
                if isinstance(obj.get(key), str):
                    return obj[key]
            vals = [v for v in obj.values() if isinstance(v, str)]
            if len(vals) == 1:
                return vals[0]
        return s
    # 去单层引号（模型常给整句套上引号）
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
    if len(s) >= 2 and pairs.get(s[0]) == s[-1]:
        s = s[1:-1]
    return s


def _looks_like_meta_reply(source: str, result: str) -> bool:
    """粗判译文是否是「聊天式回应/解释」而非真正的翻译。

    只在置信度较高时返回 True（例如模型答复“请提供需要翻译的文本”），
    误判的代价是这条回退原文并计入未翻译报告，不会污染游戏文本。
    """
    if not result.strip():
        return True
    if any(m in result for m in _META_REPLY_MARKERS):
        return True
    # 远长于原文（>4 倍且多出 80 字）→ 多半是解释性回复而非译文
    return len(result) > len(source) * 4 + 80


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


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _looks_untranslated(source: str, result: str) -> bool:
    """译文与原文一致、原文含拉丁字母且无 CJK → 疑似模型未翻译。"""
    if result != source or not _LATIN_RE.search(source):
        return False
    return not _CJK_RE.search(source)


def tcp_error_hint(err: object, host: str = "") -> str:
    """把连接类错误翻译成可操作的中文提示（本地模型场景尤其常见）。

    - WinError 10061 / Connection refused：端口上没有服务在监听。本地模型
      服务（Ollama / LM Studio）未启动，或只监听 IPv4 而地址解析成了 ::1，
      或服务端口与填写的地址不一致。
    - timed out：主机无响应，多为防火墙丢弃或地址/端口错误。
    """
    text = str(err).lower()
    if "10061" in text or "refused" in text:
        tip = ("目标端口没有服务在监听。使用本地模型时请先启动推理服务"
               "（Ollama: `ollama serve`；LM Studio: Developer → Start Server），"
               "并确认服务端口与 API 地址一致（Ollama 默认 11434，LM Studio 默认 1234）。")
        if host.lower() in ("localhost", "::1", ""):
            tip += " 若地址写的是 localhost，可改成 127.0.0.1（本地服务多只监听 IPv4）。"
        return "可能原因：" + tip
    if "timed out" in text or "timeout" in text:
        return ("可能原因：目标主机无响应——服务未启动、被防火墙拦截，"
                "或本地模型推理过慢。本地模型可适当调大超时时间。")
    if "10013" in text or "permission" in text:
        return "可能原因：端口访问被系统/安全软件拦截（WinError 10013）。"
    return "可能原因：网络不通、端口被防火墙拦截、或需要代理。"


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

# 精简系统提示词：用于「无法按常规指令返回结构化结果」的模型。
#
# 部分机器翻译模型（含本地部署的各类 MT 模型）未经指令微调，对较长的中文
# 系统提示不耐受——会退化成自由"聊天"而不返回 JSON 数组，导致整批翻译失败；
# 改用极简英文指令 + 严格 JSON/占位符约束后可稳定输出。
#
# 何时启用由运行时的**实际返回行为**自动判定（见
# TranslationClient._request_json_array），不依赖模型名称、厂商或是否本地部署，
# 因此对任意本地/云端模型通用。
_COMPACT_SYSTEM_PROMPT = (
    "This is an automated, non-interactive batch translation job. "
    "Never greet, ask questions or explain; always answer with exactly "
    "one JSON array.\n"
    "Translate each item of the input JSON array into {target}.\n"
    "Rules:\n"
    "1. Output ONLY a JSON array of the same length and order; no explanation, "
    "no markdown fences.\n"
    "2. Every \"{ph}\"-style token in the input is a placeholder: copy it "
    "exactly as-is. Never translate, delete, move, alter, or newly create "
    "such tokens.\n"
    "3. Keep the original line-break structure.\n"
    "4. Keep all character and proper names in original form.\n"
    "5. Natural tone matching the character; keep lengths close to the source."
)

_COMPACT_SINGLE_PROMPT = (
    "This is an automated, non-interactive translation job. Never greet, "
    "ask questions or explain; answer with the translation only.\n"
    "Translate the following Ren'Py game line into {target}.\n"
    "Every \"{ph}\"-style token is a placeholder: copy it exactly as-is. "
    "Never translate, delete, move, alter, or newly create such tokens.\n"
    "Keep character and proper names in original form."
)

# 精简提示词使用英文语言名（与多数机器翻译模型的训练指令一致，效果更稳定）
_COMPACT_TARGETS = {
    "简体中文": "Simplified Chinese",
    "繁体中文": "Traditional Chinese",
    "繁體中文": "Traditional Chinese",
    "日本語": "Japanese",
    "English": "English",
}


# 批量失败时的最大折半层数：40 条 → 20 → 10 → 5 → 2，再退化为逐条翻译
_MAX_SPLIT_DEPTH = 4

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
    max_workers: int = 1             # 并发批次数（>1 显著加速，注意服务端限流）
    # 提示词风格：None=按模型实际返回行为自动适配（默认，通用）；
    # True=始终使用精简提示词；False=始终使用详细提示词（关闭自动切换）。
    compact_prompt: bool | None = None


class TranslationError(Exception):
    """翻译过程中不可恢复的错误。"""


class ResponseFormatError(TranslationError):
    """模型返回内容不符合预期结构（非 JSON 数组 / 条目数不符等）。

    单独成类是为了与「网络 / HTTP 失败」区分开：只有这类错误才意味着
    可能是提示词风格与模型不匹配，值得换一种提示词风格再试。
    """


class PauseRequested(Exception):
    """用户请求暂停：立即停止翻译。已完成的条目经 item_cb 实时落盘，
    缓存里保留了断点，下次运行可从暂停处继续。"""


class TranslationClient:
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(self, config: TranslationConfig):
        self.config = config
        self.request_count = 0       # 实际发出的 API 请求次数
        self.error_count = 0         # 失败的请求次数
        self.error_messages: list[str] = []  # 去重后的错误详情
        # 暂停信号（threading.Event，置位即暂停）。由 GUI 注入。
        self.pause_event = None
        # 会话内自适应的提示词风格：False=详细（初始值），True=精简。
        # 一旦观测到模型无法按详细提示词返回结构化结果，即切换并记住。
        self._compact_style = False
        # 自适应批大小（AIMD，类拥塞控制）：部分模型在较大批次上会生成几条
        # 就提前停止或格式错乱。与其反复发起注定失败的大批再折半（每次失败
        # 都浪费一整次生成），不如记住「近期稳定成功的批大小」，超过上限的
        # 组在发请求前就预先折半。成功后缓慢放大探测更大批次，失败立即减半。
        self._batch_cap = config.chunk_items or 40
        # 本地推理服务（Ollama / LM Studio / vLLM 等）没有云端限流问题，
        # 串行单请求时 GPU 大部分时间处于等待状态；并发多个批次可让
        # GPU 同时处理多路请求，吞吐明显提升。云端服务保持串行以免限流。
        if self.config.max_workers == 1:
            try:
                _host = (urlparse(self.config.base_url).hostname or "").lower()
            except Exception:  # noqa: BLE001
                _host = ""
            if _host in ("localhost", "127.0.0.1", "::1"):
                self.config.max_workers = 4

    def _check_pause(self) -> None:
        ev = self.pause_event
        if ev is not None and ev.is_set():
            raise PauseRequested()

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

    def chat(self, messages: list[dict], error_cb=None,
             max_tokens: int | None = None) -> str:
        """发起一次对话请求，返回 assistant 的文本内容。

        ``max_tokens`` 可为「注定被拒收的长回复」（如模型转成聊天模式后
        输出的大段解释）设置输出上限：回复会被截断而更快结束，省下
        等待整段无效生成的时间；正常译文远短于该上限，不受影响。
        """
        self.request_count += 1
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": False,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
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
        except OSError as e:
            # URLError（连接被拒/DNS 失败）、TimeoutError（读取超时，读取阶段
            # 由 http.client 直接抛出、不被 URLError 包裹）等都归到这里，
            # 统一附上可操作的中文排查提示。
            self.error_count += 1
            reason = getattr(e, "reason", e)
            try:
                host = urlparse(self.config.base_url).hostname or ""
            except Exception:  # noqa: BLE001
                host = ""
            msg = (f"无法连接翻译服务（{self.config.base_url}）: {reason}"
                   f"\n{tcp_error_hint(reason, host)}")
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
        item_cb=None,
    ) -> list[str]:
        """批量翻译文本，返回与输入等长的译文列表（失败项回退原文）。

        names: 需整体保留原文的人名/专有名词列表（翻译前保护为占位符）。
        progress_cb(done, total) 在每完成一个批次时回调，用于实时进度显示。
        error_cb(msg) 在每次请求失败时回调（去重），用于实时错误提示。
        item_cb(text, result) 每翻译完一条文本即回调 (原文, 译文)，
            供调用方实时落盘缓存实现断点续译（中断后跳过已翻译文本）。
        """
        results: list[str] = list(texts)
        indices = [i for i, t in enumerate(texts) if t.strip()]
        if not indices:
            return results
        if total is None:
            total = len(indices)
        payload_texts = [texts[i] for i in indices]

        chunks = self._chunk(payload_texts)
        if self.config.max_workers > 1 and len(chunks) > 1:
            translated = self._translate_chunks_parallel(
                chunks, target, error_cb, names, progress_cb, offset, total,
                item_cb=item_cb)
        else:
            translated = []
            done = 0
            for chunk in chunks:
                self._check_pause()
                chunk_out = self._translate_chunk(
                    chunk, target, error_cb, names)
                if item_cb:
                    for t, r in zip(chunk, chunk_out):
                        item_cb(t, r)
                translated.extend(chunk_out)
                done += len(chunk)
                if progress_cb:
                    progress_cb(offset + done, total)

        for idx, val in zip(indices, translated):
            results[idx] = val
        self._warn_if_untranslated(payload_texts, translated, target, error_cb)
        return results

    def _warn_if_untranslated(self, texts, translated, target, error_cb=None):
        if "中文" not in target and "chinese" not in target.lower():
            return
        n = sum(1 for t, r in zip(texts, translated)
                if _looks_untranslated(t, r))
        if n >= 3 and n >= len(texts) * 0.4:
            self._record_error(
                f"警告：{n}/{len(texts)} 条译文与原文完全相同——模型"
                f" {self.config.model} 可能不支持翻译到 {target}"
                "（部分机器翻译模型只支持特定语向，其他语言会原样返回）。"
                "建议改用支持该语言的通用指令模型。", error_cb)

    def _translate_chunks_parallel(
        self, chunks: list[list[str]], target: str, error_cb=None,
        names: list[str] | None = None, progress_cb=None,
        offset: int = 0, total: int | None = None,
        item_cb=None,
    ) -> list[str]:
        """并发翻译多个批次（按提交顺序收集，保持结果稳定）。"""
        done_prefix: list[int] = []
        acc = 0
        for c in chunks:
            acc += len(c)
            done_prefix.append(acc)

        results: list[list[str]] = [None] * len(chunks)  # type: ignore[list-item]

        def worker(i: int, chunk: list[str]) -> tuple[int, list[str]]:
            self._check_pause()
            return i, self._translate_chunk(chunk, target, error_cb, names)

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as ex:
            futures = [ex.submit(worker, i, c) for i, c in enumerate(chunks)]
            for f in futures:  # 按提交顺序取结果，避免多线程期间列表错位
                i, out = f.result()
                results[i] = out
                if item_cb:
                    for t, r in zip(chunks[i], out):
                        item_cb(t, r)
                if progress_cb:
                    progress_cb(offset + done_prefix[i], total)

        flat: list[str] = []
        for o in results:
            flat.extend(o)
        return flat

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
        self._translate_indices(
            list(range(len(texts))), texts, payload, ph_list, name_ph_list,
            target, error_cb, names, out, depth=0)
        return out

    def _translate_indices(self, pending, texts, payload, ph_list, name_ph_list,
                           target, error_cb, names, out, depth):
        """对一组条目执行「批译 → 校验 → 失败折半 → 逐条兜底」。

        折半的意义：批量越大，弱模型（上下文小 / 指令跟随能力差）越容易
        返回条数不符、格式错乱或占位符丢失的响应。把失败批次不断对半切开，
        成功率会快速回升，比反复重发同样大的批次更有效，也比直接逐条翻译
        省请求。折半层数有上限，最末层仍失败才退化为逐条（回退原文）。
        """
        pending = list(pending)
        # 预切：组大小超过自适应批上限时直接折半，不发起注定失败的大批请求
        # （失败的大批要白等一整次生成，是吞吐的最大浪费源）。
        if len(pending) > 1 and len(pending) > self._batch_cap:
            mid = len(pending) // 2
            for group in (pending[:mid], pending[mid:]):
                self._translate_indices(group, texts, payload, ph_list,
                                        name_ph_list, target, error_cb, names,
                                        out, depth)
            return
        tries = self.config.max_retries if depth == 0 else 1
        for attempt in range(tries):
            if not pending:
                return
            self._check_pause()
            sub_payload = [payload[i] for i in pending]
            try:
                # 条数校验与提示词风格自动切换都在 _request_json_array 内完成
                raw = self._request_json_array(sub_payload, target, error_cb)
            except ResponseFormatError as e:
                # 格式错误不是瞬时的：同一批重发必然再失败（内部已试过两种
                # 提示词），直接跳出重试、进入折半。AIMD 上限同步减半。
                self._batch_cap = max(1, len(sub_payload) // 2)
                self._record_error(str(e), error_cb)
                break
            except TranslationError as e:
                # 网络/HTTP 类瞬时错误：等待后原样重试
                self._record_error(str(e), error_cb)
                if attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue
            except Exception as e:  # noqa: BLE001 - 兜底，绝不炸掉整个任务
                self._record_error(str(e), error_cb)
                if attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue

            still_failed: list[int] = []
            for si, orig_i in enumerate(pending):
                ph, nph = ph_list[orig_i], name_ph_list[orig_i]
                restored = restore_text(raw[si], ph)
                restored = restore_names(restored, nph)
                if (not _placeholders_preserved(ph, restored)
                        or not _placeholders_preserved(nph, restored)
                        or _placeholder_residue(restored)
                        or _TEMPLATE_LEAK_RE.search(restored)
                        or _looks_like_meta_reply(texts[orig_i], restored)):
                    still_failed.append(orig_i)
                else:
                    out[orig_i] = restored
            if still_failed and len(still_failed) < len(pending):
                self._record_error(
                    f"本批 {len(pending)} 条中有 {len(still_failed)} 条校验失败"
                    f"（已缩小批次重试），示例: {texts[still_failed[0]][:50]!r}",
                    error_cb)
            elif still_failed and len(still_failed) == len(pending):
                # 整批都过不了校验 → 批大小仍偏大，同样减半上限
                self._batch_cap = max(1, len(sub_payload) // 2)
            elif len(pending) >= self._batch_cap:
                # AIMD：整批成功且已达上限 → 缓慢放大，探测更大的可行批次
                self._batch_cap = min(self.config.chunk_items or 40,
                                      self._batch_cap + 2)
            pending = still_failed
            if pending and attempt < tries - 1:
                time.sleep(1.2 * (attempt + 1))

        if not pending:
            return
        if len(pending) > 1 and depth < _MAX_SPLIT_DEPTH:
            mid = len(pending) // 2
            for group in (pending[:mid], pending[mid:]):
                self._translate_indices(group, texts, payload, ph_list,
                                        name_ph_list, target, error_cb, names,
                                        out, depth + 1)
            return
        # 最末层：逐条翻译（失败回退原文）
        for i in pending:
            self._check_pause()
            out[i] = self._translate_single(texts[i], target, error_cb, names)

    def _use_compact(self) -> bool:
        """当前是否使用精简提示词（True=精简，False=详细）。

        ``config.compact_prompt`` 显式指定时以其为准（True 强制精简、
        False 强制详细且不自动切换）；默认（None）按会话内观测到的模型
        行为自适应。
        """
        if self.config.compact_prompt is not None:
            return self.config.compact_prompt
        return self._compact_style

    @staticmethod
    def _build_array_messages(payload: list[str], target: str,
                              compact: bool) -> list[dict]:
        """组装批量翻译的 messages，按风格选用精简 / 详细系统提示词。"""
        template = _COMPACT_SYSTEM_PROMPT if compact else _SYSTEM_PROMPT
        tgt = _COMPACT_TARGETS.get(target, target) if compact else target
        system = template.format(target=tgt, ph=_PH_PREFIX + "0" + _PH_PREFIX)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _request_json_array(self, payload: list[str], target: str,
                            error_cb=None) -> list[str]:
        """请求并解析 JSON 数组；结构不符时自动换提示词风格各试一次。

        自适应依据是模型的**实际返回行为**（能否给出条数正确的 JSON 数组），
        与模型名称、厂商、是否本地部署无关，因此对任意模型通用：
        默认先用详细提示词；若模型返回的不是可解析的 JSON 数组（未经过
        指令微调的机器翻译模型的典型表现），自动改用精简提示词重试；
        精简风格一旦成功即记住，本会话后续请求直接走精简提示词。
        """
        styles = [self._use_compact()]
        if self.config.compact_prompt is None:
            styles.append(not styles[0])   # 自动模式：允许换一种风格再试
        last_err: Exception | None = None
        for i, compact in enumerate(styles):
            try:
                arr = self._parse_json_array(
                    self.chat(self._build_array_messages(payload, target, compact),
                              error_cb))
                if len(arr) != len(payload):
                    raise ResponseFormatError(
                        f"返回条目数不符（期望 {len(payload)}，实际 {len(arr)}）")
            except ResponseFormatError as e:
                last_err = e
                if i + 1 < len(styles):
                    self._record_error(
                        "模型未按%s提示词返回结构化结果，自动改用%s提示词重试…"
                        % ("精简" if compact else "详细",
                           "详细" if compact else "精简"), error_cb)
                continue
            # 只「升级」不「降级」：确认精简提示词有效后长期沿用，
            # 避免偶发的单次格式异常把结论反复改回详细提示词。
            if compact and self.config.compact_prompt is None:
                self._compact_style = True
            return arr
        raise last_err if last_err is not None else TranslationError("批译失败")

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
        try:
            arr = json.loads(text)
        except ValueError as e:
            raise ResponseFormatError(
                f"模型返回内容不是合法 JSON 数组（{str(e)[:80]}），"
                f"响应片段: {text[:120]!r}") from None
        if not isinstance(arr, list):
            raise ResponseFormatError("模型未返回 JSON 数组")
        return [str(x) for x in arr]

    # -- 单条翻译 ----------------------------------------------------------

    @staticmethod
    def _build_single_prompt(target: str, compact: bool) -> str:
        """组装单条翻译的系统提示词，按风格选用精简 / 详细版本。"""
        if compact:
            return _COMPACT_SINGLE_PROMPT.format(
                target=_COMPACT_TARGETS.get(target, target),
                ph=_PH_PREFIX + "0" + _PH_PREFIX)
        return _SINGLE_SYSTEM_PROMPT.format(
            target=target, ph=_PH_PREFIX + "0" + _PH_PREFIX)

    def _translate_single(self, text: str, target: str, error_cb=None,
                          names: list[str] | None = None) -> str:
        ntext, nph = protect_names(text, names or [])
        ptext, ph = protect_text(ntext)
        if not ptext.strip():
            return text
        # 与批量路径一致：默认用详细提示词，失败则换精简提示词再试一次
        styles = [self._use_compact()]
        if self.config.compact_prompt is None:
            styles.append(not styles[0])
        for style_i, compact in enumerate(styles):
            system = self._build_single_prompt(target, compact)
            # 首选风格沿用完整重试次数，换风格后只再试一次
            tries = self.config.max_retries if style_i == 0 else 1
            for attempt in range(tries):
                try:
                    resp = self.chat([
                        {"role": "system", "content": system},
                        {"role": "user", "content": ptext},
                    ], error_cb,
                        # 译文长度与原文同量级；超出数倍的几乎必是聊天式
                        # 回复（反正会被拒收），提前截断省时间
                        max_tokens=max(80, len(ptext) * 3 + 120))
                    restored = restore_text(_unwrap_single_response(resp), ph)
                    restored = restore_names(restored, nph)
                    # 四重把关：占位符齐全、无残留标记、无模板泄漏、非聊天语
                    if (_placeholders_preserved(ph, restored)
                            and _placeholders_preserved(nph, restored)
                            and not _placeholder_residue(restored)
                            and not _TEMPLATE_LEAK_RE.search(restored)
                            and not _looks_like_meta_reply(text, restored)):
                        if compact and self.config.compact_prompt is None:
                            self._compact_style = True
                        return restored
                except TranslationError as e:
                    self._record_error(str(e), error_cb)
                except Exception as e:
                    self._record_error(str(e), error_cb)
                if attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
        return text  # 回退原文（宁可留英文，也不要聊天语/乱码进游戏）


def build_client(config: TranslationConfig | None = None) -> TranslationClient:
    return TranslationClient(config or TranslationConfig())
