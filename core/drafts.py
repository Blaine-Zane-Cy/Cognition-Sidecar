"""草稿保管：让输入框里的字比页面活得久。

为什么不能只放 st.session_state：
Streamlit 在控件不再渲染时会删掉它的 key。实测切到别的页面再回来，
``st.session_state["draft_x"]`` 已经不存在，输入框回到空值。

为什么不能只靠 on_change：
text_area 的值在前端失焦时才提交上来。「打完最后一个字直接点侧栏导航」
这种情况下值可能还没提交就被卸载，所以要再加一道：
每次 rerun 开头把 session_state 里残留的输入框内容刷进库（sync_pending）。

注意：st.form 里的控件在点提交前**不会**把值发到后端，草稿捕获对 form 无效，
所以工作台的输入框已经改成非 form 控件。
"""
from __future__ import annotations

import streamlit as st

from . import db

PREFIX = "draft::"
RESET_FLAG = "_draft_reset::"


def widget_key(scope: str, key: str) -> str:
    return f"{PREFIX}{scope}::{key}"


def _split(wkey: str) -> tuple[str, str]:
    body = wkey[len(PREFIX) :]
    scope, _, key = body.partition("::")
    return scope, key


def prepare(scope: str, key: str) -> str:
    """首渲染时把库里的草稿塞进 widget key；之后不再传 value。"""
    wkey = widget_key(scope, key)
    if st.session_state.pop(RESET_FLAG + wkey, False):
        # 上一次提交成功，这一轮重新从库里取（通常是空）
        st.session_state[wkey] = db.load_draft(scope, key)
    elif wkey not in st.session_state:
        st.session_state[wkey] = db.load_draft(scope, key)
    return wkey


def save_now(scope: str, key: str) -> None:
    """on_change 回调：失焦即存。"""
    wkey = widget_key(scope, key)
    value = st.session_state.get(wkey, "")
    if isinstance(value, str):
        db.save_draft(scope, key, value)


def append(scope: str, key: str, text: str) -> str:
    """把外部内容并进草稿（上传的文件用），并让下一轮输入框显示合并结果。

    不能直接写 st.session_state[widget_key] —— 控件这一轮已经实例化过了，
    Streamlit 会抛 "cannot be modified after the widget is instantiated"。
    所以沿用 clear() 的 RESET_FLAG 机制：先落库 + 打标记，下一轮 prepare()
    在控件创建之前把值覆盖进去。返回合并后的文本，方便调用方判长度。
    """
    wkey = widget_key(scope, key)
    cur = st.session_state.get(wkey) or db.load_draft(scope, key) or ""
    cur = cur.rstrip() if isinstance(cur, str) else ""
    merged = (cur + "\n\n" + text.strip()).strip() if cur else text.strip()
    db.save_draft(scope, key, merged)
    st.session_state[RESET_FLAG + wkey] = True
    return merged


def clear(scope: str, key: str) -> None:
    """提交成功后调用：库里的和框里的都清掉，避免下次又把旧内容带出来。

    不能在这里直接改 session_state[wkey] —— 控件已经实例化过了，Streamlit 会报错。
    所以只留一个复位标记，由下一次 prepare() 在控件创建之前完成覆盖。
    """
    db.clear_draft(scope, key)
    st.session_state[RESET_FLAG + widget_key(scope, key)] = True


def clear_all(scope: str = "") -> None:
    """清空草稿（设置页用）。"""
    db.clear_drafts(scope or None)
    for wkey in list(st.session_state.keys()):
        if isinstance(wkey, str) and wkey.startswith(PREFIX):
            st.session_state[RESET_FLAG + wkey] = True


def sync_pending() -> int:
    """每次 rerun 开头跑。

    读的是「上一次 run 留在 session_state 里的控件值」——这一刻控件还没被卸载，
    是把「打完字直接切页」那道缝补上的唯一时机。
    """
    changed = 0
    for wkey in list(st.session_state.keys()):
        if not isinstance(wkey, str) or not wkey.startswith(PREFIX):
            continue
        if st.session_state.get(RESET_FLAG + wkey):
            # 刚提交成功、等着被 prepare() 复位：不要再把它写回去
            continue
        value = st.session_state.get(wkey)
        if not isinstance(value, str):
            continue
        scope, key = _split(wkey)
        if db.load_draft(scope, key) != value:
            db.save_draft(scope, key, value)
            changed += 1
    return changed
