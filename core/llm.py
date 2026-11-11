"""OpenAI 兼容 LLM 客户端 + 任务分发。

只用 /chat/completions，不依赖 openai SDK，任何兼容端点都能换。
"""
from __future__ import annotations

import json
import os
import re

import requests

from . import mock_llm, settings


class LLMError(RuntimeError):
    """网络/鉴权/额度类错误。"""


class LLMContractError(LLMError):
    """模型返回的不是可用 JSON。UI 会退化成“手动填写”。"""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def provider() -> str:
    """返回 'mock' 或 'live'。对应“没配 Key 也能跑通闭环”。"""
    if os.environ.get("COGNITION_FORCE_MOCK", "").strip().lower() in ("1", "true", "yes"):
        return "mock"  # demo.py 用，不改动用户在设置里的选择
    mode = (settings.get("provider_mode") or "auto").lower()
    if mode == "mock":
        return "mock"
    if mode == "live":
        return "live"
    return "live" if settings.get_api_key() else "mock"


def call(
    task: str,
    payload: dict,
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tokens: int = 2600,
) -> dict:
    if provider() == "mock":
        return mock_llm.handle(task, payload)
    return _live_json(system, user, temperature=temperature, max_tokens=max_tokens)


def complete_text(prompt: str, system: str = "你是一个简洁的助手。", max_tokens: int = 60) -> str:
    if provider() == "mock":
        return "mock 模式：未联网。"
    body = {
        "model": settings.get("model"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    data = _post(body)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMContractError("返回结构不符合 OpenAI 兼容格式", json.dumps(data)[:2000]) from exc


def _live_json(system: str, user: str, temperature: float, max_tokens: int) -> dict:
    key = settings.get_api_key()
    if not key:
        raise LLMError("还没有配置 API Key，请在「设置」里填写，或切到演示模式。")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    body = {
        "model": settings.get("model"),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    first = _post(body)
    try:
        return parse_json(_content_of(first))
    except LLMContractError:
        pass

    repair = {
        "model": settings.get("model"),
        "messages": messages
        + [
            {"role": "assistant", "content": _content_of(first)[:2000]},
            {
                "role": "user",
                "content": "上面的输出不是合法 JSON。请只输出一个 JSON 对象，不要解释、不要 markdown 代码块。",
            },
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    second = _post(repair)
    return parse_json(_content_of(second))


def _post(body: dict) -> dict:
    base = (settings.get("base_url") or "").rstrip("/")
    if not base:
        raise LLMError("还没有配置 base_url。")
    url = base + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.get_api_key()}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=120)
    except requests.RequestException as exc:
        raise LLMError(f"请求失败：{exc}") from exc

    if resp.status_code >= 400:
        snippet = resp.text[:400]
        raise LLMError(f"接口返回 {resp.status_code}：{snippet}")
    try:
        return resp.json()
    except ValueError as exc:
        raise LLMContractError("接口返回的不是 JSON", resp.text[:2000]) from exc


def _content_of(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMContractError("返回结构不符合 OpenAI 兼容格式", json.dumps(data)[:2000]) from exc


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> dict:
    """尽量从一个可能带废话的回复里抠出 JSON 对象。"""
    if not text or not text.strip():
        raise LLMContractError("模型返回空内容", text)

    candidates = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)

    for chunk in candidates:
        chunk = chunk.strip()
        start = chunk.find("{")
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(chunk)):
            ch = chunk[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(chunk[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
    raise LLMContractError("模型返回内容里找不到合法 JSON", text[:2000])
