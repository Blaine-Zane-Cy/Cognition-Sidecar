"""自由对话：像 LLM 一样随时开聊，但带着「对接工作 Agent」的自觉。

和审阅流水线并列，不互相串台：
- 数据单独两张表（chats / chat_messages），不复用 messages —— 后者全是
  session_id / review_id / branch_id 的审阅语义，硬塞进来会污染主干查询。
- 上下文只有三样：用户沉淀的偏好规则、这一支的对话历史、刚说的这句。
  不自动拉全文、不自动拉评审结果，避免又一次"模型自己把一堆东西塞进来"。
- 失败也留一条明示的 system 消息，不让界面表现成"你的话没人接"。
"""
from __future__ import annotations

from . import db, llm, prompts

HISTORY_TURNS = 12
HISTORY_CHARS = 700
MAX_RULES = 12


def rules_context() -> list[dict]:
    """把启用的偏好规则交给模型。这是用户的意志，不是装饰。"""
    return [
        {"规则": r["text"], "适用范围": r["scope"]}
        for r in db.list_rules(status="active")[:MAX_RULES]
    ]


def build_payload(cid: str, text: str) -> dict:
    history = [
        {"role": m["role"], "content": (m["content"] or "")[:HISTORY_CHARS]}
        for m in db.list_chat_messages(cid, limit=HISTORY_TURNS)
        if m["role"] in ("user", "assistant")
    ]
    return {
        "用户偏好规则": rules_context(),
        "最近对话": history,
        "用户刚说的话": text,
    }


def respond(cid: str, text: str) -> str:
    """一轮对话。返回助手说的话；失败时留一条 system 消息后抛出。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("写点东西再发。")
    if not db.get_chat(cid):
        raise ValueError("这个对话已经不在了。")

    db.add_chat_message(cid, "user", text)
    payload = build_payload(cid, text)
    try:
        raw = llm.call(
            "free_chat",
            payload,
            prompts.FREE_CHAT_SYSTEM,
            prompts.free_chat_user(payload),
            temperature=0.6,
            max_tokens=900,
        )
        say = str((raw or {}).get("say") or "").strip()
        if not say:
            raise llm.LLMContractError("自由对话没有返回 say", str(raw)[:600])
    except Exception as exc:  # noqa: BLE001
        db.add_chat_message(
            cid, "system", f"（这一轮没接上：{type(exc).__name__}：{str(exc)[:160]}）"
        )
        raise
    db.add_chat_message(cid, "assistant", say)
    return say


def handoff_to_workbench(text: str) -> None:
    """把聊清楚的一段想法放进工作台的起始输入框，等用户自己切过去。

    刻意不自动跳页：改 segmented_control 的 widget key 容易和 Streamlit 的
    实例化顺序打架，而且"什么时候开始正式委托"应该由用户按那一下。
    """
    db.save_draft("workbench", "brief", (text or "").strip())
