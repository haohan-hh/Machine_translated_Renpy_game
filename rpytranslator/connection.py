"""Connection/readiness helpers used by the GUI.

The GUI runs these checks in a worker thread, so this module deliberately has
no WinUI dependency.  Keeping the polling here also makes the Ollama startup
race testable without opening the application window.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


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
        time.sleep(min(max(0.05, retry_interval), deadline - time.monotonic()))
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
    else:
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


def _json_get(url: str, *, timeout: float, api_key: str = "") -> tuple[int, object | None, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(url, headers=headers, method="GET")
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
        return exc.code, None, body or str(exc.reason)
    except OSError as exc:
        return 0, None, str(exc)


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
    scheme, _host, port = endpoint_parts(base_url)
    parsed = urlparse(base_url)
    origin = "%s://%s" % (scheme, parsed.netloc)
    urls = [origin + "/api/tags", origin + "/v1/models"]
    variants = _model_name_variants(model)
    deadline = time.monotonic() + max(0.0, timeout)
    unsupported: set[str] = set()
    last_detail = "模型尚未注册"
    likely_ollama = port == 11434

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
                if likely_ollama:
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
            names = _payload_model_names(payload)
            if any(variants & _model_name_variants(name) for name in names):
                return True, ""
            last_detail = "模型 %s 尚未注册" % model

        if len(unsupported) == len(urls):
            return True, ""
        if time.monotonic() >= deadline:
            if port == 11434:
                return False, (
                    "Ollama 服务已启动，但模型 %s 尚未出现在 /api/tags；"
                    "请确认模型已下载（ollama pull %s）。" % (model, model))
            return False, last_detail
        time.sleep(min(max(0.05, poll_interval), deadline - time.monotonic()))

