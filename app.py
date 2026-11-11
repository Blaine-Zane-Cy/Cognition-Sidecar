"""认知掌控侧车 —— 本地原型入口。

    python -m streamlit run app.py
    宽屏：http://localhost:8501/
    窄栏：http://localhost:8501/?embed=true&narrow=1   （浏览器右侧 sidePanel 用这个）

窄栏模式不改任何业务：它只是换一层样式、换一个导航条。
`layout` 在这里最先接入，页面模块只负责排内容。
"""
from __future__ import annotations

import streamlit as st

from core import db, drafts, llm
from ui import layout, page_chat, page_profile, page_review, page_workbench

st.set_page_config(page_title="认知掌控侧车", layout="wide")
layout.inject_css()  # 窄栏的样式层：必须在任何内容之前

db.init_db()
drafts.sync_pending()  # 补「打完字直接切页」那道缝：必须在任何控件渲染之前跑
if "session_id" not in st.session_state:
    st.session_state["session_id"] = None

# 主导航只放「当下正在做的事」。规则库与设置收进「我的」，
# 工作时不再被长期配置打断；自由对话与流水线并列，不互相替代。
PAGES = {
    "工作台": page_workbench,
    "对话": page_chat,
    "结果卡": page_review,
    "我的": page_profile,
}


# ---------------------------------------------------------------- 导航


def _sidebar_nav() -> str:
    """宽屏：原来的侧边栏，一行不动。"""
    with st.sidebar:
        st.markdown("### 认知掌控侧车")
        st.caption("旁路读懂工作 Agent 的结果，不改造它")
        name = st.radio("导航", list(PAGES), key="nav", label_visibility="collapsed")
        st.divider()

        provider = llm.provider()
        active_rules = len(db.list_rules(status="active"))
        provider_text = "演示替身（不联网）" if provider == "mock" else "真实模型"
        st.html(
            f'<div style="font-size:12px;color:#5F5E5A;line-height:1.9;">'
            f"模型：{provider_text}<br>"
            f"数据库：{db.db_file().name}<br>"
            f"规则：{active_rules} 条启用</div>",
        )
        if provider == "mock":
            st.caption("在「设置」填 API Key 可切真实模型。")

        st.divider()
        st.html(
            '<div style="font-size:12px;color:#888780;line-height:1.8;">'
            "接受 / 驳回由你点，侧车只给依据和选项。"
            "</div>",
        )
    return name


def _narrow_nav() -> str:
    """窄栏：顶部一排选项卡 + 一行状态。没有侧边栏可躲，所以信息要一行说完。"""
    provider = llm.provider()
    active_rules = len(db.list_rules(status="active"))
    provider_text = "演示替身" if provider == "mock" else "真实模型"

    st.html(
        '<div style="display:flex;align-items:baseline;gap:8px;margin:0 0 2px 1px;">'
        '<span style="font-size:13.5px;font-weight:600;color:#2C2C2A;">认知掌控侧车</span>'
        f'<span style="font-size:11px;color:#888780;margin-left:auto;">'
        f"{provider_text} · 规则 {active_rules}</span></div>"
    )
    name = st.segmented_control(
        "导航",
        list(PAGES),
        default="工作台",
        key="nav",
        label_visibility="collapsed",
    )
    return name or "工作台"


def _attach_recent() -> None:
    """侧栏每次打开都是新会话，`session_id` 一定是空的。

    「接着上一次看」几乎总是本意，所以窄栏下默认挂到最近的会话；
    用户主动选「＋ 新建会话」时 `sess_pick` 会是那个值，这里就不抢。
    """
    if st.session_state.get("session_id"):
        return
    if st.session_state.get("sess_pick") == "__new__":
        return
    recent = db.list_sessions(1)
    if recent:
        st.session_state["session_id"] = recent[0]["id"]


NARROW = layout.narrow()
if NARROW:
    name = _narrow_nav()
    _attach_recent()
else:
    name = _sidebar_nav()

PAGES[name].render()
