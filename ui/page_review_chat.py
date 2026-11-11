"""审阅对话界面：侧车像对面坐着的人，先开口，再跟你聊。

首屏只放三样：一行数字、几段人话、几颗胶囊。
完整结果卡没有消失，它被收进「展开完整结果卡」——对话负责让你看懂，卡片负责让你核对。
"""
from __future__ import annotations

import html

import streamlit as st

from core import branches, db, drafts, flow, review_chat
from core.models import (
    CERTAINTY_LABELS,
    DEVIATION_LABELS,
    FINDING_KIND_LABELS,
    REVIEW_STATUS_LABELS,
)
from ui import components, layout

INK = components.INK
MUTED = components.MUTED
BORDER = components.BORDER

CERT_STYLE = {
    "high": ("#0F6E56", "#E1F5EE"),
    "medium": ("#854F0B", "#FAEEDA"),
    "low": ("#A32D2D", "#FCEBEB"),
}
DEV_STYLE = {
    "none": ("#0F6E56", "#E1F5EE"),
    "minor": ("#185FA5", "#E6F1FB"),
    "major": ("#854F0B", "#FAEEDA"),
    "off_track": ("#A32D2D", "#FCEBEB"),
}
BRANCH_STATE_STYLE = {
    "open": ("未合并", "#185FA5"),
    "merged": ("已合并", "#0F6E56"),
    "dropped": ("已放弃", "#8B8B8B"),
}


def _bkey(rev: dict) -> str:
    """当前停在哪一支。空 = 主干。不进 URL：embed 参数已经被 WebView2 占着。"""
    return f"branch::{rev['id']}"


def render(sid: str) -> None:
    rev = db.latest_review(sid)
    if not rev:
        st.warning("这段对话还没有对应的评审。请先贴回一次结果。")
        return

    st.html(
        "<style>"
        'div[data-testid="stChatMessage"] p{margin-bottom:.35rem;}'
        'div[data-testid="stChatMessage"] .stMarkdown{margin-bottom:0;}'
        "</style>"
    )

    bid = st.session_state.get(_bkey(rev))
    branch = db.get_branch(bid) if bid else None
    if branch and branch["review_id"] == rev["id"]:
        _branch_view(rev, branch)
        return
    if bid:  # 这一支已经不在了（清过库/换过评审），别把用户卡在空白页
        st.session_state.pop(_bkey(rev), None)

    _header(rev)
    cands = review_chat.candidates(rev["id"])
    nos = {f["id"]: f.get("no") for f in cands["doubts_all"]}
    readonly = rev["status"] != "pending"
    _transcript(rev, nos, readonly)
    _branches_section(rev)

    if rev["status"] == "pending":
        _capsules(rev)
        _composer(rev)
    else:
        _closed_actions(rev)

    _card(rev)


# ---------------------------------------------------------------- 头部


def _header(rev: dict) -> None:
    status = REVIEW_STATUS_LABELS.get(rev["status"], rev["status"])
    tone = {"pending": ("#185FA5", "#E6F1FB"), "accepted": ("#0F6E56", "#E1F5EE")}.get(
        rev["status"], ("#854F0B", "#FAEEDA")
    )
    # 窄栏：评审 ID 有十六位，会把状态徽标挤到下一行；时间 + 状态已经够定位
    mid = rev["created_at"][5:16] if layout.narrow() else f'{rev["id"]} · {rev["created_at"][5:16]}'
    st.html(
        f'<div style="display:flex;align-items:center;gap:10px;padding-bottom:8px;'
        f'border-bottom:1px solid {BORDER};margin-bottom:14px;">'
        f'<div style="font-size:15px;font-weight:500;color:{INK};">审阅对话</div>'
        f'<div style="font-size:12px;color:{MUTED};">{mid}</div>'
        f'<div style="margin-left:auto;">{components.badge(status, tone[0], tone[1])}</div></div>'
    )


# ---------------------------------------------------------------- 对话


def _transcript(rev: dict, nos: dict, readonly: bool = False) -> None:
    rows = db.list_review_messages(rev["id"])
    if not rows:
        st.info("这段评审还没有开场白。点下面的「让它重新开口」补一段。")
        if st.button("让它重新开口", key=f"reopen_{rev['id']}"):
            review_chat.ensure_opening(rev["id"])
            st.rerun()
        return

    for msg in rows:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
            continue
        if msg["kind"] == "failure":
            st.warning(msg["content"])
            continue
        if msg["kind"] == "note":
            # 结案留言（"这次收了" / "理由已入库"）没有 refs，走气泡会渲染成一个空白方块
            st.html(
                f'<div style="font-size:12px;color:{MUTED};margin:2px 0 8px;">'
                f"{html.escape(msg['content'])}</div>"
            )
            continue
        with st.chat_message("assistant"):
            if msg["kind"] == "opening":
                _numbers(rev)
            _paragraphs(rev, msg, nos, readonly)


def _numbers(rev: dict) -> None:
    cert_fg, cert_bg = CERT_STYLE.get(rev["certainty"], (MUTED, "#F1EFE8"))
    dev_fg, dev_bg = DEV_STYLE.get(rev["deviation"], (MUTED, "#F1EFE8"))
    comp = "无法核定" if rev["completion_pct"] is None else f"{rev['completion_pct']}%"
    cells = [
        ("完成度", comp, INK, "#F7F6F2"),
        ("确定性", CERTAINTY_LABELS.get(rev["certainty"], rev["certainty"]), cert_fg, cert_bg),
        ("偏差", DEVIATION_LABELS.get(rev["deviation"], rev["deviation"]), dev_fg, dev_bg),
    ]
    body = "".join(
        f'<span style="display:inline-block;background:{bg};color:{fg};border-radius:8px;'
        f'padding:2px 10px;font-size:12px;margin-right:6px;">'
        f"{label} <b>{html.escape(value)}</b></span>"
        for label, value, fg, bg in cells
    )
    if rev["low_confidence"]:
        body += components.badge("低置信", "#A32D2D", "#FCEBEB")

    if layout.narrow():
        # 窄栏：三颗标签已经占满一行，解释按钮只能整行放，但压成一颗矮按钮
        st.html(f'<div style="margin-bottom:2px;">{body}</div>')
        with st.popover("为什么这个判定", use_container_width=True):
            _certainty_body(rev)
        return

    c1, c2 = st.columns([0.78, 0.22], vertical_alignment="center")
    with c1:
        st.html(f'<div style="margin-bottom:2px;">{body}</div>')
    with c2:
        with st.popover("为什么这个判定", use_container_width=True):
            _certainty_body(rev)


def _certainty_body(rev: dict) -> None:
    st.markdown(f"**确定性：{CERTAINTY_LABELS.get(rev['certainty'])}**")
    if rev["certainty_note"]:
        for part in rev["certainty_note"].split("；"):
            if part:
                st.markdown(f"- {part}")
    else:
        st.caption("这次没有触发任何降级条件。")
    st.caption("完成度 = 成功标准逐条判定（达标 1 / 部分 0.5 / 未达标 0）加权。")


def _paragraphs(rev: dict, msg: dict, nos: dict, readonly: bool = False) -> None:
    """每段人话下面挂一颗「依据」。这就是"像真人说话"和"每句可核对"的交汇点。"""
    for para in msg["refs"] or []:
        ref = str(para.get("ref") or "")
        text = str(para.get("text") or "")
        if not text:
            continue
        kind, _, raw_id = ref.partition(":")
        if kind == "suggest":
            _suggest_row(rev, text)   # 只渲染一次：不要再走下面的 st.markdown
            continue
        st.markdown(text)

        if kind in ("doubt", "risk", "decision", "action_item") and raw_id.isdigit():
            _evidence_row(rev, int(raw_id), kind, nos.get(int(raw_id)), readonly)
        elif kind == "verdict" and msg["kind"] == "opening":
            pass


def _evidence_row(rev: dict, finding_id: int, kind: str, no, readonly: bool = False) -> None:
    """一个疑点下面的那行：谁、依据在哪、我允不允许。

    窄栏里三列会被 Streamlit 竖着叠成三行（每行还占满宽度），所以换成两颗并排的按钮：
    左边那颗把编号和「依据」焊在一起（`#1 疑点 · 依据`），右边那颗是动作。
    """
    f = db.get_finding(finding_id)
    if not f:
        return
    label = FINDING_KIND_LABELS.get(kind, kind)
    if no:
        label = f"#{no} {label}"

    can_allow = kind in ("doubt", "risk") and f["state"] != "dismissed" and not readonly

    if layout.narrow():
        c1, c2 = st.columns([1.1, 1], gap="small")
        with c1:
            with st.popover(f"{label} · 依据", use_container_width=True):
                _evidence_body(rev, f)
        with c2:
            if can_allow:
                if st.button("这条我允许", key=f"allow_{rev['id']}_{finding_id}",
                             use_container_width=True):
                    review_chat.quick_dismiss(rev["id"], finding_id)
                    st.rerun()
            else:
                _state_hint(f, kind)
        return

    cols = st.columns([0.12, 0.2, 0.68], vertical_alignment="center")
    with cols[0]:
        st.html(f'<span style="font-size:11px;color:{MUTED};">{label}</span>')
    with cols[1]:
        with st.popover("依据", use_container_width=True):
            _evidence_body(rev, f)
    if can_allow:
        if cols[2].button("这条我允许", key=f"allow_{rev['id']}_{finding_id}"):
            review_chat.quick_dismiss(rev["id"], finding_id)
            st.rerun()
    else:
        with cols[2]:
            _state_hint(f, kind)


def _state_hint(f: dict, kind: str) -> None:
    """不能操作时，位置上要有一句话说清为什么。"""
    if f["state"] == "dismissed":
        text = f'已排除：{html.escape(f["chosen"] or "你允许了")}'
    elif kind == "decision" and f["state"] == "decided":
        text = f'已定：{html.escape(f["chosen"] or "")}'
    else:
        text = ""
    if text:
        st.html(f'<span style="font-size:11px;color:{MUTED};">{text}</span>')


def _evidence_body(rev: dict, f: dict) -> None:
    if f["detail"]:
        st.markdown(f["detail"])
    result = db.get_result(rev["result_id"])
    text = result["raw_output"] if result else ""
    if f["quote"] and f["char_start"] is not None:
        where = f"Agent 输出第 {f['char_start']}–{f['char_end']} 字"
        where += "（近似匹配）" if f["match_type"] == "fuzzy" else "（逐字命中）"
        st.caption(f"追溯依据：{where}")
        st.html(components.mark_window(text, f["char_start"], f["char_end"], ctx=140))
    elif f["quote"]:
        st.caption("模型声称的原文片段，但没能定位：")
        st.code(f["quote"])
    elif f["rule_id"]:
        st.caption("这条没有原文片段，依据是你自己的规则：")
    if f["rule_id"]:
        rule = db.get_rule(f["rule_id"])
        if rule:
            st.markdown(f"**{rule['id']}** {rule['text']}")


def _suggest_row(rev: dict, text: str) -> None:
    st.html(
        f'<div style="font-size:12px;color:{MUTED};border-left:2px solid #D3D1C7;'
        f'padding-left:8px;margin:-2px 0 6px;">建议：{html.escape(text)}</div>'
    )


# ---------------------------------------------------------------- 胶囊


def _capsules(rev: dict) -> None:
    acts = review_chat.build_actions(rev["id"])
    if not acts:
        return
    st.html(
        f'<div style="font-size:11px;color:{MUTED};margin:6px 0 2px;">下一步（点一个，或者直接在下面说）</div>'
    )
    if layout.narrow():
        # 窄栏：一排四颗会被 Streamlit 竖着叠成四行（占掉 160px），改成两行两列
        for i in range(0, len(acts[:4]), 2):
            chunk = acts[i : i + 2]
            c = st.columns(len(chunk), gap="small")
            for j, act in enumerate(chunk):
                if c[j].button(act["label"], key=f"cap_{rev['id']}_{act['action']}",
                               use_container_width=True, help=act.get("hint")):
                    _run_action(rev, act["action"])
        return

    cols = st.columns(min(len(acts), 4))
    for i, act in enumerate(acts[:4]):
        if cols[i].button(act["label"], key=f"cap_{rev['id']}_{act['action']}",
                          use_container_width=True, help=act.get("hint")):
            _run_action(rev, act["action"])


def _run_action(rev: dict, action: str) -> None:
    rid = rev["id"]
    if action == "accept":
        flow.accept(rid)
        db.add_review_message(rid, rev["session_id"], "结案：这次收了。", kind="note")
        st.rerun()
    if action == "reject":
        reason = st.session_state.get(f"draft::workbench::reply:{rid}", "").strip()
        if not reason:
            st.warning("驳回要有理由——先在下面输入框写一句，再点一次。理由会变成规则。")
            return
        ok, _ = components.call(flow.reject, rid, reason, spinner="正在把理由转成规则…")
        if ok:
            db.add_review_message(rid, rev["session_id"], f"结案：驳回。理由已入库：{reason}", kind="note")
            drafts.clear("workbench", f"reply:{rid}")
            st.rerun()
    if action == "followup":
        st.session_state[f"followup_{rid}"] = not st.session_state.get(f"followup_{rid}", False)
        st.rerun()
    if action == "fork":
        st.session_state[f"forkpick_{rid}"] = not st.session_state.get(f"forkpick_{rid}", False)
        st.rerun()
    if action == "card":
        st.session_state[f"card_{rid}"] = not st.session_state.get(f"card_{rid}", False)
        st.rerun()
    if action == "next_round":
        flow.to_awaiting(rev["session_id"])
        st.rerun()


def _extras(rev: dict) -> None:
    rid = rev["id"]
    if st.session_state.get(f"followup_{rid}"):
        with st.container(border=True):
            st.markdown("##### 补充说明（直接复制给 Agent）")
            text = review_chat.build_followup(rid)
            st.code(text, language=None)
            st.download_button(
                "存成 .txt",
                data=text,
                file_name=f"补充说明-{rid}.txt",
                mime="text/plain",
                key=f"dl_{rid}",
            )
            st.caption("只包含你在这段对话里定下的事：排除的、已定的、还欠的。")

    if st.session_state.get(f"forkpick_{rid}"):
        cands = review_chat.candidates(rid)
        with st.container(border=True):
            st.markdown("##### 挑一条单独去做")
            if not cands["actions"]:
                st.caption("没有待办可以单独立项。")
            for f in cands["actions"]:
                c1, c2 = st.columns([0.75, 0.25], vertical_alignment="center")
                c1.markdown(f"**{f['title']}**")
                if c2.button("开新会话", key=f"fk_{rid}_{f['id']}", use_container_width=True):
                    new_sid = flow.fork_from_finding(f["id"])
                    st.session_state["session_id"] = new_sid
                    st.rerun()


# ---------------------------------------------------------------- 输入


def _composer(rev: dict) -> None:
    rid = rev["id"]
    _extras(rev)
    st.html('<div style="height:6px;"></div>')
    narrow = layout.narrow()
    text = components.draft_text_area(
        "对审阅专家说",
        "workbench",
        f"reply:{rid}",
        height=96 if narrow else 82,
        placeholder="例如：第二个疑点不用管，那个是我允许的；决策那条选「先修移动端排版」",
        label_visibility="collapsed",
    )
    hint = (
        f'<span style="font-size:11px;color:{MUTED};">'
        "你说的每句话都会落库；带「这条我允许」的动作会顺手变成规则。</span>"
    )
    if narrow:
        # 窄栏：提示语和被撑窄的按钮抢同一行会互相挤，提示上、主按钮整行
        st.html(hint)
        if st.button("发送", type="primary", use_container_width=True, key=f"send_{rid}"):
            _send(rid, text)
        return

    c1, c2 = st.columns([0.78, 0.22], vertical_alignment="center")
    with c1:
        st.html(hint)
    if c2.button("发送", type="primary", use_container_width=True, key=f"send_{rid}"):
        _send(rid, text)


def _send(rid: str, text: str) -> None:
    if not text.strip():
        st.warning("写点东西再发。")
        return
    ok, res = components.call(review_chat.respond, rid, text.strip(), spinner="正在想…")
    if ok:
        drafts.clear("workbench", f"reply:{rid}")
        st.rerun()


# ---------------------------------------------------------------- 结案后


def _closed_actions(rev: dict) -> None:
    _extras(rev)
    st.divider()
    if rev["status"] == "rejected":
        st.success("已驳回。理由已经变成规则，下一次审批会先按它看。")
        new_rules = [r for r in db.list_rules() if r["source_review_id"] == rev["id"]]
        for rule in new_rules:
            st.markdown(f"**新规则 {rule['id']}**：{rule['text']}")
            components.rule_editor(rule, f"chat_{rev['id']}", compact=True)
    elif rev["status"] == "accepted":
        st.success("这次收了。")

    if layout.narrow():
        # 窄栏：主行动（贴下一轮）整行，两个次要动作并排
        if st.button("贴下一轮结果", type="primary", use_container_width=True, key=f"next_{rev['id']}"):
            flow.to_awaiting(rev["session_id"])
            st.rerun()
        c1, c2 = st.columns(2, gap="small")
        if c1.button("回到澄清改委托", use_container_width=True, key=f"back_{rev['id']}"):
            flow.to_clarify(rev["session_id"])
            st.rerun()
        if c2.button("看完整结果卡", use_container_width=True, key=f"opencard_{rev['id']}"):
            st.session_state[f"card_{rev['id']}"] = not st.session_state.get(f"card_{rev['id']}", False)
            st.rerun()
        return

    c1, c2, c3 = st.columns(3)
    if c1.button("贴下一轮结果", type="primary", use_container_width=True, key=f"next_{rev['id']}"):
        flow.to_awaiting(rev["session_id"])
        st.rerun()
    if c2.button("回到澄清改委托", use_container_width=True, key=f"back_{rev['id']}"):
        flow.to_clarify(rev["session_id"])
        st.rerun()
    if c3.button("看完整结果卡", use_container_width=True, key=f"opencard_{rev['id']}"):
        st.session_state[f"card_{rev['id']}"] = not st.session_state.get(f"card_{rev['id']}", False)
        st.rerun()


# ---------------------------------------------------------------- 完整卡片


def _card(rev: dict) -> None:
    rid = rev["id"]
    expanded = bool(st.session_state.get(f"card_{rid}"))
    label = "展开完整结果卡（标尺 · 条目 · 规则）" if layout.narrow() else \
        "展开完整结果卡（四格标尺 · 全部条目 · 规则引用）"
    with st.expander(label, expanded=expanded):
        findings = db.list_findings(rid)
        usages = db.usages_for_review(rid)
        result = db.get_result(rev["result_id"])
        card = db.get_delegation(rev["delegation_id"])
        components.render_review_card(
            rev,
            findings,
            usages,
            card,
            result["raw_output"] if result else "",
            allow_fork=False,
            key_prefix=f"chat_{rid}",
        )
        st.divider()
        st.caption("整段对话（含各分支，纯文本，可复制）：")
        st.code(review_chat.transcript_text(rid, with_branches=True), language=None)


# ---------------------------------------------------------------- 分支入口
#
# 主干负责"看懂全局"，分支负责"想清楚这一条"。所有分支都在库里，
# 页面只是换一屏 —— 所以「← 主干」永远不会丢东西。


def _branches_section(rev: dict) -> None:
    rows = branches.numbered(rev["id"])
    prog = db.branch_progress(rev["id"])

    if rows:
        done = prog["merged"] + sum(
            1 for b in rows if b["state"] == "dropped"
        )
        st.html(
            f'<div style="font-size:12px;color:{INK};margin:14px 0 2px;">'
            f"可以单独处理的 {prog['total']} 件事</div>"
            f'<div style="font-size:11px;color:{MUTED};margin-bottom:6px;">'
            f"点开一支只聊这一条，主干不动。已结 {done} 件</div>"
        )
        for i, b in enumerate(rows, 1):
            _branch_card(rev, b, i)

    label = "＋ 自己开一支讨论" if not rows else "＋ 再开一支（没有对应条目也能聊）"
    with st.popover(label, use_container_width=layout.narrow()):
        st.caption("没有对应待办、但你想就整体方向再问一嘴时用这个。合并时不会改任何条目。")
        title = st.text_input("这一支要问什么", key=f"mtitle_{rev['id']}",
                              placeholder="比如：这个页面到底给谁用")
        if st.button("开这一支", type="primary", use_container_width=True,
                     key=f"mopen_{rev['id']}"):
            bid = branches.manual(rev["id"], title)
            st.session_state[_bkey(rev)] = bid
            st.rerun()


def _branch_card(rev: dict, b: dict, idx: int) -> None:
    fg, _ = BRANCH_STATE_STYLE.get(b["state"], ("", MUTED))
    kind = FINDING_KIND_LABELS.get(b["kind"], "自由讨论")
    meta = [kind]
    meta.append(f'{b["msg_count"]} 句' if b["msg_count"] else "未开")
    if b["state"] != "open":
        meta.append(BRANCH_STATE_STYLE[b["state"]][0])
    title = b["title"] if len(b["title"]) <= 24 else b["title"][:23] + "…"

    # 一行摘要：让用户在点开之前就能分辨这几张卡（否则五张卡长得一样）
    f = db.get_finding(b["finding_id"]) if b.get("finding_id") else None
    brief = ((f or {}).get("detail") or b.get("question") or "").strip()
    brief = brief.replace("\n", " ")
    if len(brief) > 30:
        brief = brief[:29] + "…"

    with st.container(border=True):
        st.html(
            f'<div style="font-size:13px;color:{INK};font-weight:500;line-height:1.3;">'
            f'<span style="color:{MUTED};">#{idx}</span> {html.escape(title)}</div>'
            f'<div style="font-size:11px;color:{fg if b["state"] != "open" else MUTED};'
            f'margin-top:2px;">{" · ".join(meta)}</div>'
            + (
                f'<div style="font-size:11px;color:{MUTED};margin-top:1px;">'
                f"{html.escape(brief)}</div>"
                if brief
                else ""
            )
        )
        can_chat = b["state"] == "open" and rev["status"] == "pending"
        label = "聊这一条" if can_chat else ("看结论" if b["state"] == "merged" else "看记录")
        if st.button(label, key=f"open_{b['id']}", use_container_width=True):
            st.session_state[_bkey(rev)] = b["id"]
            st.rerun()


# ---------------------------------------------------------------- 分支视图


def _branch_view(rev: dict, b: dict) -> None:
    rows = db.list_branches(rev["id"])
    idx = next((i for i, x in enumerate(rows, 1) if x["id"] == b["id"]), 0)
    _route_bar(rev, b, idx, len(rows))

    kind = FINDING_KIND_LABELS.get(b["kind"], "自由讨论")
    state = BRANCH_STATE_STYLE.get(b["state"], ("", MUTED))[0]
    st.html(
        f'<div style="font-size:14px;color:{INK};font-weight:500;line-height:1.35;margin:0 0 2px;">'
        f"{html.escape(b['title'])}</div>"
        f'<div style="font-size:11px;color:{MUTED};margin:0 0 6px;">'
        f"只讨论这一条 · {kind} · {state}</div>"
    )
    if b["question"] and b["question"] != b["title"]:
        st.html(
            f'<div style="font-size:13px;color:{INK};line-height:1.45;margin-bottom:8px;">'
            f"{html.escape(b['question'])}</div>"
        )

    _ctx_expanders(rev, b)
    st.html(f'<div style="height:1px;background:{BORDER};margin:10px 0;"></div>')
    _branch_msgs(b)
    _receipt_panel(rev, b)

    if b["state"] == "open" and rev["status"] == "pending":
        _branch_composer(rev, b)
        _merge_panel(rev, b)
    else:
        if b["resolution"]:
            st.success(f"结论：{b['resolution']}")
        st.caption("这一支已经归档，只读。结论已经在主干的合并记录里。")


def _route_bar(rev: dict, b: dict, idx: int, total: int) -> None:
    """常驻路由条：从主干进分支，位置和高度不变，只有文字变。"""
    c1, c2 = st.columns([0.4, 0.6], vertical_alignment="center")
    if c1.button("← 主干", key=f"back_{b['id']}", use_container_width=True):
        st.session_state.pop(_bkey(rev), None)
        st.rerun()
    c2.html(
        f'<div style="text-align:right;font-size:12px;color:{MUTED};">'
        f"分支 {idx} / {total}</div>"
    )


def _ctx_expanders(rev: dict, b: dict) -> None:
    card = db.get_delegation(rev["delegation_id"]) or {}
    with st.expander("目标与边界（委托卡）", expanded=False):
        if card.get("goal"):
            st.markdown(f"**目标**　{card['goal']}")
        if card.get("deliverable"):
            st.markdown(f"**交付物**　{card['deliverable']}")
        for i, c in enumerate(card.get("success_criteria") or [], 1):
            st.markdown(f"{i}. {c.get('text', '')}")
        if card.get("forbidden"):
            st.caption(f"禁止：{card['forbidden']}")

    f = db.get_finding(b["finding_id"]) if b.get("finding_id") else None
    if f and (f["quote"] or f["detail"]):
        with st.expander("原文依据", expanded=False):
            _evidence_body(rev, f)


def _branch_msgs(b: dict) -> None:
    rows = db.list_branch_messages(b["id"])
    if not rows:
        st.info("这一支还没开始聊。说一句就行，比如「安卓上也试过吗」。")
        return
    for msg in rows:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
            continue
        if msg["kind"] == "failure":
            st.warning(msg["content"])
            continue
        with st.chat_message("assistant"):
            for para in msg["refs"] or [{"ref": "say", "text": msg["content"]}]:
                if para.get("text"):
                    st.markdown(para["text"])


def _extra(b: dict) -> list[dict]:
    return st.session_state.get(f"extra_{b['id']}") or []


def _receipt_panel(rev: dict, b: dict) -> None:
    ctx = branches.context_for(b, rev, _extra(b))
    total = ctx["stats"]["total"]
    with st.expander(f"给模型看什么 · 约 {round(total / 1000, 1)}k 字", expanded=False):
        st.code(branches.receipt(ctx), language=None)
        st.caption(
            "按这一支现在的状态估算。锚点是你这一条对应的原文段落；"
            "骨架是全文每段首句，只用来告诉模型这件事在哪儿。"
        )
        q = st.text_input("在原文里找…", key=f"find_{b['id']}",
                          placeholder="比如：部署步骤")
        if q and st.button("找这几段", key=f"findgo_{b['id']}", use_container_width=True):
            st.session_state[f"hits_{b['id']}"] = branches.find_in_source(b, rev, q)

        for i, h in enumerate(st.session_state.get(f"hits_{b['id']}") or []):
            st.caption(f"第 {h['start']}–{h['end']} 字 · 命中「{'、'.join(h['terms'])}」")
            st.code(h["text"][:600], language=None)
            if st.button(f"把第 {i + 1} 段带进对话", key=f"take_{b['id']}_{i}",
                         use_container_width=True):
                picked = st.session_state.setdefault(f"extra_{b['id']}", [])
                if all(x["start"] != h["start"] for x in picked):
                    picked.append(h)
                st.rerun()

        if _extra(b):
            st.caption(f"已追加 {len(_extra(b))} 段，会随下一句话一起给模型看。")
            if st.button("清空追加", key=f"clr_{b['id']}", use_container_width=True):
                st.session_state[f"extra_{b['id']}"] = []
                st.rerun()


def _branch_composer(rev: dict, b: dict) -> None:
    narrow = layout.narrow()
    st.html('<div style="height:6px;"></div>')
    text = components.draft_text_area(
        "说说这一条怎么弄",
        "review_branch",
        b["id"],
        height=88 if narrow else 78,
        placeholder="例如：安卓上我试过两台都不复现，那就只按 iOS 兼容处理？",
        label_visibility="collapsed",
    )
    if narrow:
        if st.button("发送", type="primary", use_container_width=True, key=f"bsend_{b['id']}"):
            _branch_send(rev, b, text)
        return

    c1, c2 = st.columns([0.78, 0.22], vertical_alignment="center")
    c1.html(
        f'<span style="font-size:11px;color:{MUTED};">'
        "只影响这一支。聊完点下面「标记完成并合并」，才会写回主干。</span>"
    )
    if c2.button("发送", type="primary", use_container_width=True, key=f"bsend_{b['id']}"):
        _branch_send(rev, b, text)


def _branch_send(rev: dict, b: dict, text: str) -> None:
    if not text.strip():
        st.warning("写点东西再发。")
        return
    extra = _extra(b)
    ok, _ = components.call(
        branches.respond, b["id"], text.strip(), extra, spinner="正在想…"
    )
    if ok:
        drafts.clear("review_branch", b["id"])
        st.rerun()


def _merge_panel(rev: dict, b: dict) -> None:
    st.html('<div style="height:8px;"></div>')
    with st.expander("标记完成并合并到主干", expanded=False):
        st.caption(
            "主干只留一句结论。合并后这一支归档、不能再聊 —— 一件事只留一个真相源。"
        )
        res = st.text_area(
            "结论",
            value=b["resolution"] or "",
            key=f"res_{b['id']}",
            height=68,
            label_visibility="collapsed",
            placeholder="一句话写清这条怎么定的",
        )
        c1, c2 = st.columns(2, gap="small")
        if c1.button("合并到主干", type="primary", use_container_width=True,
                     key=f"merge_{b['id']}"):
            ok, _ = components.call(branches.merge, b["id"], res, spinner="合并中…")
            if ok:
                st.session_state.pop(_bkey(rev), None)
                st.session_state.pop(f"extra_{b['id']}", None)
                st.rerun()
        if c2.button("放弃这一支", use_container_width=True, key=f"drop_{b['id']}"):
            db.drop_branch(b["id"], res.strip() or "已放弃")
            st.session_state.pop(_bkey(rev), None)
            st.rerun()

        if b.get("finding_id"):
            st.caption("这一条要单独做成一件事（换到工作台、重新澄清）？")
            if st.button("开成独立会话", key=f"forkb_{b['id']}", use_container_width=True):
                sid_new = flow.fork_from_finding(b["finding_id"])
                st.session_state["session_id"] = sid_new
                st.rerun()
