"""运行时设置。

API Key 不写进 SQLite（DB 会被导出/分享），单独放 data/secrets.json。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = PROJECT_ROOT / "data" / "secrets.json"

DEFAULTS = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "provider_mode": "auto",  # auto | live | mock
    "fuzzy_threshold": "0.75",  # 对应需求改动 ④
    "max_output_chars": "30000",
}


def get(key: str) -> str:
    return db.get_setting(key, DEFAULTS.get(key, ""))


def set_value(key: str, value) -> None:
    db.set_setting(key, str(value))


def get_float(key: str, fallback: float = 0.0) -> float:
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return fallback


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(float(get(key)))
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------- API Key


def get_api_key() -> str:
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        return (data.get("api_key") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


def set_api_key(key: str) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    try:
        payload = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload["api_key"] = (key or "").strip()
    SECRETS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        SECRETS_PATH.chmod(0o600)
    except OSError:
        pass


def mask_key(key: str) -> str:
    if not key:
        return "（未填写）"
    if len(key) <= 8:
        return key[:2] + "****"
    return f"{key[:4]}****{key[-4:]}"


def snapshot() -> dict:
    return {
        "base_url": get("base_url"),
        "model": get("model"),
        "provider_mode": get("provider_mode"),
        "fuzzy_threshold": get_float("fuzzy_threshold", 0.75),
        "max_output_chars": get_int("max_output_chars", 30000),
        "has_key": bool(get_api_key()),
    }
