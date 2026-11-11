"""结果程度卡生成。

模型只负责“说人话”，能不能算数由服务端判断：
- quote 必须锚回原文（anchoring），锚不上就移出疑点区、并压确定性；
- 确定性由 模型自评 与 客观事实（跳过槽位/未锚定条目）取低者；
- 完成度由成功标准的逐条判定算出来，不由模型直接给一个百分数。
"""
from __future__ import annotations

from . import anchoring, db, llm, prompts, rules, settings
from .models import (
    CERTAINTY_LABELS,
    CERTAINTY_ORDER,
    DEVIATION_LABELS,
    SLOT_LABELS,
    Anchor,
)

MAX_DOUBTS = 3
MAX_RISKS = 3
MAX_BRANCHES = {"action_item": 4, "decision": 3}
DEFAULT_OPTIONS = [
    {"label": "接受当前结果，进入下一件事", "cost": "已知风险留在原地，后续返工成本由你承担"},
    {"label": "带着疑点让 Agent 补一轮", "cost": "要多花一轮时间，且新输出仍需重新核对"},
    {"label": "改方向，重写委托卡", "cost": "放弃当前进度，但能避免在错的目标上继续投入"},
]


def generate(result_id: str) -> str:
    result = db.get_result(result_id)
    if not result:
        raise ValueError("结果不存在")
    delegation = db.get_delegation(result["delegation_id"])
    session = db.get_session(result["session_id"])
    if not delegation or not session:
        raise ValueError("委托卡或会话已丢失")

    state = db.get_clarify_state(session["id"])
    skipped = state["skipped"]
    threshold = settings.get_float("fuzzy_threshold", 0.75)

    candidates = rules.candidates(delegation, result["raw_output"])
    payload = {
        "delegation": {
            "goal": delegation["goal"],
            "deliverable": delegation["deliverable"],
            "boundaries": delegation["boundaries"],
            "forbidden": delegation["forbidden"],
            "success_criteria": delegation["success_criteria"],
            "keywords": delegation["keywords"],
        },
        "candidate_rules": rules.candidate_brief(candidates),
        "skipped": [SLOT_LABELS.get(s.get("slot"), s.get("slot")) for s in skipped],
        "agent_output": result["raw_output"],
    }
    raw = llm.call(
        "review",
        payload,
        prompts.REVIEW_SYSTEM,
        prompts.review_user(payload),
        temperature=0.15,
    )
    return _persist(result, delegation, skipped, candidates, raw, threshold)


def _persist(result, delegation, skipped, candidates, raw, threshold) -> str:
    text = result["raw_output"]
    cand_ids = {c["id"] for c in candidates}
    skip_slots = [s.get("slot") for s in skipped]

    # ---------- 疑点 / 风险：先限量，再逐条锚定
    doubts: list[dict] = []
    risks: list[dict] = []
    for item in raw.get("findings") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") if item.get("kind") in ("doubt", "risk") else "doubt"
        title = str(item.get("title") or "").strip()[:40]
        if not title:
            continue
        bucket = doubts if kind == "doubt" else risks
        if len(bucket) >= (MAX_DOUBTS if kind == "doubt" else MAX_RISKS):
            continue
        quote = str(item.get("quote") or "").strip()
        anchor = anchoring.locate(quote, text, threshold) if quote else Anchor("unanchored")
        rule_id = item.get("rule_id") if item.get("rule_id") in cand_ids else None
        anchored = anchor.ok or bool(rule_id)
        bucket.append(
            {
                "kind": kind,
                "title": title,
                "detail": str(item.get("detail") or "").strip(),
                "severity": item.get("severity") if item.get("severity") in ("high", "medium", "low") else None,
                "quote": quote or None,
                "anchor": anchor,
                "rule_id": rule_id,
                "certainty": "medium" if anchored else "low",
                "anchored": anchored,
            }
        )

    unanchored_count = sum(1 for f in doubts + risks if not f["anchored"])

    # ---------- 待办 / 决策：走同一套锚定，但未锚定不压确定性（它不是说结论）
    branches = _collect_branches(raw, text, threshold, cand_ids)

    # ---------- 成功标准逐条判定 → 完成度
    criteria = delegation.get("success_criteria") or []
    raw_subs = {}
    for sg in raw.get("sub_goals") or []:
        if isinstance(sg, dict):
            try:
                raw_subs[int(sg.get("criterion", -1))] = sg
            except (TypeError, ValueError):
                continue

    sub_goals = []
    score = 0.0
    judged = 0
    for idx, crit in enumerate(criteria):
        sg = raw_subs.get(idx, {})
        verdict = sg.get("verdict") if sg.get("verdict") in ("pass", "partial", "fail") else "fail"
        evidence = str(sg.get("evidence") or "").strip()
        anchor = anchoring.locate(evidence, text, threshold) if evidence else Anchor("unanchored")
        score += {"pass": 1.0, "partial": 0.5, "fail": 0.0}[verdict]
        judged += 1
        sub_goals.append(
            {
                "criterion": idx,
                "title": crit.get("text", ""),
                "how_to_verify": crit.get("how_to_verify", ""),
                "verdict": verdict,
                "note": str(sg.get("note") or ("模型未给出判定" if not sg else "")).strip(),
                "quote": evidence or None,
                "anchor": anchor,
            }
        )

    criteria_skipped = "success_criteria" in skip_slots
    if criteria_skipped or judged == 0:
        completion = None
    else:
        completion = int(round(100 * score / judged))

    # ---------- 确定性：模型自评 vs 客观约束，取低者
    base = raw.get("certainty") if raw.get("certainty") in CERTAINTY_ORDER else "medium"
    cap = "high"
    notes: list[str] = []

    if skip_slots:
        labels = "、".join(SLOT_LABELS.get(s, s) for s in skip_slots)
        cap = _lower(cap, "medium")
        notes.append(f"委托阶段显式跳过了「{labels}」，确定性上限降为「推测」")
    if criteria_skipped:
        cap = "low"
        notes.append("成功标准被跳过，完成度无法核定，确定性降为「存疑」")
    if unanchored_count:
        cap = _lower(cap, "medium")
        notes.append(f"{unanchored_count} 条疑点未能在原文中定位，不足以支撑「确定」")
    if unanchored_count >= 2:
        cap = "low"

    final = base if CERTAINTY_ORDER[base] <= CERTAINTY_ORDER[cap] else cap
    if final != base:
        notes.append(
            f"模型自评「{CERTAINTY_LABELS[base]}」，按上述约束下调为「{CERTAINTY_LABELS[final]}」"
        )

    deviation = raw.get("deviation") if raw.get("deviation") in DEVIATION_LABELS else "minor"
    options = _normalize_options(raw.get("next_options"))
    headline = str(raw.get("headline") or "").strip()[:60] or "模型未给出结论句，请直接看下方维度。"

    review_id = db.create_review(
        session_id=result["session_id"],
        result_id=result["id"],
        delegation_id=delegation["id"],
        headline=headline,
        completion_pct=completion,
        sub_goals=[{k: v for k, v in sg.items() if k != "anchor"} for sg in sub_goals],
        certainty=final,
        certainty_note="；".join(notes),
        deviation=deviation,
        deviation_note=str(raw.get("deviation_note") or "").strip(),
        next_options=options,
        skipped=skipped,
        unanchored_count=unanchored_count,
        low_confidence=bool(skip_slots or unanchored_count),
        provider=llm.provider(),
        raw=raw,
    )

    for sg in sub_goals:
        anchor = sg["anchor"]
        db.add_finding(
            review_id,
            kind="sub_goal",
            title=sg["title"],
            detail=sg["note"],
            certainty="medium",
            verdict=sg["verdict"],
            anchor_type="quote" if anchor.ok else "none",
            quote=sg["quote"],
            char_start=anchor.char_start,
            char_end=anchor.char_end,
            match_type=anchor.match_type if sg["quote"] else None,
            match_score=anchor.score if sg["quote"] else None,
        )

    for f in doubts + risks:
        anchor = f["anchor"]
        db.add_finding(
            review_id,
            kind=f["kind"],
            title=f["title"],
            detail=f["detail"],
            certainty=f["certainty"],
            severity=f["severity"],
            anchor_type="rule" if (f["rule_id"] and not anchor.ok) else ("quote" if anchor.ok else "none"),
            quote=f["quote"],
            char_start=anchor.char_start,
            char_end=anchor.char_end,
            match_type=anchor.match_type,
            match_score=anchor.score,
            rule_id=f["rule_id"],
        )

    for b in branches:
        anchor = b["anchor"]
        anchored = anchor.ok or bool(b["rule_id"])
        db.add_finding(
            review_id,
            kind=b["kind"],
            title=b["title"],
            detail=b["detail"],
            certainty="medium" if anchored else "low",
            severity=b["severity"],
            anchor_type="rule" if (b["rule_id"] and not anchor.ok) else ("quote" if anchor.ok else "none"),
            quote=b["quote"],
            char_start=anchor.char_start,
            char_end=anchor.char_end,
            match_type=anchor.match_type if b["quote"] else None,
            match_score=anchor.score if b["quote"] else None,
            rule_id=b["rule_id"],
            options=b["options"],
            recommended=b["recommended"],
            why=b["why"],
        )

    # ---------- 规则引用：只接受候选集内的 id
    used: set[str] = set()
    for check in raw.get("rule_checks") or []:
        if not isinstance(check, dict):
            continue
        rule_id = check.get("rule_id")
        if rule_id not in cand_ids or rule_id in used:
            continue
        verdict = check.get("verdict") if check.get("verdict") in ("satisfied", "violated") else "mentioned"
        db.add_usage(rule_id, review_id, verdict, str(check.get("reason") or "").strip())
        used.add(rule_id)
    for f in doubts + risks:
        if f["rule_id"] and f["rule_id"] not in used:
            db.add_usage(f["rule_id"], review_id, "cited", f"疑点「{f['title']}」引用了该规则")
            used.add(f["rule_id"])
    for b in branches:
        if b["rule_id"] and b["rule_id"] not in used:
            db.add_usage(b["rule_id"], review_id, "cited", f"分支「{b['title']}」引用了该规则")
            used.add(b["rule_id"])

    db.update_stage(result["session_id"], "reviewing")
    return review_id


def _collect_branches(raw: dict, text: str, threshold: float, cand_ids: set) -> list[dict]:
    """把 action_items / decisions 收敛成和疑点同构的条目，走同一套锚定。"""
    out: list[dict] = []
    for field, kind in (("action_items", "action_item"), ("decisions", "decision")):
        limit = MAX_BRANCHES[kind]
        taken = 0
        for item in raw.get(field) or []:
            if not isinstance(item, dict) or taken >= limit:
                continue
            title = str(item.get("title") or "").strip()[:40]
            if not title:
                continue

            options = _normalize_options_inner(item.get("options"))
            if kind == "decision" and len(options) < 2:
                continue  # 只有一个选项就不是分叉，别占首屏

            quote = str(item.get("quote") or "").strip()
            anchor = anchoring.locate(quote, text, threshold) if quote else Anchor("unanchored")
            rule_id = item.get("rule_id") if item.get("rule_id") in cand_ids else None
            recommended = str(item.get("recommended") or "").strip()[:30]
            labels = [o["label"] for o in options]
            if kind == "decision" and recommended not in labels:
                recommended = labels[0] if labels else ""

            out.append(
                {
                    "kind": kind,
                    "title": title,
                    "detail": str(item.get("detail") or "").strip(),
                    "why": str(item.get("why") or "").strip()[:60],
                    "severity": item.get("severity")
                    if item.get("severity") in ("high", "medium", "low")
                    else None,
                    "quote": quote or None,
                    "anchor": anchor,
                    "rule_id": rule_id,
                    "options": options,
                    "recommended": recommended,
                }
            )
            taken += 1
    return out


def _normalize_options_inner(options) -> list[dict]:
    out = []
    for opt in options or []:
        if isinstance(opt, dict) and str(opt.get("label") or "").strip():
            out.append(
                {
                    "label": str(opt["label"]).strip()[:30],
                    "cost": str(opt.get("cost") or "").strip()[:60],
                }
            )
        elif isinstance(opt, str) and opt.strip():
            out.append({"label": opt.strip()[:30], "cost": ""})
    return out[:4]


def _lower(a: str, b: str) -> str:
    return a if CERTAINTY_ORDER[a] <= CERTAINTY_ORDER[b] else b


def _normalize_options(options) -> list:
    out = []
    for item in options or []:
        if isinstance(item, dict) and str(item.get("label") or "").strip():
            out.append(
                {
                    "label": str(item["label"]).strip()[:40],
                    "cost": str(item.get("cost") or "").strip()[:60],
                }
            )
        elif isinstance(item, str) and item.strip():
            out.append({"label": item.strip()[:40], "cost": ""})
    return out[:4] if out else DEFAULT_OPTIONS
