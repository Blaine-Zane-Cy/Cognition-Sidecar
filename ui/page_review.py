"""结果卡页：回看历次评审，并把疑点对回原文。"""
from __future__ import annotations

import streamlit as st

from core import db
from core.models import REVIEW_STATUS_LABELS
from ui import components, layout


def _goto_chat(sid: str) -> None:
    """回看页 → 对话页的跳转。

    必须在 on_click 里改 session_state：按钮这时候已经渲染完了，
    在 render 期直接写 ["nav"] 会被 Streamlit 拦下来。
    """
    st.session_state["session_id"] = sid
    st.session_state["nav"] = "工作台"


def render() -> None:
    if not layout.narrow():  # 窄栏顶栏已有同名选项卡，标题只占高度
        st.markdown("## 结果卡")
    reviews = db.list_reviews(limit=120)
    if not reviews:
        st.info("还没有结果卡。去「工作台」走完一次委托，结果卡会出现在这里。")
        return

    labels = {
        r["id"]: f"{r['created_at'][5:16]} · {REVIEW_STATUS_LABELS.get(r['status'], r['status'])} · {r['headline'][:26]}"
        for r in reviews
    }
    rid = st.selectbox(
        "选择一次评审",
        list(labels),
        format_func=lambda x: labels[x],
        key="rev_pick",
    )
    rev = db.get_review(rid)
    if not rev:
        st.error("结果卡不见了。")
        return

    findings = db.list_findings(rid)
    usages = db.usages_for_review(rid)
    result = db.get_result(rev["result_id"])
    card = db.get_delegation(rev["delegation_id"])
    text = result["raw_output"] if result else ""

    # 必须用 on_click：回调在整页重跑之前执行，那时导航控件还没实例化；
    # 在按钮的 if 分支里直接写 session_state["nav"] 会触发
    # StreamlitWidgetAlreadyInstantiatedError。
    enter = dict(
        label="进入那次审阅对话",
        type="primary",
        use_container_width=True,
        on_click=_goto_chat,
        args=(rev["session_id"],),
    )
    if layout.narrow():
        st.button(**enter)
        st.caption("这里只回看。要追问、要排除某条疑点，进对话页说。")
    else:
        c1, c2 = st.columns([1, 3], vertical_alignment="center")
        c1.button(**enter)
        c2.caption("这里只回看。要追问、要排除某条疑点、要把决定打包给 Agent，进对话页说。")

    tab_card, tab_text = st.tabs(["结果卡", "原文对照"])

    with tab_card:
        # 回看页是历史视图：可以回溯，「以此新建会话」只在工作台给
        components.render_review_card(
            rev, findings, usages, card, text,
            allow_fork=False, key_prefix=f"rv_{rid}",
        )
        with st.expander("这张卡是怎么来的", expanded=False):
            st.markdown(
                f"- 生成模式：`{rev['provider']}`（mock = 演示替身）\n"
                f"- 评审时间：{rev['created_at']}\n"
                f"- 结果原文：{len(text)} 字\n"
                f"- 判定：{REVIEW_STATUS_LABELS.get(rev['status'], rev['status'])}"
                + (f"\n- 驳回理由：{rev['reject_reason']}" if rev["reject_reason"] else "")
            )
            if rev["skipped"]:
                st.markdown("跳过的槽位：")
                for s in rev["skipped"]:
                    st.markdown(f"- {s.get('slot')}：{s.get('reason')}")

    with tab_text:
        spans = [
            (f["char_start"], f["char_end"])
            for f in findings
            if f["char_start"] is not None
            and f["kind"] in ("doubt", "risk", "action_item", "decision")
        ]
        st.caption(
            f"高亮的是 {len(spans)} 处锚定的原文位置（疑点 + 待办/决策）。"
            "没有高亮的段落，就是模型没有给出处的地方。"
        )
        st.html(components.highlight_all(text, spans))
