"""个人主页：把「长期资产」从工作视野里挪走。

规则库、模型设置、数据管理都是偶尔才动的东西，不该和「现在正在处理的这一件事」
挤在同一排导航里。工作台 / 对话 / 结果卡保持沉浸，这里单独开门。
"""
from __future__ import annotations

import streamlit as st

from core import db, llm
from ui import layout, page_rules, page_settings

INK = "#2C2C2A"
MUTED = "#5F5E5A"
BORDER = "#E6E4DE"


def render() -> None:
    if not layout.narrow():
        st.markdown("## 我的")
    _overview()

    tab = st.segmented_control(
        "分区",
        ["偏好规则", "模型与设置"],
        default="偏好规则",
        key="profile_tab",
        label_visibility="collapsed",
    )
    st.html(f'<div style="height:1px;background:{BORDER};margin:8px 0 12px;"></div>')

    if tab == "模型与设置":
        page_settings.render()
    else:
        page_rules.render()


def _overview() -> None:
    rules_n = len(db.list_rules(status="active"))
    sess_n = len(db.list_sessions(200))
    rev_n = len(db.list_reviews(200))
    chat_n = len(db.list_chats(200))
    model = "演示替身（不联网）" if llm.provider() == "mock" else "真实模型"

    st.html(
        '<div style="padding:10px 12px;border:1px solid ' + BORDER + ';'
        'border-radius:10px;background:#FBFAF7;margin:2px 0 12px;">'
        f'<div style="font-size:13px;color:{INK};font-weight:600;">你攒下的东西</div>'
        f'<div style="font-size:12px;color:{MUTED};line-height:1.9;margin-top:4px;">'
        f"启用规则 {rules_n} 条　·　工作台会话 {sess_n} 个　·　结果卡 {rev_n} 张<br>"
        f"自由对话 {chat_n} 个　·　当前模型 {model}</div>"
        f'<div style="font-size:11px;color:#888780;margin-top:6px;">'
        "这里放的是长期资产；清空工作台历史不会动规则库和设置。</div>"
        "</div>"
    )
