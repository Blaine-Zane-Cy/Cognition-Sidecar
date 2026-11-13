"""会话编排。UI 只调用这里的函数，不自己拼业务逻辑。

两条不变量（v2 加）：
- 建模失败不允许留下"只有 brief 的空会话"，失败就标 aborted，从下拉里消失；
- user / assistant 严格交替：模型挂了也补一格 kind='failure'，保证对话流不断层。
"""
from __future__ import annotations

import json

from . import clarify, db, delegation, llm, narrate, prompts, review, rules
from .models import BRANCH_LABELS, SLOT_LABELS

# 澄清轮次硬上限：无论模型是否填完，到这个数就强制用现有信息出卡。
# 防止弱模型/演示模式陷入"一直在问同一件事"的死循环。
MAX_CLARIFY_ROUNDS = 12


# ---------------------------------------------------------------- 澄清阶段


def start_session(brief: str) -> str:
    sid = db.create_session(brief.strip())
    db.save_clarify_state(sid, {}, [], 0)
    db.update_stage(sid, "clarifying")
    db.add_message(sid, "user", brief.strip(), kind="brief", round_no=0)
    try:
        ask(sid, round_no=1)
    except Exception:
        # 会话已经落库，但没建成：标记而不是留着冒充"进行中"
        db.mark_aborted(sid)
        raise
    return sid


def submit_answer(sid: str, text: str) -> None:
    rnd = db.next_round(sid)
    db.add_message(sid, "user", text.strip(), kind="chat", round_no=rnd)
    try:
        ask(sid, round_no=rnd, user_text=text.strip())
    except Exception:
        _record_failure(sid, rnd)
        raise


def retry_ask(sid: str) -> None:
    """LLM 失败后重试：复用上一轮的 round_no，先撤掉 failure 占位，不重复写用户消息。"""
    msgs = db.list_messages(sid)
    users = [m for m in msgs if m["role"] == "user"]
    if not users:
        ask(sid, round_no=1)
        return
    rnd = users[-1]["round_no"]
    db.clear_failures(sid, rnd)
    try:
        ask(sid, round_no=rnd, user_text=users[-1]["content"])
    except Exception:
        _record_failure(sid, rnd)
        raise


def _record_failure(sid: str, round_no: int) -> None:
    db.add_exchange(
        sid,
        round_no,
        "这一次没跑通（模型返回的结构不可用或请求失败）。你的回答已经存下来了，点「重试上一步」继续。",
        kind="failure",
    )


def ask(sid: str, round_no: int, user_text: str = "") -> None:
    state = db.get_clarify_state(sid)
    slots, skipped = state["slots"], state["skipped"]
    missing = clarify.missing(slots, skipped)

    if not missing and not user_text.strip():
        ensure_delegation(sid)
        return

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in db.list_messages(sid)
        if m["kind"] in ("chat", "question", "brief")
    ][-6:]
    payload = {
        "slots": slots,
        "skipped": [s.get("slot") for s in skipped],
        "missing": missing,
        "history": history,
        "user_text": user_text,
    }
    raw = llm.call(
        "clarify",
        payload,
        prompts.CLARIFY_SYSTEM,
        prompts.clarify_user(payload),
        temperature=0.3,
    )

    slots = clarify.merge(slots, raw.get("slot_updates") or {})
    db.save_clarify_state(sid, slots, skipped, state["round_count"] + 1)

    missing = clarify.missing(slots, skipped)
    if not missing:
        db.add_exchange(sid, round_no, "信息够了，我按这些内容生成委托卡。", kind="note")
        ensure_delegation(sid)
        return

    # 轮次上限：宁可带缺口出卡，也绝不无限循环。
    if state["round_count"] + 1 >= MAX_CLARIFY_ROUNDS and missing:
        db.add_exchange(
            sid,
            round_no,
            f"已到澄清轮次上限（{MAX_CLARIFY_ROUNDS}），我用现有信息直接出卡；"
            "缺的项你可以在委托卡里补或在结果卡阶段纠偏。",
            kind="note",
        )
        ensure_delegation(sid)
        return

    question = str(raw.get("next_question") or "").strip() or clarify.question_for(missing[0])
    db.add_exchange(sid, round_no, question, kind="question")


def skip_slots(sid: str, names: list[str], reason: str) -> None:
    """显式跳过：记录原因，后续结果卡确定性会被压级。"""
    state = db.get_clarify_state(sid)
    slots, skipped = state["slots"], list(state["skipped"])
    existing = {s.get("slot") for s in skipped}
    added = []
    for name in names:
        if name in SLOT_LABELS and name not in existing:
            skipped.append({"slot": name, "reason": reason.strip() or "用户选择跳过"})
            added.append(name)
    db.save_clarify_state(sid, slots, skipped, state["round_count"])

    if added:
        labels = "、".join(SLOT_LABELS[a] for a in added)
        db.add_exchange(
            sid,
            _current_round(sid),
            f"已跳过「{labels}」。这一项不会进委托卡，结果卡的确定性会因此降级。",
            kind="note",
        )
    if clarify.is_ready(slots, skipped):
        ensure_delegation(sid)


def _current_round(sid: str) -> int:
    msgs = db.list_messages(sid)
    return int(msgs[-1]["round_no"]) if msgs else 0


# ---------------------------------------------------------------- 委托卡


def ensure_delegation(sid: str) -> str:
    state = db.get_clarify_state(sid)
    sess = db.get_session(sid) or {}
    brief = (sess.get("brief") or "").strip()
    card = delegation.generate(state["slots"], state["skipped"], brief)
    did = delegation.save(sid, card)
    db.update_stage(sid, "delegation_ready")
    return did


def update_delegation(sid: str, card: dict) -> str:
    # 编辑委托卡时保留原始构想：除非调用方显式给了新 brief，否则沿用库里那版。
    existing = db.latest_delegation(sid) or {}
    if not str(card.get("brief") or "").strip():
        card = dict(card)
        card["brief"] = existing.get("brief", "")
    did = delegation.save(sid, card)
    db.update_stage(sid, "delegation_ready")
    return did


def force_delegation(sid: str) -> None:
    """用户主动喊停澄清：把剩下的缺失槽位全部标记跳过，直接出卡。"""
    state = db.get_clarify_state(sid)
    if clarify.is_ready(state["slots"], state["skipped"]):
        ensure_delegation(sid)
        return
    missing = clarify.missing(state["slots"], state["skipped"])
    flow_skip = [s for s in missing if s not in {x.get("slot") for x in state["skipped"]}]
    if flow_skip:
        skip_slots(sid, flow_skip, "用户选择直接出卡，剩余项先跳过")
    else:
        ensure_delegation(sid)


def to_awaiting(sid: str) -> None:
    db.update_stage(sid, "awaiting_result")


def to_clarify(sid: str) -> None:
    db.update_stage(sid, "clarifying")
    db.add_exchange(
        sid, _current_round(sid), "回到澄清阶段。哪一项要改，直接说，我更新委托卡。", kind="note"
    )


# ---------------------------------------------------------------- 分支（待办 / 决策）


def revisit_branch(finding_id: int) -> dict | None:
    """回溯：只读，不新建任何实体，只把这条标记成看过。"""
    f = db.get_finding(finding_id)
    if not f:
        return None
    if f["state"] == "open":
        db.update_finding(finding_id, state="revisited")
    return db.get_finding(finding_id)


def decide_branch(finding_id: int, chosen: str) -> None:
    """记下选了哪条：只记录，不自动改委托卡（方向盘在人手里）。"""
    db.update_finding(finding_id, state="decided", chosen=chosen.strip())


def fork_from_finding(finding_id: int) -> str:
    """以此新建会话：有意的分叉，必须留血缘，否则和"缺陷分叉"分不清。"""
    f = db.get_finding(finding_id)
    if not f:
        raise ValueError("这条分支不存在，可能已被删除。")
    review_row = db.get_review(f["review_id"])
    card = db.get_delegation(review_row["delegation_id"]) if review_row else None
    goal = (card or {}).get("goal") or ""
    label = BRANCH_LABELS.get(f["kind"], f["kind"])

    brief = f"{f['title']}｜承接自 {f['review_id']}"
    if goal:
        brief += f"｜原目标：{goal}"

    sid = db.create_session(
        brief, parent_review_id=f["review_id"], parent_finding_id=finding_id
    )
    db.save_clarify_state(sid, {"goal": f["title"]}, [], 0)
    db.update_stage(sid, "clarifying")
    db.add_message(sid, "user", brief, kind="brief", round_no=0)
    db.add_message(
        sid,
        "system",
        f"本会话承接自结果卡 {f['review_id']} 的第 {f['id']} 条{label}。"
        + (f"原委托目标：{goal}" if goal else ""),
        kind="note",
        round_no=0,
    )
    db.update_finding(finding_id, state="followed")
    try:
        ask(sid, round_no=1)
    except Exception:
        _record_failure(sid, 1)
    return sid


# ---------------------------------------------------------------- 评审


def submit_result(sid: str, raw_output: str, source: str = "paste") -> str:
    """贴回结果 → 生成结构化评审 → 立刻让审阅专家开口。

    开场白在这里生成而不是在 UI 里：这样脚本、demo、UI 三个入口看到的是同一段话，
    不会出现"网页上有开场白、导出里没有"的情况。
    """
    card = db.latest_delegation(sid)
    if not card:
        raise ValueError("还没有委托卡，无法评审。")
    result_id = db.create_result(sid, card["id"], raw_output, source=source)
    review_id = review.generate(result_id)
    narrate.ensure_opening(review_id)  # 内部已兜底：模型挂了也留骨架
    return review_id


def accept(review_id: str) -> None:
    rev = db.get_review(review_id)
    db.decide_review(review_id, "accepted")
    if rev:
        db.update_stage(rev["session_id"], "accepted")


def reject(review_id: str, reason: str) -> str:
    """驳回 → 理由沉淀成规则 → 下次评审会被引用。"""
    rev = db.get_review(review_id)
    if not rev:
        raise ValueError("结果卡不存在")
    if not reason.strip():
        raise ValueError("驳回必须写理由")

    findings = db.list_findings(review_id)
    doubts = [f for f in findings if f["kind"] == "doubt"]
    draft = rules.create_from_reason(reason.strip(), rev, doubts)
    rule_id = rules.save_from_reject(review_id, draft)

    db.decide_review(review_id, "rejected", reason.strip())
    db.update_stage(rev["session_id"], "rejected")
    return rule_id


def redirect(review_id: str, reason: str = "") -> None:
    rev = db.get_review(review_id)
    if not rev:
        raise ValueError("结果卡不存在")
    db.decide_review(review_id, "redirected", reason.strip() or None)
    db.update_stage(rev["session_id"], "clarifying")
    note = "你选择了改方向。" + (f"原因：{reason.strip()}" if reason.strip() else "")
    db.add_exchange(
        rev["session_id"], _current_round(rev["session_id"]), note + " 直接说新的方向，我更新委托卡。", kind="note"
    )


# ---------------------------------------------------------------- 辅助


def card_text(sid: str) -> str:
    card = db.latest_delegation(sid)
    if not card:
        return ""
    return delegation.render_text(card)


def session_overview(sid: str) -> dict:
    sess = db.get_session(sid)
    if not sess:
        return {}
    state = db.get_clarify_state(sid)
    return {
        "session": sess,
        "slots": state["slots"],
        "skipped": state["skipped"],
        "missing": clarify.missing(state["slots"], state["skipped"]),
        "delegation": db.latest_delegation(sid),
        "review": db.latest_review(sid),
    }


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
