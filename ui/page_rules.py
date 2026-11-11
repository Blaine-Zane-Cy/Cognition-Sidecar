"""规则库页：每条规则都可编辑、可归档、可删除，并看得到它被引用过几次。"""
from __future__ import annotations

import streamlit as st

from core import db, rules
from ui import components, layout


def render() -> None:
    if not layout.narrow():  # 窄栏顶栏已有同名选项卡，标题只占高度
        st.markdown("## 偏好规则库")
    st.caption(
        "规则是侧车学到的你的意志。每条都有来源：来自某次驳回，或你手写。"
        "评审时只会引用关键词命中的规则，命中记录在这里可查。"
    )

    all_rules = db.list_rules()
    active = [r for r in all_rules if r["status"] == "active"]
    archived = [r for r in all_rules if r["status"] == "archived"]

    if layout.narrow():
        # 三个 st.metric 在 400px 下会各占一行、还都顶着一张大数字，换成一句话
        total_hits = sum(db.usage_count(r["id"]) for r in all_rules)
        st.html(
            f'<div style="font-size:12px;color:{components.MUTED};">'
            f"启用 <b>{len(active)}</b> · 归档 <b>{len(archived)}</b> · 累计引用 <b>{total_hits}</b> 次</div>"
        )
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("启用中", len(active))
        c2.metric("已归档", len(archived))
        c3.metric("被引用总次数", sum(db.usage_count(r["id"]) for r in all_rules))

    with st.expander("＋ 手动新增一条规则", expanded=False):
        with st.form("new_rule"):
            text = st.text_area("规则（祈使句，一句说完）", height=68, placeholder="例如：交付前必须给出移动端真机截图")
            a, b = layout.cols(2)
            scope = a.selectbox("适用范围", ["both", "review", "delegate"], format_func=lambda x: rules.SCOPE_LABELS[x])
            keywords = b.text_input("关键词（顿号分隔）", placeholder="移动端、手机、截图")
            rationale = st.text_area("为什么有这条", height=60, placeholder="不写也行")
            ok = st.form_submit_button("入库")
        if ok:
            if len(text.strip()) < 4:
                st.error("规则太短。")
            else:
                rules.save_manual(text.strip(), rationale, scope, rules.parse_keywords(keywords))
                st.rerun()

    st.divider()

    if not all_rules:
        st.info("还没有规则。驳回一次结果，理由会自动变成规则；也可以在上面手动加。")
        return

    st.markdown("#### 启用中")
    for rule in active:
        components.rule_editor(rule, "rules")

    if archived:
        st.markdown("#### 已归档")
        st.caption("归档的规则不参与匹配，但保留引用记录。")
        for rule in archived:
            components.rule_editor(rule, "rules_arch")
