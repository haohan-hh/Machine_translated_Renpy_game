"""Connection and model readiness shared by connection tests and translation."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future
from enum import Enum
from http.client import HTTPException
from urllib.parse import urlparse


_OLLAMA_START_LOCK = threading.Lock()
_OLLAMA_START_PROCESS: subprocess.Popen | None = None
# A cold load reads several GB from disk before any chat tokens are produced.
_OLLAMA_LOAD_TIMEOUT = 600
_OLLAMA_LOAD_LOCK = threading.Lock()
_OLLAMA_LOADS: dict[tuple[str, str, str], Future[tuple[bool, str]]] = {}


def endpoint_parts(base_url: str) -> tuple[str, str, int]:
    """Return ``(scheme, hostname, port)`` or raise ``ValueError``."""
    parsed = urlparse((base_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("API 地址格式无效: %s" % base_url)
    return parsed.scheme, parsed.hostname, parsed.port or (
        443 if parsed.scheme == "https" else 80)


def is_local_endpoint(base_url: str) -> bool:
    """Whether an endpoint points at the local machine.

    Ollama and LM Studio do not require an API key by default.  Hostname
    matching is kept strict so a remote name such as ``localhost.example`` is
    not accidentally treated as local.
    """
    try:
        host = endpoint_parts(base_url)[1].strip("[]").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _ollama_origin(base_url: str) -> str:
    """Native routes for the managed local Ollama installation."""
    if not is_local_endpoint(base_url):
        return ""
    parsed = urlparse(base_url.strip())
    if (parsed.scheme == "http" and parsed.port == 11434
            and parsed.path.rstrip("/") in (
                "", "/v1", "/v1/chat/completions", "/chat/completions")):
        return "%s://%s" % (parsed.scheme, parsed.netloc)
    return ""


def openai_api_url(base_url: str, route: str) -> str:
    """Use the configured API prefix for both chat and the model list."""
    parsed = urlparse(base_url.strip())
    base_path = parsed.path.rstrip("/")
    if not base_path and _ollama_origin(base_url):
        base_path = "/v1"
    prefix = base_path.removesuffix("/chat/completions")
    return parsed._replace(path=prefix + "/" + route, fragment="").geturl()


def _ollama_project_root() -> Path:
    """Return the folder containing the launcher and bundled dependencies."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _ollama_server_command(root: Path) -> list[str]:
    """Find the CLI supplied with the desktop app or this portable installation."""
    candidates = []
    if os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Programs" / "Ollama" / "ollama.exe")
        candidates.append(root / "_dependencies" / "ollama_program" / "ollama.exe")
    for path in candidates:
        if path.is_file():
            return [str(path), "serve"]
    # Match absolute PATH entries on every supported Python/Windows version;
    # do not implicitly search the terminal's current game directory.
    binary = "ollama.exe" if os.name == "nt" else "ollama"
    for location in os.get_exec_path():
        directory = Path(location.strip('"'))
        candidate = directory / binary
        if directory.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate), "serve"]
    raise FileNotFoundError("未找到 Ollama 程序；请安装 Ollama 或将其加入 PATH。")


def _ollama_server_env(root: Path, origin: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OLLAMA_HOST"] = urlparse(origin).netloc
    if not env.get("OLLAMA_MODELS"):
        stores = [root / "_dependencies" / name / "models"
                  for name in ("ollama_user_data_live", "ollama_user_data")]
        for models in stores:
            if any(p.is_file() for p in models.glob("manifests*/*/*/*/*")):
                env["OLLAMA_MODELS"] = str(models)
                break
    env.setdefault("OLLAMA_NO_CLOUD", "1")
    return env


def ensure_ollama_service(
    base_url: str,
    *,
    timeout: float = 60.0,
    launcher: str | os.PathLike[str] | None = None,
) -> tuple[bool, str]:
    """Reuse a manually opened Ollama; start its API with the same store when needed."""
    try:
        endpoint_parts(base_url)
    except ValueError as exc:
        return False, str(exc)
    origin = _ollama_origin(base_url)
    if not origin:
        return True, ""

    def _version_ready() -> bool:
        status, payload, _detail = _json_get(
            origin + "/api/version", timeout=min(2.0, max(0.5, timeout)))
        return status == 200 and isinstance(payload, dict) and bool(payload.get("version"))

    if _version_ready():
        return True, ""

    global _OLLAMA_START_PROCESS
    with _OLLAMA_START_LOCK:
        if _version_ready():
            return True, ""
        root = _ollama_project_root()
        try:
            if launcher is None:
                command = _ollama_server_command(root)
                env = _ollama_server_env(root, origin)
            else:
                # Older callers can still explicitly provide their own launcher.
                script = root / Path(launcher)
                if not script.is_file():
                    return False, "Ollama 启动脚本不存在: %s" % script
                command = (["cmd.exe", "/d", "/c", str(script)]
                           if os.name == "nt" and script.suffix.lower() in (".cmd", ".bat") else [str(script)])
                env = None
            _OLLAMA_START_PROCESS = subprocess.Popen(
                command, cwd=str(root), env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return False, "无法启动 Ollama: %s" % exc

        deadline = time.monotonic() + max(0.0, timeout)
        exited_at = None
        while True:
            if _version_ready():
                return True, ""
            code = _OLLAMA_START_PROCESS.poll()
            if code not in (None, 0):
                # The tray can win the port between our readiness probe and
                # `serve`. Briefly wait for its API before reporting the exit.
                if exited_at is None:
                    exited_at = time.monotonic()
                if time.monotonic() >= min(deadline, exited_at + 3):
                    return False, "Ollama 启动进程已退出（退出码 %s），服务仍未就绪。请查看 Ollama 日志。" % code
            if time.monotonic() >= deadline:
                return False, "Ollama 服务未在 %.0f 秒内就绪" % timeout
            time.sleep(0.5)


def wait_for_tcp(
    host: str,
    port: int,
    *,
    timeout: float = 30.0,
    addresses: list[tuple] | None = None,
    retry_interval: float = 0.5,
) -> tuple[bool, Exception | None]:
    """Wait until a TCP listener accepts a connection.

    ``localhost`` can resolve to both ``::1`` and ``127.0.0.1``.  Every
    resolved address is tried on every round, so a service listening only on
    IPv4 is not reported as down merely because IPv6 was returned first.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    last_error: Exception | None = None
    resolved = addresses
    refresh_addresses = addresses is None
    while True:
        if resolved is None:
            try:
                resolved = socket.getaddrinfo(
                    host, port, 0, socket.SOCK_STREAM)
            except Exception as exc:  # DNS can fail transiently at startup.
                last_error = exc
                resolved = []
        for family, socktype, proto, _, sockaddr in resolved:
            try:
                sock = socket.socket(family, socktype, proto)
            except Exception as exc:  # noqa: BLE001 - try the next address
                last_error = exc
                continue
            remaining = max(0.1, deadline - time.monotonic())
            try:
                sock.settimeout(min(2.0, remaining))
                sock.connect(sockaddr)
                return True, None
            except Exception as exc:  # noqa: BLE001 - preserve OS detail
                last_error = exc
            finally:
                sock.close()
        if time.monotonic() >= deadline:
            return False, last_error
        time.sleep(min(max(0.05, retry_interval), max(0, deadline - time.monotonic())))
        # Re-resolve periodically so a hostname whose address changes during
        # service startup does not remain pinned to a stale result.
        if refresh_addresses:
            resolved = None


def _model_name_variants(name: str) -> set[str]:
    value = (name or "").strip().lower()
    if not value:
        return set()
    variants = {value}
    if value.endswith(":latest"):
        variants.add(value[:-len(":latest")])
    elif ":" not in value.rsplit("/", 1)[-1]:
        variants.add(value + ":latest")
    return variants


def _payload_model_names(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    raw = payload.get("models")
    if not isinstance(raw, list):
        raw = payload.get("data")
    if not isinstance(raw, list):
        return set()
    names: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            for key in ("name", "model", "id"):
                value = item.get(key)
                if isinstance(value, str):
                    names.add(value)
    return names


def _read_json(request: urllib.request.Request, timeout: float) -> tuple[int, object | None, str]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(body), ""
            except (TypeError, ValueError):
                return response.status, None, body[:300]
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, None, body or str(exc.reason)
    except (OSError, HTTPException) as exc:
        return 0, None, str(exc)


def _json_get(url: str, *, timeout: float, api_key: str = "") -> tuple[int, object | None, str]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", "Bearer " + api_key)
    return _read_json(request, timeout)


class ModelPrompt(Enum):
    ABSENT = "absent"
    PRESENT = "present"
    UNKNOWN = "unknown"
    REMOTE = "remote"


def _modelfile_prompt(text: str) -> ModelPrompt:
    """Read Ollama's generated directives, never examples inside a quoted TEMPLATE/LICENSE."""
    has_from = False
    template_state = ModelPrompt.ABSENT
    lines = iter(text.splitlines())
    for line in lines:
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or fields[0].startswith("#"):
            continue
        key, value = fields[0].upper(), fields[1].strip()
        if key == "MESSAGE":
            role_value = value.split(None, 1)
            if role_value[0].upper() == "SYSTEM":
                key = "SYSTEM"
            value = role_value[1] if len(role_value) == 2 else ""
        if value.startswith('"""'):
            content = value[3:]
            if '"""' not in content:
                parts = [content]
                for continuation in lines:
                    parts.append(continuation)
                    if '"""' in continuation:
                        break
                else:
                    return ModelPrompt.UNKNOWN
                content = "\n".join(parts)
            value = content.split('"""', 1)[0]
        elif value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if key == "SYSTEM" and value.strip():
            return ModelPrompt.PRESENT
        if key == "TEMPLATE":
            template_state = _template_prompt(value)
            if template_state == ModelPrompt.PRESENT:
                return template_state
        if key == "FROM" and value.strip():
            has_from = True
    return template_state if has_from else ModelPrompt.UNKNOWN


# Some chat templates supply fixed instructions even without a SYSTEM layer.
# Role delimiters or {{ .System }} alone only render a caller-provided message.
_FIXED_TEMPLATE_PROMPT = re.compile(
    r"(?:<\|im_start\|>system|<\|start_header_id\|>system<\|end_header_id\|>|<\|system\|>|<<SYS>>)"
    r"(?:\s|\\[rn])*[^\W\d_]"
    r"|(?:\{%[- ]*set\s+(?:default_)?system_(?:message|prompt)\s*=|\|\s*default\()\s*[\"'][^\W\d_]"
    r"|(?:^|}})\s*(?:you are\b|你是|你的任务是)", re.I | re.M)

_TEMPLATE_TAG = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.S)
_TEMPLATE_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`''', re.S)
_CHAT_DELIMITERS = re.compile(
    r"<\|[\w.:/-]+\|>|</?(?:s|think|tools|tool_call|tool_response)>|\[/?INST\]|<</?SYS>>|\\[nrt]"
    r"|\b(?:system|user|assistant|tool|name|arguments)\b", re.I)


def _template_prompt(template: str) -> ModelPrompt:
    """Recognize plain turn serialization without executing a server-supplied template."""
    active = True
    parents = []
    fragments = []
    for part in _TEMPLATE_TAG.split(template):
        if part.startswith('{#'):
            continue
        if part.startswith('{%'):
            action = part[2:-2].strip(' -\r\n\t')
            keyword = action.split(None, 1)[0] if action else ''
            if keyword == 'if':
                parents.append(active)
                # Qwen documents its tool protocol here. This app sends no tools.
                active = active and action != 'if tools'
            elif keyword in ('else', 'elif', 'endif'):
                if not parents:
                    return ModelPrompt.UNKNOWN
                active = parents.pop() if keyword == 'endif' else parents[-1]
            if active and keyword == 'set':
                # Assigned prose may be printed later as {{ variable }}.
                fragments.extend(_TEMPLATE_STRING.findall(action))
            elif active and keyword not in ('if', 'elif', 'else', 'endif', 'for', 'endfor'):
                # Includes, macros and output extensions can supply text outside
                # the literal fragments we can inspect without running a template.
                return ModelPrompt.UNKNOWN
            continue
        if not active:
            continue
        if part.startswith('{{'):
            expression = part[2:-2].strip(' -\r\n\t')
            if not expression.startswith('/*'):
                fragments.extend(_TEMPLATE_STRING.findall(expression))
        else:
            fragments.append(part)
    if parents:
        return ModelPrompt.UNKNOWN
    # An unfamiliar literal in a print expression, conditional fallback or plain
    # text might be an instruction in any language. Preserve the provider default.
    if _FIXED_TEMPLATE_PROMPT.search(template):
        return ModelPrompt.PRESENT
    return (ModelPrompt.UNKNOWN if any(re.search(r"[^\W\d_]", _CHAT_DELIMITERS.sub(' ', f)) for f in fragments)
            else ModelPrompt.ABSENT)


def _model_prompt_from_show(payload: object) -> ModelPrompt:
    if not isinstance(payload, dict) or payload.get("error"):
        return ModelPrompt.UNKNOWN
    system = payload.get("system", "")
    if not isinstance(system, str):
        return ModelPrompt.UNKNOWN
    if system.strip():
        return ModelPrompt.PRESENT
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return ModelPrompt.UNKNOWN
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            return ModelPrompt.UNKNOWN
        if message["role"].lower() == "system":
            content = message.get("content")
            if not isinstance(content, str):
                return ModelPrompt.UNKNOWN
            if content.strip():
                return ModelPrompt.PRESENT
    info = payload.get("model_info", {})
    if not isinstance(info, dict):
        return ModelPrompt.UNKNOWN
    template = payload.get("template", "")
    templates = [template]
    # Some providers expose a GGUF template only as tokenizer metadata. An
    # empty SYSTEM layer with a generic outer template is not sufficient.
    templates.extend(value for key, value in info.items() if isinstance(key, str)
                     and (key == 'tokenizer.chat_template' or key.startswith('tokenizer.chat_template.')))
    states = [_template_prompt(item) if isinstance(item, str) else ModelPrompt.UNKNOWN for item in templates]
    if ModelPrompt.PRESENT in states:
        return ModelPrompt.PRESENT
    template_state = ModelPrompt.UNKNOWN if ModelPrompt.UNKNOWN in states else ModelPrompt.ABSENT
    modelfile = payload.get("modelfile")
    if isinstance(modelfile, str) and modelfile.strip():
        directives = _modelfile_prompt(modelfile)
        return directives if directives is not ModelPrompt.ABSENT else template_state
    # Current Ollama omits empty optional fields. An unrelated JSON object or
    # proxy's generic success response must never count as a promptless model.
    if "system" in payload or (isinstance(template, str) and template.strip() and isinstance(payload.get("details"), dict)):
        return template_state
    return ModelPrompt.UNKNOWN


def selected_model_prompt(base_url: str, model: str, *, api_key: str = "", timeout: float = 4) -> ModelPrompt:
    """Inspect only the selected local service; other OpenAI-compatible APIs need not expose presets."""
    if not is_local_endpoint(base_url):
        return ModelPrompt.REMOTE
    parsed = urlparse(base_url.strip())
    if parsed.path.rstrip("/") not in ("", "/v1", "/chat/completions", "/v1/chat/completions"):
        return ModelPrompt.UNKNOWN
    req = urllib.request.Request(
        parsed._replace(path="/api/show", fragment="").geturl(),
        data=json.dumps({"model": model}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    status, payload, _detail = _read_json(req, timeout)
    return _model_prompt_from_show(payload) if status == 200 else ModelPrompt.UNKNOWN


def wait_for_model_registration(
    base_url: str,
    model: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 1.0,
    api_key: str = "",
) -> tuple[bool, str]:
    """Wait until a local model is listed by the service.

    Ollama exposes ``/api/tags`` and its OpenAI-compatible endpoint exposes
    ``/v1/models``.  The native endpoint is preferred because it is available
    before the first generation.  A non-Ollama local service may not expose
    either endpoint; in that case the check returns success and the caller's
    normal chat request remains the source of truth.
    """
    endpoint_parts(base_url)
    origin = _ollama_origin(base_url)
    urls = [openai_api_url(base_url, "models")]
    if origin:
        urls.insert(0, origin + "/api/tags")
    variants = _model_name_variants(model)
    deadline = time.monotonic() + max(0.0, timeout)
    unsupported: set[str] = set()
    last_detail = "模型尚未注册"
    while True:
        for url in urls:
            if url in unsupported:
                continue
            status, payload, detail = _json_get(
                url, timeout=min(5.0, max(0.5, deadline - time.monotonic())),
                api_key=api_key)
            if status in (404, 405):
                # Ollama can accept TCP briefly before all routes are
                # registered.  Treat a 404 on its standard port as transient;
                # custom local services can safely skip unsupported listings.
                if origin:
                    last_detail = "模型列表接口尚未注册"
                else:
                    unsupported.add(url)
                continue
            if status in (401, 403):
                # A custom local endpoint may protect model listing while its
                # chat endpoint is usable.  Do not block the user's test.
                unsupported.add(url)
                continue
            if status != 200:
                last_detail = detail or "模型列表接口暂不可用"
                continue
            if not origin and not (
                    isinstance(payload, dict)
                    and any(isinstance(payload.get(k), list) for k in ("models", "data"))):
                # A server may return its home page for unknown routes.
                unsupported.add(url)
                continue
            names = _payload_model_names(payload)
            if any(variants & _model_name_variants(name) for name in names):
                return True, ""
            last_detail = "模型 %s 尚未注册" % model

        if len(unsupported) == len(urls):
            return True, ""
        if time.monotonic() >= deadline:
            if origin:
                return False, (
                    "Ollama 服务已启动，但模型 %s 尚未出现在 /api/tags；"
                    "请确认模型已下载（ollama pull %s）。" % (model, model))
            return False, last_detail
        time.sleep(min(max(0.05, poll_interval), max(0, deadline - time.monotonic())))


def prepare_local_model(base_url: str, model: str, *, api_key: str = "",
                        log: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """Prepare the selected local service before a test or translation request."""
    try:
        endpoint_parts(base_url)
    except ValueError as exc:
        return False, str(exc)
    if not is_local_endpoint(base_url):
        return True, ""
    origin = _ollama_origin(base_url)
    if origin:
        if log:
            log("正在检查 Ollama 服务，未启动时会自动启动…")
        ready, detail = ensure_ollama_service(base_url)
        if not ready:
            return False, detail
    if log:
        log("正在确认本地模型是否已注册：%s" % model)
    ready, detail = wait_for_model_registration(base_url, model, api_key=api_key)
    if not ready or not origin:
        return ready, detail
    return _ensure_ollama_loaded(origin, model, api_key, log)


def _loaded_ollama_model(origin: str, model: str, api_key: str) -> dict | None:
    """Look up the selected tag, including Ollama's optional :latest suffix."""
    status, payload, _detail = _json_get(origin + "/api/ps", timeout=2, api_key=api_key)
    if status != 200 or not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    wanted = _model_name_variants(model)
    for row in models:
        if not isinstance(row, dict):
            continue
        names = (row.get("name"), row.get("model"))
        if any(wanted & _model_name_variants(n) for n in names if isinstance(n, str)):
            return row
    return None


def _load_ollama_model(origin: str, model: str, api_key: str,
                       log: Callable[[str], None] | None) -> tuple[bool, str]:
    if _loaded_ollama_model(origin, model, api_key) is not None:
        return True, ""
    if log:
        log(f"Ollama 正在加载模型 {model}，首次加载最多等待 {_OLLAMA_LOAD_TIMEOUT} 秒…")
    # Ollama documents an empty /api/generate request for preloading. Leave
    # context size, GPU offload and keep-alive at the user's Ollama settings.
    request = urllib.request.Request(
        origin + "/api/generate",
        data=json.dumps({"model": model, "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", "Bearer " + api_key)
    started = time.monotonic()
    status, payload, detail = _read_json(request, _OLLAMA_LOAD_TIMEOUT)
    elapsed = time.monotonic() - started
    if (status == 200 and isinstance(payload, dict) and payload.get("done") is True
            and not payload.get("error") and payload.get("done_reason") in (None, "", "load")):
        if log:
            log(f"Ollama 已加载模型 {model}，用时 {elapsed:.1f} 秒。")
        return True, ""
    if not detail and isinstance(payload, dict):
        detail = str(payload.get("error") or "")
    reason = detail or "模型未返回加载完成标记"
    if status:
        reason = f"HTTP {status}，{reason}"
    return False, f"Ollama 加载模型 {model} 失败（等待 {elapsed:.1f} 秒）：{reason}"


def _ensure_ollama_loaded(origin: str, model: str, api_key: str,
                          log: Callable[[str], None] | None) -> tuple[bool, str]:
    """Concurrent tests/jobs share this load and its result, including failures."""
    key = origin, model.strip().lower().removesuffix(":latest"), api_key
    with _OLLAMA_LOAD_LOCK:
        existing = _OLLAMA_LOADS.get(key)
        if existing is None:
            future: Future[tuple[bool, str]] = Future()
            _OLLAMA_LOADS[key] = future
    if existing is not None:
        if log:
            log(f"正在等待 Ollama 完成已有的模型加载请求：{model}…")
        return existing.result()
    try:
        result = _load_ollama_model(origin, model, api_key, log)
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _OLLAMA_LOAD_LOCK:
            del _OLLAMA_LOADS[key]


def ollama_gpu_status(base_url: str, model: str, *, api_key: str = "") -> str:
    """Return VRAM reported for this model after generation, when available."""
    origin = _ollama_origin(base_url)
    if origin:
        row = _loaded_ollama_model(origin, model, api_key)
        size = row.get("size_vram") if row else None
        if type(size) is int and size >= 0:
            return "Ollama 报告模型显存占用 %.1f MiB" % (size / (1024 * 1024))
    return ""

