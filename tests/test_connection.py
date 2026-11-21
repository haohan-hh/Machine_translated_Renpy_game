"""Readiness checks for local OpenAI-compatible services."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from rpytranslator.connection import (
    ensure_ollama_service,
    is_local_endpoint,
    ollama_gpu_status,
    openai_api_url,
    prepare_local_model,
    wait_for_model_registration,
    wait_for_tcp,
)


class ConnectionReadinessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows launcher integration")
    def test_cmd_launches_with_inherited_store_and_waits_without_console_input(self):
        # Run the real batch logic on a fresh port. Only the tray executable,
        # its arguments and the port are substituted; the user's app is never used.
        original = (Path(__file__).resolve().parents[1] / "start_ollama_fixed.cmd").read_text()
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        tray_name = f"tray-fixture-{os.getpid()}.exe"
        for mode in ("already-ready", "configured", "bundled", "ollama-default"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                record = root / "tray.log"
                probes = []
                delay = 1.3 if mode == "configured" else 0

                class Version(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_GET(self):
                        probes.append(time.monotonic())
                        ready = mode == "already-ready" or (
                            record.exists() and time.time() - record.stat().st_mtime >= delay)
                        self.send_response(200 if ready else 503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"version":"test"}' if ready else b'{"error":"starting"}')

                with HTTPServer(("127.0.0.1", 0), Version) as api:
                    alt = f"127.0.0.1:{api.server_port}"
                    batch = original.replace("127.0.0.1:11434", alt).replace("ollama app.exe", tray_name)
                    batch = batch.replace("--hide --fast-startup", "/d /c capture.cmd")
                    wrapper = root / "start_ollama_fixed.cmd"
                    wrapper.write_text(batch, encoding="ascii")
                    env = dict(os.environ, LOCALAPPDATA=str(root / "appdata"), TRAY_LOG=str(record))
                    env.pop("OLLAMA_MODELS", None)
                    bundled = root / "_dependencies" / "ollama_user_data_live" / "models"
                    if mode in ("bundled", "configured"):
                        (bundled / "manifests" / "registry.ollama.ai").mkdir(parents=True)
                    selected = None
                    if mode == "configured":
                        selected = root / "custom model store"
                        (selected / "manifests" / "private-registry").mkdir(parents=True)
                        env["OLLAMA_MODELS"] = str(selected)
                    elif mode == "bundled":
                        selected = bundled
                    if mode != "already-ready":
                        app = Path(env["LOCALAPPDATA"]) / "Programs" / "Ollama"
                        app.mkdir(parents=True)
                        shutil.copyfile(cmd, app / tray_name)
                        (app / "capture.cmd").write_text(
                            '@echo off\nset OLLAMA_MODELS > "%TRAY_LOG%" 2>nul\n', encoding="ascii")
                    threading.Thread(target=api.serve_forever, daemon=True).start()
                    try:
                        result = subprocess.run([str(cmd), "/d", "/c", str(wrapper)], cwd=root, env=env,
                                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                    finally:
                        api.shutdown()
                self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
                if mode == "already-ready":
                    self.assertFalse(record.exists())
                    self.assertEqual(len(probes), 1)
                else:
                    logged = record.read_text(encoding="mbcs").strip()
                    self.assertEqual(logged, f"OLLAMA_MODELS={selected}" if selected else "")
                    self.assertGreaterEqual(probes[-1] - probes[0], delay)
                    self.assertLess(len(probes), 12)

    def test_local_endpoint_detection_allows_empty_ollama_key(self) -> None:
        self.assertTrue(is_local_endpoint("http://localhost:11434/v1"))
        self.assertTrue(is_local_endpoint("http://127.0.0.1:11434/v1"))
        self.assertTrue(is_local_endpoint("http://[::1]:11434/v1"))
        self.assertFalse(is_local_endpoint("https://localhost.example/v1"))
        self.assertFalse(is_local_endpoint("https://api.example.test/v1"))

    def test_ollama_service_starts_wrapper_when_api_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "start_ollama_fixed.cmd"
            wrapper.write_text("@echo off\n", encoding="utf-8")
            replies = iter((
                (0, None, "connection refused"),
                (0, None, "connection refused"),
                (200, {"version": "0.40.13"}, ""),
            ))

            class FakeProcess:
                def poll(self):
                    return None

            with patch("rpytranslator.connection._json_get",
                       side_effect=lambda *args, **kwargs: next(replies)), \
                    patch("rpytranslator.connection.subprocess.Popen",
                          return_value=FakeProcess()) as popen:
                ready, detail = ensure_ollama_service(
                    "http://127.0.0.1:11434/v1", timeout=1,
                    launcher=wrapper)

        self.assertTrue(ready)
        self.assertEqual(detail, "")
        popen.assert_called_once()
        # Both the GUI and direct use inherit the user's environment. The
        # launcher chooses the bundled store only if OLLAMA_MODELS is unset.
        self.assertIsNone(popen.call_args.kwargs.get("env"))

    def test_ollama_service_does_not_launch_when_api_is_ready(self) -> None:
        with patch("rpytranslator.connection._json_get",
                   return_value=(200, {"version": "0.40.13"}, "")), \
                patch("rpytranslator.connection.subprocess.Popen") as popen:
            ready, detail = ensure_ollama_service(
                "http://localhost:11434/v1")

        self.assertTrue(ready)
        self.assertEqual(detail, "")
        popen.assert_not_called()

    def test_concurrent_ollama_readiness_checks_launch_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "start_ollama_fixed.cmd"
            wrapper.write_text("@echo off\n", encoding="utf-8")
            initial_checks = threading.Barrier(2)
            ready = threading.Event()
            call_lock = threading.Lock()
            calls = 0

            def fake_get(*args, **kwargs):
                nonlocal calls
                with call_lock:
                    calls += 1
                    check_number = calls
                if check_number <= 2:
                    initial_checks.wait(timeout=2)
                if ready.is_set():
                    return 200, {"version": "0.40.13"}, ""
                return 0, None, "connection refused"

            class FakeProcess:
                def poll(self):
                    return None

            def launch(*args, **kwargs):
                threading.Timer(0.1, ready.set).start()
                return FakeProcess()

            with patch("rpytranslator.connection._json_get", side_effect=fake_get), \
                    patch("rpytranslator.connection.subprocess.Popen",
                          side_effect=launch) as popen:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(
                        ensure_ollama_service,
                        "http://127.0.0.1:11434/v1",
                        timeout=2,
                        launcher=wrapper,
                    ) for _ in range(2)]
                    results = [future.result(timeout=3) for future in futures]

        self.assertEqual(results, [(True, ""), (True, "")])
        popen.assert_called_once()

    def test_model_registration_waits_until_model_appears(self) -> None:
        replies = iter((
            (200, {"models": []}, ""),
            (200, {"models": [{"name": "qwen3-8b-q4_k_m:latest"}]}, ""),
        ))
        with patch(
            "rpytranslator.connection._json_get",
            side_effect=lambda *args, **kwargs: next(replies),
        ), patch("rpytranslator.connection.time.sleep"):
            ready, detail = wait_for_model_registration(
                "http://127.0.0.1:11434/v1",
                "qwen3-8b-q4_k_m",
                timeout=1,
                poll_interval=0,
            )
        self.assertTrue(ready)
        self.assertEqual(detail, "")

    def test_ollama_404_is_treated_as_startup_race(self) -> None:
        replies = iter((
            (404, None, "route not ready"),
            (404, None, "route not ready"),
            (200, {"models": [{"model": "qwen3:8b"}]}, ""),
        ))
        with patch(
            "rpytranslator.connection._json_get",
            side_effect=lambda *args, **kwargs: next(replies),
        ), patch("rpytranslator.connection.time.sleep"):
            ready, detail = wait_for_model_registration(
                "http://127.0.0.1:11434/v1", "qwen3:8b",
                timeout=1, poll_interval=0)
        self.assertTrue(ready)
        self.assertEqual(detail, "")

    def test_unknown_local_api_does_not_block_on_unsupported_model_listing(self) -> None:
        with patch(
            "rpytranslator.connection._json_get",
            return_value=(404, None, "not found"),
        ):
            ready, detail = wait_for_model_registration(
                "http://127.0.0.1:1234/v1", "any-model", timeout=1)
        self.assertTrue(ready)
        self.assertEqual(detail, "")

    def test_model_and_chat_urls_share_prefix_even_for_full_chat_url(self) -> None:
        base = "http://localhost:1234/service/v1/chat/completions?version=beta"
        with patch("rpytranslator.connection._json_get",
                   return_value=(200, {"data": [{"id": "my-model"}]}, "")) as get:
            self.assertEqual(prepare_local_model(base, "my-model", api_key="test"), (True, ""))
        self.assertEqual(get.call_args.args[0], "http://localhost:1234/service/v1/models?version=beta")
        self.assertEqual(get.call_args.kwargs["api_key"], "test")
        self.assertEqual(openai_api_url(base, "chat/completions"), base)
        self.assertEqual(openai_api_url("http://localhost:11434/", "chat/completions"),
                         "http://localhost:11434/v1/chat/completions")

    def test_prepare_reports_launcher_failure_and_skips_remote_endpoints(self) -> None:
        with patch("rpytranslator.connection.ensure_ollama_service",
                   return_value=(False, "未能启动")) as launch, \
                patch("rpytranslator.connection.wait_for_model_registration") as listing:
            self.assertEqual(prepare_local_model("https://example.test/v1", "m"), (True, ""))
            launch.assert_not_called()
            self.assertEqual(prepare_local_model("http://localhost:11434/v1", "m"),
                             (False, "未能启动"))
            listing.assert_not_called()

    def test_local_service_home_page_is_not_used_as_model_listing(self) -> None:
        with patch("rpytranslator.connection._json_get", return_value=(200, None, "<html>")) as get:
            self.assertEqual(prepare_local_model("http://localhost:1234/prefix", "m"), (True, ""))
        get.assert_called_once()

    def test_registered_cold_model_loads_before_chat_and_warm_model_skips_the_post(self) -> None:
        class Response(io.BytesIO):
            status = 200

        model = "qwen3-8b-q4_k_m"
        base = "http://localhost:11434"
        requests = []
        running = [{"name": "unrelated:latest"}]

        def serve(req, *, timeout):
            requests.append((req, timeout))
            name = req.full_url.removeprefix(base + "/api/")
            if name == "version":
                value = {"version": "0.40.13"}
            elif name == "tags":
                value = {"models": [{"model": model + ":latest"}]}
            elif name == "ps":
                value = {"models": running}
            elif name == "generate":
                self.assertEqual(req.get_method(), "POST")
                self.assertEqual(json.loads(req.data), {"model": model, "stream": False})
                self.assertEqual(req.get_header("Authorization"), "Bearer local-key")
                self.assertGreater(timeout, 180)
                running.append({"name": model + ":latest"})
                value = {"done": True, "done_reason": "load", "response": ""}
            else:
                self.fail(f"unexpected request: {name}")
            return Response(json.dumps(value).encode())

        logs = []
        with patch("urllib.request.urlopen", side_effect=serve):
            for _ in range(2):
                self.assertEqual(prepare_local_model(base + "/v1", model, api_key="local-key",
                                                     log=logs.append), (True, ""))
        paths = [req.full_url.rsplit("/", 1)[1] for req, _ in requests]
        self.assertEqual(paths, ["version", "tags", "ps", "generate", "version", "tags", "ps"])
        self.assertTrue(any("正在加载" in message for message in logs))
        self.assertTrue(any("已加载" in message for message in logs))

    def test_overlapping_prepares_share_load_failure_and_next_attempt_can_retry(self) -> None:
        model = "qwen3:8b"
        base = "http://localhost:11434/v1"
        started, waiting = threading.Event(), threading.Event()

        def load(request, timeout):
            started.set()
            self.assertTrue(waiting.wait(3))
            return 500, None, "runner out of memory"

        def log(message):
            if "已有的模型加载" in message:
                waiting.set()

        with patch("rpytranslator.connection.ensure_ollama_service", return_value=(True, "")), \
                patch("rpytranslator.connection.wait_for_model_registration", return_value=(True, "")), \
                patch("rpytranslator.connection._json_get", return_value=(404, None, "old /api/ps")), \
                patch("rpytranslator.connection._read_json", side_effect=load) as post:
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(prepare_local_model, base, model)
                self.assertTrue(started.wait(3))
                second = pool.submit(prepare_local_model, base, model, log=log)
                failures = [job.result(timeout=4) for job in (first, second)]
            post.assert_called_once()
            self.assertEqual(failures[0], failures[1])
            self.assertFalse(failures[0][0])
            self.assertIn("runner out of memory", failures[0][1])
            post.side_effect = None
            for payload, ok in (({"done": False}, False), ({"done": True}, True)):
                post.return_value = (200, payload, "")
                self.assertIs(prepare_local_model(base, model)[0], ok)

    def test_gpu_status_uses_only_exact_loaded_model_and_explicit_vram_size(self) -> None:
        url = "http://localhost:11434/v1"
        rows = [{"model": "qwen3:8b", "size_vram": 1900 * 1024 * 1024},
                {"name": "qwen3-8b-q4_k_m:latest", "size_vram": 128 * 1024 * 1024},
                {"model": "hy-mt2"},
                {"model": "cpu:latest", "size_vram": 0}]
        with patch("rpytranslator.connection._json_get",
                   return_value=(200, {"models": rows}, "")) as get:
            self.assertEqual(ollama_gpu_status("https://example.test/v1", "qwen3:8b"), "")
            get.assert_not_called()
            self.assertIn("128.0 MiB", ollama_gpu_status(url, "qwen3-8b-q4_k_m"))
            self.assertIn("0.0 MiB", ollama_gpu_status(url, "cpu"))
            self.assertEqual(ollama_gpu_status(url, "hy-mt2"), "")
            self.assertEqual(ollama_gpu_status(url, "qwen3:14b"), "")
            self.assertEqual(get.call_args.args[0], "http://localhost:11434/api/ps")

    def test_tcp_wait_tries_ipv4_after_ipv6_refusal(self) -> None:
        addresses = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 11434, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 11434)),
        ]

        class FakeSocket:
            attempts = []

            def __init__(self, family, socktype, proto):
                self.family = family
                self.socktype = socktype
                self.proto = proto

            def settimeout(self, value):
                pass

            def connect(self, sockaddr):
                self.attempts.append(sockaddr[0])
                if sockaddr[0] == "::1":
                    raise OSError(10061, "connection refused")

            def close(self):
                pass

        with patch("rpytranslator.connection.socket.socket", FakeSocket):
            ready, error = wait_for_tcp(
                "localhost", 11434, timeout=1, addresses=addresses)
        self.assertTrue(ready)
        self.assertIsNone(error)
        self.assertEqual(FakeSocket.attempts, ["::1", "127.0.0.1"])


if __name__ == "__main__":
    unittest.main()

