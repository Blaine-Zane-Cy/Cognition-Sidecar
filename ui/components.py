"""纯渲染组件：只读 DB 数据，不写业务状态。"""
from __future__ import annotations

import html
import re

import streamlit as st

from core import db, drafts, flow, llm, rules
from core.models import (
    ANCHOR_LABELS,
    BRANCH_KINDS,
    BRANCH_MARKS,
    BRANCH_STATE_LABELS,
    CERTAINTY_LABELS,
    DEVIATION_LABELS,
)
from ui import layout

INK = "#2C2C2A"
MUTED = "#5F5E5A"
BORDER = "#E3E1DA"

BRANCH_FIRST_SCREEN = 2  # 首屏每类只露 2 条，其余折叠

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
VERDICT_STYLE = {
    "pass": ("#0F6E56", "#E1F5EE", "达标"),
    "partial": ("#854F0B", "#FAEEDA", "部分达标"),
    "fail": ("#A32D2D", "#FCEBEB", "未达标"),
}


def badge(text: str, fg: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'padding:2px 9px;border-radius:7px;font-size:12px;line-height:1.8;">{text}</span>'
    )


def metric_card(
    label: str,
    value: str,
    note: str,
    fg: str = INK,
    bg: str = "#F7F6F2",
    compact: bool = False,
) -> None:
    """一格标尺。compact 是窄栏用的：高度交给内容，字号小一号。"""
    pad = "8px 10px" if compact else "11px 13px"
    vsize = "17px" if compact else "21px"
    floor = "auto" if compact else "96px"
    nsize = "11px" if compact else "12px"
    st.html(
        f'<div style="border:1px solid {BORDER};border-radius:12px;padding:{pad};'
        f'background:{bg};min-height:{floor};">'
        f'<div style="font-size:12px;color:{MUTED};margin-bottom:2px;">{label}</div>'
        f'<div style="font-size:{vsize};font-weight:500;color:{fg};line-height:1.35;">{value}</div>'
        f'<div style="font-size:{nsize};color:{MUTED};margin-top:2px;">{note}</div>'
        f"</div>",
    )


def call(fn, *args, spinner: str | None = None, **kwargs):
    """统一处理模型异常，返回 (ok, value)。kwargs 透传给业务函数。"""
    try:
        if spinner:
            with st.spinner(spinner):
                return True, fn(*args, **kwargs)
        return True, fn(*args, **kwargs)
    except llm.LLMContractError as exc:
        st.error(f"模型返回的不是可用结构：{exc}")
        with st.expander("查看模型原始返回"):
            st.code(exc.raw or "（空）")
        return False, None
    except Exception as exc:  # noqa: BLE001 原型阶段统一兜住
        st.error(str(exc))
        return False, None


# ---------------------------------------------------------------- 草稿输入
# 注意：这些控件不能塞进 st.form —— form 里的值在点提交前不会发到后端，
# 草稿就捕不到「打完字直接切页」那种情况。


def draft_text_area(
    label: str,
    scope: str,
    key: str,
    *,
    height: int = 96,
    placeholder: str = "",
    label_visibility: str = "visible",
):
    wkey = drafts.prepare(scope, key)
    return st.text_area(
        label,
        key=wkey,
        height=height,
        placeholder=placeholder,
        label_visibility=label_visibility,
        on_change=drafts.save_now,
        args=(scope, key),
    )


def draft_text_input(
    label: str,
    scope: str,
    key: str,
    *,
    placeholder: str = "",
    label_visibility: str = "visible",
):
    wkey = drafts.prepare(scope, key)
    return st.text_input(
        label,
        key=wkey,
        placeholder=placeholder,
        label_visibility=label_visibility,
        on_change=drafts.save_now,
        args=(scope, key),
    )


# ---------------------------------------------------------------- 原文高亮


_MD_NOISE = [
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M), ""),
    (re.compile(r"\*\*|__|`"), ""),
]


def _strip_md(s: str) -> str:
    for pattern, repl in _MD_NOISE:
        s = pattern.sub(repl, s)
    return s


def _mark(text: str, start, end, ctx: int = 70) -> str:
    a = max(0, start - ctx)
    b = min(len(text), end + ctx)
    lead = "…" if a > 0 else ""
    tail = "…" if b < len(text) else ""
    return (
        f'<div style="font-size:13px;line-height:1.85;background:#F7F6F2;border:1px solid {BORDER};'
        f'border-radius:10px;padding:10px 12px;white-space:pre-wrap;">'
        f"{lead}{html.escape(_strip_md(text[a:start]))}"
        f'<mark style="background:#FAEEDA;color:#412402;border-bottom:2px solid #BA7517;">'
        f"{html.escape(_strip_md(text[start:end]))}</mark>"
        f"{html.escape(_strip_md(text[end:b]))}{tail}</div>"
    )


def highlight_all(text: str, spans: list[tuple[int, int]]) -> str:
    spans = sorted({(int(s), int(e)) for s, e in spans if s is not None and e is not None})
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out, last = [], 0
    for s, e in merged:
        out.append(html.escape(_strip_md(text[last:s])))
        out.append(
            '<mark style="background:#FAEEDA;color:#412402;border-bottom:2px solid #BA7517;">'
            + html.escape(_strip_md(text[s:e]))
            + "</mark>"
        )
        last = e
    out.append(html.escape(_strip_md(text[last:])))
    return (
        f'<div style="font-size:13px;line-height:1.9;background:#FFF;border:1px solid {BORDER};'
        f'border-radius:10px;padding:14px 16px;white-space:pre-wrap;">'
        + "".join(out)
        + "</div>"
    )


def mark_window(text: str, start, end, ctx: int = 70) -> str:
    """只露命中处前后一小段（对话气泡里的「依据」用这个，不能把全文塞进去）。"""
    return _mark(text, start, end, ctx=ctx)


# ---------------------------------------------------------------- 待办 / 决策分支


def _is_anchored(item: dict) -> bool:
    return item.get("match_type") in ("exact", "fuzzy") or bool(item.get("rule_id"))


def _anchor_badge(item: dict) -> tuple[str, str, str]:
    """(文字, 前景色, 背景色)。"""
    if item.get("rule_id") and item.get("match_type") not in ("exact", "fuzzy"):
        return ANCHOR_LABELS["rule"], "#185FA5", "#E6F1FB"
    if item.get("match_type") == "exact":
        return ANCHOR_LABELS["exact"], "#0F6E56", "#E1F5EE"
    if item.get("match_type") == "fuzzy":
        return ANCHOR_LABELS["fuzzy"], "#854F0B", "#FAEEDA"
    return ANCHOR_LABELS["unanchored"], "#A32D2D", "#FCEBEB"


def _start_fork(finding: dict) -> None:
    sid = flow.fork_from_finding(finding["id"])
    st.session_state["session_id"] = sid
    st.rerun()


def _branch_block(f: dict, agent_text: str, allow_fork: bool, key_prefix: str) -> None:
    text, fg, bg = _anchor_badge(f)
    mark = BRANCH_MARKS.get(f["kind"], "·")
    opened = bool(st.session_state.get(f"evidence_{f['id']}")) or f["state"] in (
        "revisited",
        "decided",
        "followed",
        "merged",
    )

    title = f"{mark} {html.escape(f['title'])}"
    sub = ""
    if f["kind"] == "decision" and f["recommended"]:
        sub = f"推荐：{html.escape(f['recommended'])}"
        if f["why"]:
            sub += f" · {html.escape(f['why'])}"
    elif f["state"] != "open":
        sub = BRANCH_STATE_LABELS.get(f["state"], f["state"])
        if f["chosen"]:
            sub += f"　已选：{html.escape(f['chosen'])}"

    with st.container(border=True):
        if layout.narrow():
            # 窄栏：标题和徽标同一块，动作降到下一行（2~3 颗并排，仍然一行放得下）
            st.html(
                f'<div style="font-size:13px;line-height:1.5;">{title}　{badge(text, fg, bg)}</div>'
                + (f'<div style="font-size:11px;color:{MUTED};margin-top:1px;">{sub}</div>' if sub else "")
            )
            acts = [("依据", f"{key_prefix}_ev_{f['id']}", "ev"),
                    ("回溯原文", f"{key_prefix}_rv_{f['id']}", "rv")]
            if allow_fork:
                acts.append(("开新会话", f"{key_prefix}_fk_{f['id']}", "fk"))
            c = st.columns(len(acts), gap="small")
            for i, (label, key, kind) in enumerate(acts):
                if c[i].button(label, key=key, use_container_width=True):
                    if kind == "ev":
                        st.session_state[f"evidence_{f['id']}"] = not opened
                    elif kind == "rv":
                        flow.revisit_branch(f["id"])
                        st.session_state[f"evidence_{f['id']}"] = True
                    else:
                        _start_fork(f)
                    st.rerun()
        else:
            widths = [0.46, 0.11, 0.13, 0.15, 0.15] if allow_fork else [0.58, 0.13, 0.14, 0.15]
            cols = st.columns(widths, vertical_alignment="center")
            with cols[0]:
                st.html(f'<div style="font-size:13px;line-height:1.5;">{title}</div>')
                if sub:
                    st.html(
                        f'<div style="font-size:11px;color:{MUTED};margin-top:1px;">{sub}</div>'
                    )
            with cols[1]:
                st.html(badge(text, fg, bg))
            if cols[2].button("依据", key=f"{key_prefix}_ev_{f['id']}", use_container_width=True):
                st.session_state[f"evidence_{f['id']}"] = not opened
                st.rerun()
            if cols[3].button("回溯原文", key=f"{key_prefix}_rv_{f['id']}", use_container_width=True):
                flow.revisit_branch(f["id"])
                st.session_state[f"evidence_{f['id']}"] = True
                st.rerun()
            if allow_fork and cols[4].button(
                "以此新建会话", key=f"{key_prefix}_fk_{f['id']}", use_container_width=True
            ):
                _start_fork(f)

        if not opened:
            return

        if f["detail"]:
            st.markdown(f["detail"])
        if f["quote"] and f["char_start"] is not None:
            detail = f"追溯依据：Agent 输出第 {f['char_start']}–{f['char_end']} 字"
            detail += "（近似匹配）" if f["match_type"] == "fuzzy" else "（逐字命中）"
            st.html(f'<div style="font-size:12px;color:{MUTED};margin-bottom:4px;">{detail}</div>')
            st.html(_mark(agent_text, f["char_start"], f["char_end"], ctx=120))
        elif f["quote"]:
            st.markdown("模型声称的原文片段（未能定位）：")
            st.code(f["quote"])
        if f["rule_id"]:
            rule = db.get_rule(f["rule_id"])
            if rule:
                st.markdown(f"依据规则：**{rule['id']}** {rule['text']}")

        if f["kind"] == "decision" and f["options"]:
            labels = [f"{o['label']}　代价：{o.get('cost') or '未说明'}" for o in f["options"]]
            pick = st.radio(
                "选一个",
                labels,
                index=None,
                key=f"{key_prefix}_opt_{f['id']}",
                label_visibility="collapsed",
            )
            if st.button("记下选择", key=f"{key_prefix}_ok_{f['id']}"):
                if not pick:
                    st.warning("先选一个再记。")
                else:
                    flow.decide_branch(f["id"], f["options"][labels.index(pick)]["label"])
                    st.session_state.pop(f"evidence_{f['id']}", None)
                    st.rerun()


def render_branches(
    review: dict,
    findings: list[dict],
    agent_text: str,
    *,
    allow_fork: bool = True,
    key_prefix: str = "wb",
) -> None:
    """首屏的「待处理分支」：待办 2 条 + 决策 2 条，其余折叠。"""
    items = [f for f in findings if f["kind"] in BRANCH_KINDS]
    st.markdown("##### 待处理分支")
    if not items:
        st.caption("这一轮没有待办，也没有需要你拍板的分叉。")
        return

    anchored = [f for f in items if _is_anchored(f)]
    sink = [f for f in items if not _is_anchored(f)]
    todo = [f for f in anchored if f["kind"] == "action_item"]
    dec = [f for f in anchored if f["kind"] == "decision"]
    total_todo = sum(1 for f in items if f["kind"] == "action_item")
    total_dec = sum(1 for f in items if f["kind"] == "decision")

    head = []
    if total_todo:
        head.append(f"待办 {total_todo}")
    if total_dec:
        head.append(f"决策 {total_dec}")
    st.html(
        f'<div style="font-size:12px;color:{MUTED};margin:-4px 0 8px;">'
        f"{' · '.join(head)}　首屏只露 2 + 2，其余折叠</div>"
    )

    shown = todo[:BRANCH_FIRST_SCREEN] + dec[:BRANCH_FIRST_SCREEN]
    more = todo[BRANCH_FIRST_SCREEN:] + dec[BRANCH_FIRST_SCREEN:]

    if not shown:
        st.caption("可追溯的待办/决策为 0 条，未锚定的收在下面。")

    for f in shown:
        _branch_block(f, agent_text, allow_fork, key_prefix)

    if more:
        with st.expander(f"还有 {len(more)} 条（折叠）", expanded=False):
            for f in more:
                _branch_block(f, agent_text, allow_fork, key_prefix)

    if sink:
        with st.expander(f"未锚定的待办/决策（{len(sink)} 条，不进首屏）", expanded=False):
            st.caption(
                "这些条目在 Agent 输出里找不到对应原文，也没有规则依据。当建议看，别当结论。"
            )
            for f in sink:
                st.markdown(f"**{BRANCH_MARKS.get(f['kind'], '·')} {f['title']}**")
                if f["detail"]:
                    st.caption(f["detail"])
                if f["quote"]:
                    st.caption(f"它声称的原文：{f['quote']}")
                if allow_fork and st.button(
                    "以此新建会话", key=f"{key_prefix}_sfk_{f['id']}"
                ):
                    _start_fork(f)


# ---------------------------------------------------------------- 结果卡


def render_summary(
    review: dict,
    findings: list[dict],
    agent_text: str,
    *,
    allow_fork: bool = True,
    key_prefix: str = "wb",
) -> None:
    """结果卡的上半部分：结论 + 标尺 + 待处理分支 + 疑点。决定所需的一切都在这里。"""
    subs = [f for f in findings if f["kind"] == "sub_goal"]
    doubts = [f for f in findings if f["kind"] == "doubt"]
    risks = [f for f in findings if f["kind"] == "risk"]
    anchored = [f for f in doubts + risks if f["match_type"] in ("exact", "fuzzy") or f["anchor_type"] == "rule"]
    unanchored = [f for f in doubts + risks if f not in anchored]

    # 1. 结论
    st.markdown(f"#### {review['headline']}")
    if review["low_confidence"]:
        st.html(
            badge("低置信结果卡", "#A32D2D", "#FCEBEB")
            + f' <span style="font-size:12px;color:{MUTED}">这张卡里有跳过的槽位或未锚定的疑点，按它做决定前先看说明</span>',
        )

    # 2. 四格标尺
    passed = sum(1 for f in subs if f["verdict"] == "pass")
    partial = sum(1 for f in subs if f["verdict"] == "partial")
    if review["completion_pct"] is None:
        comp_value, comp_note, comp_fg, comp_bg = "无法核定", "成功标准被跳过", "#854F0B", "#FAEEDA"
    else:
        comp_value = f"{review['completion_pct']}%"
        comp_note = f"{passed} 达标 / {partial} 部分 / {len(subs) - passed - partial} 未达标"
        fg_pair = CERT_STYLE["high"] if review["completion_pct"] >= 80 else (
            CERT_STYLE["medium"] if review["completion_pct"] >= 40 else CERT_STYLE["low"]
        )
        comp_fg, comp_bg = fg_pair

    cert_fg, cert_bg = CERT_STYLE.get(review["certainty"], ("#5F5E5A", "#F1EFE8"))
    dev_fg, dev_bg = DEV_STYLE.get(review["deviation"], ("#5F5E5A", "#F1EFE8"))

    note = f"{len(unanchored)} 条未锚定" if unanchored else "全部可追溯"
    dev_label = "偏差" if layout.narrow() else "与目标偏差"  # 窄栏一格只有 160px 宽，五个字会折行
    cells = [
        ("完成度", comp_value, comp_note, comp_fg, comp_bg),
        ("确定性", CERTAINTY_LABELS.get(review["certainty"], review["certainty"]), "结论有多少依据", cert_fg, cert_bg),
        (dev_label, DEVIATION_LABELS.get(review["deviation"], review["deviation"]), "跑偏程度", dev_fg, dev_bg),
        ("疑点", f"{len(anchored)} 条", note, "#A32D2D" if unanchored else INK, "#FCEBEB" if unanchored else "#F7F6F2"),
    ]

    if layout.narrow():
        # 窄栏：四格一行会被压成每格 90px（"完成度"三个字就撑满），改 2×2
        for k in (slice(0, 2), slice(2, 4)):
            row = st.columns(2, gap="small")
            for col, cell in zip(row, cells[k]):
                with col:
                    metric_card(*cell, compact=True)
    else:
        for col, cell in zip(st.columns(4), cells):
            with col:
                metric_card(*cell)

    if review["certainty_note"]:
        with st.expander(f"为什么确定性是「{CERTAINTY_LABELS.get(review['certainty'])}」", expanded=False):
            for part in review["certainty_note"].split("；"):
                if part:
                    st.markdown(f"- {part}")

    if review["deviation_note"]:
        st.html(
            f'<div style="font-size:13px;color:{MUTED};margin:2px 0 10px;">偏差说明：{html.escape(review["deviation_note"])}</div>',
        )

    # 3. 待处理分支（待办 / 决策）——在疑点之前，因为「下一步做什么」比「哪里不对」更靠前
    render_branches(
        review, findings, agent_text, allow_fork=allow_fork, key_prefix=key_prefix
    )

    # 4. 疑点（默认只露标题，点开看依据）
    st.markdown("##### 疑点与风险")
    if not anchored:
        st.caption("这一轮没有锚定成功的疑点。")
    for item in anchored:
        tag = "疑点" if item["kind"] == "doubt" else "风险"
        mark = {"exact": "逐字命中", "fuzzy": "近似匹配"}.get(item["match_type"], "引用规则")
        sev = {"high": "高", "medium": "中", "low": "低"}.get(item["severity"] or "", "")
        suffix = f"　（{mark}{'· 严重度' + sev if sev else ''}）"
        with st.expander(f"{tag} · {item['title']}{suffix}", expanded=False):
            if item["detail"]:
                st.markdown(item["detail"])
            if item["quote"] and item["char_start"] is not None:
                detail = f"追溯依据：Agent 输出第 {item['char_start']}–{item['char_end']} 字"
                if item["match_type"] == "fuzzy":
                    detail += f"（近似匹配，相似度 {item['match_score']}）"
                else:
                    detail += "（逐字命中）"
                st.html(
                    f'<div style="font-size:12px;color:{MUTED};margin-bottom:4px;">{detail}</div>',
                )
                st.html(_mark(agent_text, item["char_start"], item["char_end"]))
            elif item["quote"]:
                st.markdown("模型声称的原文片段（未能定位）：")
                st.code(item["quote"])
            if item["rule_id"]:
                rule = db.get_rule(item["rule_id"])
                if rule:
                    st.markdown(f"依据规则：**{rule['id']}** {rule['text']}")

    if unanchored:
        with st.expander(f"未锚定的条目（{len(unanchored)} 条，已移出疑点区）", expanded=False):
            st.caption("这些结论在 Agent 输出里找不到对应原文，也没有规则依据。不作为你驳回的理由，只作参考。")
            for f in unanchored:
                st.markdown(f"**{f['title']}**")
                if f["quote"]:
                    st.markdown(f"它声称的原文：`{f['quote']}`")
                if f["detail"]:
                    st.caption(f["detail"])


def render_detail(
    review: dict,
    findings: list[dict],
    usages: list[dict],
    delegation: dict | None,
) -> None:
    """结果卡的下半部分：规则依据 + 下一步选项 + 完成度算法。"""
    subs = [f for f in findings if f["kind"] == "sub_goal"]

    # 4. 规则引用
    st.markdown("##### 按哪条规则判的")
    if usages:
        for u in usages:
            verdict = {"satisfied": "满足", "violated": "违反", "cited": "引用"}.get(u["verdict"], u["verdict"])
            with st.expander(f"{u['rule_id']} · {verdict} · {(u['rule_text'] or '')[:26]}", expanded=False):
                st.markdown(f"规则原文：{u['rule_text']}")
                if u["reason"]:
                    st.markdown(f"判断理由：{u['reason']}")
                st.caption("可在「规则库」页编辑或归档这条规则。")
    else:
        st.caption("这次没有命中任何偏好规则（规则库为空，或关键词没对上）。")

    # 5. 下一步选项
    st.markdown("##### 下一步选项")
    if review["next_options"]:
        for i, opt in enumerate(review["next_options"], 1):
            st.html(
                f'<div style="border-left:3px solid #D3D1C7;padding:2px 0 2px 10px;margin-bottom:8px;">'
                f'<div style="font-size:14px;color:{INK};">{i}. {html.escape(opt["label"])}</div>'
                f'<div style="font-size:12px;color:{MUTED};">代价：{html.escape(opt.get("cost") or "未说明")}</div>'
                f"</div>",
            )
    else:
        st.caption("模型没有给选项。")

    # 6. 成功标准逐条（完成度是怎么算的）
    if subs:
        with st.expander(f"成功标准逐条（完成度 = 上面这 {len(subs)} 条的加权）", expanded=False):
            for i, f in enumerate(subs, 1):
                fg, bg, text = VERDICT_STYLE.get(f["verdict"], ("#5F5E5A", "#F1EFE8", f["verdict"]))
                st.html(
                    f'{badge(text, fg, bg)}　**{i}. {f["title"]}**',
                )
                if f["detail"]:
                    st.caption(f"依据：{f['detail']}")
                if f["quote"] and f["char_start"] is not None:
                    st.caption(f"原文：{f['quote']}")
                st.markdown("")

    if delegation and delegation.get("success_criteria"):
        with st.expander("对照委托卡", expanded=False):
            for i, c in enumerate(delegation["success_criteria"], 1):
                st.markdown(f"**{i}. {c['text']}**")
                st.caption(f"验证方式：{c.get('how_to_verify') or '（未给）'}")


# ---------------------------------------------------------------- 规则编辑


def rule_editor(rule: dict, key_prefix: str, compact: bool = False) -> None:
    hits = db.usage_count(rule["id"])
    src = {"reject": "来自驳回", "manual": "手动添加", "chat": "对话提炼"}.get(rule["source"], rule["source"])
    with st.expander(
        f"{rule['id']} · {rule['text'][:34]}{'…' if len(rule['text']) > 34 else ''}"
        f"　（{src}｜命中 {hits} 次｜{rule['status']}）",
        expanded=False,
    ):
        with st.form(f"{key_prefix}_rule_{rule['id']}"):
            text = st.text_area("规则", rule["text"], height=68)
            c1, c2 = layout.cols(2, gap="small")
            scope = c1.selectbox(
                "适用范围",
                ["both", "review", "delegate"],
                index=["both", "review", "delegate"].index(rule["scope"]) if rule["scope"] in ("both", "review", "delegate") else 0,
                format_func=lambda x: rules.SCOPE_LABELS[x],
            )
            keywords = c2.text_input("关键词（顿号分隔）", rules.dump_keywords(rule["keywords"]))
            rationale = st.text_area("为什么有这条", rule["rationale"], height=60)
            saved = st.form_submit_button("保存")
        if saved:
            db.update_rule(
                rule["id"],
                text=text,
                rationale=rationale,
                scope=scope,
                keywords=rules.parse_keywords(keywords),
            )
            st.success("已保存")
            st.rerun()

        if layout.narrow():
            # 窄栏：三颗按钮竖排太费高度；删除是破坏性动作，自己占一行反而更安全
            row = st.columns(2, gap="small")
            b1, b2 = row[0], row[1]
            b1_ok = b1.button("归档" if rule["status"] == "active" else "重新启用",
                              key=f"{key_prefix}_arch_{rule['id']}", use_container_width=True)
            b2_ok = b2.button("命中记录", key=f"{key_prefix}_hist_{rule['id']}",
                              use_container_width=True)
            b3_ok = st.button("删除这条规则", key=f"{key_prefix}_del_{rule['id']}",
                              use_container_width=True)
            if b1_ok:
                db.update_rule(rule["id"], status="archived" if rule["status"] == "active" else "active")
                st.rerun()
            if b2_ok:
                st.session_state[f"{key_prefix}_show_hist_{rule['id']}"] = True
            if b3_ok:
                db.delete_rule(rule["id"])
                st.rerun()
        else:
            b1, b2, b3 = st.columns(3)
            if b1.button("归档" if rule["status"] == "active" else "重新启用", key=f"{key_prefix}_arch_{rule['id']}"):
                db.update_rule(rule["id"], status="archived" if rule["status"] == "active" else "active")
                st.rerun()
            if b2.button("查看命中记录", key=f"{key_prefix}_hist_{rule['id']}"):
                st.session_state[f"{key_prefix}_show_hist_{rule['id']}"] = True
            if b3.button("删除", key=f"{key_prefix}_del_{rule['id']}"):
                db.delete_rule(rule["id"])
                st.rerun()

        if st.session_state.get(f"{key_prefix}_show_hist_{rule['id']}"):
            rows = db.usages_for_rule(rule["id"])
            if not rows:
                st.caption("还没有被引用过。")
            for row in rows:
                where = row["review_headline"] or "那次评审已随历史清空"
                st.html(
                    f'<div style="font-size:12px;color:{MUTED};">{row["created_at"]} · {row["verdict"]}'
                    f" · {where[:40]}</div>",
                )

        if not compact:
            st.caption(f"来源：{src}　创建：{rule['created_at']}")


def render_review_card(
    review: dict,
    findings: list[dict],
    usages: list[dict],
    delegation: dict | None,
    agent_text: str,
    *,
    allow_fork: bool = True,
    key_prefix: str = "wb",
) -> None:
    """整张卡：上半部分供决定，下半部分供追问。"""
    render_summary(
        review, findings, agent_text, allow_fork=allow_fork, key_prefix=key_prefix
    )
    render_detail(review, findings, usages, delegation)
