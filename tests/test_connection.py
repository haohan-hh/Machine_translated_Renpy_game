"""Readiness checks for local OpenAI-compatible services."""
from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from rpytranslator.connection import (
    is_local_endpoint,
    wait_for_model_registration,
    wait_for_tcp,
)


class ConnectionReadinessTests(unittest.TestCase):
    def test_local_endpoint_detection_allows_empty_ollama_key(self) -> None:
        self.assertTrue(is_local_endpoint("http://localhost:11434/v1"))
        self.assertTrue(is_local_endpoint("http://127.0.0.1:11434/v1"))
        self.assertTrue(is_local_endpoint("http://[::1]:11434/v1"))
        self.assertFalse(is_local_endpoint("https://localhost.example/v1"))
        self.assertFalse(is_local_endpoint("https://api.example.test/v1"))

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

