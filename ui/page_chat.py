"""自由对话：像 LLM 那样随时开聊，不要求先走「构想 → 委托 → 贴结果」。

和审阅流水线并列，不替代它：
- 这里负责把模糊的想法聊清楚、把 Agent 交回来的东西想明白；
- 聊到能交给 Agent 了，用「转成委托」把这段放进工作台的起始输入框；
- 工作台那边依旧做硬槽位、验收标准、锚定审阅和分支 —— 那是侧车真正护住
  理解权的地方，不能为了"像 ChatGPT"就把它拆掉。
"""
from __future__ import annotations

import streamlit as st

from core import db, free_chat, llm
from ui import components, layout

QUICK = [
    ("帮我把一个模糊的想法理清楚", "我有个想法还很模糊，帮我理清楚："),
    ("想让 Agent 帮我做一件事", "我想让工作 Agent 帮我做一件事，不知道该怎么交代："),
    ("看不懂 Agent 交回来的东西", "工作 Agent 交回来的结果我有点看不懂，想先弄明白："),
]


def render() -> None:
    _prefill()
    cid = _current_chat()
    _topic_bar(cid)
    rows = db.list_chat_messages(cid)

    if not rows:
        _empty_state()
    else:
        for m in rows:
            if m["role"] == "system":
                st.warning(m["content"])
                continue
            with st.chat_message("user" if m["role"] == "user" else "assistant"):
                st.markdown(m["content"])

    last = next((m for m in reversed(rows) if m["role"] == "assistant"), None)
    if last:
        st.html('<div style="height:4px;"></div>')
        if st.button("把最后这段转成委托", key=f"handoff_{cid}",
                     use_container_width=layout.narrow()):
            free_chat.handoff_to_workbench(last["content"])
            st.success("已经放进工作台的起始输入框。切到「工作台」就能看到，改一改再开始澄清。")

    text = st.chat_input("想到什么就说什么…", key=f"chat_input_{cid}")
    if text:
        ok, _ = components.call(
            free_chat.respond, cid, text.strip(), spinner="正在想…"
        )
        if ok:
            st.rerun()


def _prefill() -> None:
    """空状态里的快捷提问：按钮只是存一句话，真正的一轮对话仍走同一条路径。"""
    q = st.session_state.pop("chat_prefill", None)
    if not q:
        return
    cid = _current_chat()
    ok, _ = components.call(free_chat.respond, cid, q, spinner="正在想…")
    if ok:
        st.rerun()


def _current_chat() -> str:
    cid = st.session_state.get("chat_id")
    if cid and db.get_chat(cid):
        return cid
    recent = db.list_chats(1)
    cid = recent[0]["id"] if recent else db.create_chat()
    st.session_state["chat_id"] = cid
    return cid


def _topic_bar(cid: str) -> None:
    chats = db.list_chats(30)
    if not any(c["id"] == cid for c in chats):
        chats = [db.get_chat(cid)] + chats
    labels = {c["id"]: (c["title"] or "新对话")[:24] for c in chats if c}
    options = [c["id"] for c in chats if c]
    provider = "演示替身" if llm.provider() == "mock" else "真实模型"

    st.html(
        '<div style="display:flex;align-items:baseline;gap:8px;margin:0 0 2px 1px;">'
        '<span style="font-size:13.5px;font-weight:600;color:#2C2C2A;">对话</span>'
        f'<span style="font-size:11px;color:#888780;margin-left:auto;">{provider}</span></div>'
    )
    # 点「新对话」按钮时改的是 chat_id，下拉框得跟上；必须在控件实例化之前改
    if st.session_state.get("chat_pick") != cid:
        st.session_state["chat_pick"] = cid
    st.selectbox(
        "话题",
        options,
        index=options.index(cid) if cid in options else 0,
        format_func=lambda x: labels.get(x, x),
        key="chat_pick",
        on_change=_on_pick,
        label_visibility="collapsed",
    )
    a, b = layout.cols(2)
    with a:
        if st.button("＋ 新对话", use_container_width=True, key="chat_new"):
            st.session_state["chat_id"] = db.create_chat()
            st.rerun()
    with b:
        if st.button("删除这个话题", use_container_width=True, key=f"chat_askdel_{cid}"):
            st.session_state["chat_confirm_del"] = cid

    if st.session_state.get("chat_confirm_del") == cid:
        st.warning("删掉这个话题和它的全部消息？工作台的会话、规则库和设置都不受影响。")
        x, y = layout.cols(2)
        with x:
            go = st.button("确认删除", type="primary", use_container_width=True,
                           key=f"chat_delyes_{cid}")
        with y:
            cancel = st.button("取消", use_container_width=True, key=f"chat_delno_{cid}")
        if cancel:
            st.session_state.pop("chat_confirm_del", None)
            st.rerun()
        if go:
            db.delete_chat(cid)
            rest = db.list_chats(1)
            st.session_state["chat_id"] = rest[0]["id"] if rest else db.create_chat()
            st.session_state.pop("chat_confirm_del", None)
            st.rerun()


def _on_pick() -> None:
    pick = st.session_state.get("chat_pick")
    if pick and db.get_chat(pick):
        st.session_state["chat_id"] = pick


def _empty_state() -> None:
    st.html(
        '<div style="padding:18px 2px 6px;">'
        '<div style="font-size:16px;color:#2C2C2A;font-weight:600;">想聊点什么？</div>'
        '<div style="font-size:12.5px;color:#5F5E5A;line-height:1.7;margin-top:6px;">'
        "这里没有流程，也不用先填表。想到什么就说什么；聊清楚了，再决定要不要正式委托给工作 Agent。"
        "</div></div>"
    )
    for label, q in QUICK:
        if st.button(label, key=f"quick_{label}", use_container_width=layout.narrow()):
            st.session_state["chat_prefill"] = q
            st.rerun()
