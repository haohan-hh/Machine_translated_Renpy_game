"""Portable settings shared by the Windows launcher, GUI and CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .translator import TranslationConfig

DEFAULT_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen3-8b-q4_k_m:latest"
DEFAULT_SERVICE = "Ollama 本地"
FILENAME = "rpytranslator_config.json"


def config_path(root: Path | None = None) -> Path:
    if root is None:
        root = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[1])
    # The distributed launcher historically saved its active profile beside
    # the exe. A source-only installation may use config/ from older releases.
    beside_launcher = root / FILENAME
    if beside_launcher.is_file():
        return beside_launcher
    return root / "config" / FILENAME


def load_settings(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("设置文件的内容不是配置对象")
    return value


def split_terms(value: str) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in value.replace("，", ",").split(",") if t.strip()))


def model_config(data: dict, *, url=None, key=None, model=None) -> TranslationConfig:
    from .translator import TranslationConfig

    saved_url = data.get("url") or DEFAULT_URL
    selected_url = (url or saved_url).strip().rstrip("/")
    same_service = selected_url == saved_url.strip().rstrip("/")
    # A command-line override can select a different provider. Credentials
    # and model names from the saved profile only belong to its own endpoint.
    selected_key = key if key is not None else (data.get("key", "") if same_service else "")
    selected_model = model or (data.get("model") if same_service else None)
    if not selected_model and not same_service:
        raise ValueError("更换 API 地址时请使用 --model 指定该服务的模型")
    return TranslationConfig(base_url=selected_url, api_key=selected_key,
                             model=(selected_model or DEFAULT_MODEL).strip())
