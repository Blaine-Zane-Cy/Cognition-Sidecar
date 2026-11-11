"""审阅对话：把一次评审变成一段能继续的对话。

分工（这是整个模块的骨架）：
- 模型只负责「听懂这句话想干什么」和「把回应用人话说出来」；
- 改哪条 finding、能不能改、按钮点了会发生什么，全部由代码判定。

为什么不让模型直接执行：
它可能把「这条我允许」理解成「驳回」，也可能引用一条不存在的疑点。
所以 intent 里的 finding_id 必须在候选集内，option_label 必须逐字等于某个选项，
驳回/接受这类不可逆动作还要再过一道关键词闸门 —— 没有明说就不执行。
"""
from __future__ import annotations

from . import db, llm, narrate, prompts, rules

MAX_INTENTS = 3
SEV_RANK = {"high": 0, "medium": 1, "low": 2}
TRUSTED_SCOPES = ("both", "review", "delegate")

# 不可逆动作的关键词闸门：模型说是「接受」，但用户没这么写，就不执行
_ACCEPT_WORDS = ("接受", "可以收", "收下", "就这样", "通过了", "过吧", "没问题了", "可以了")
_REJECT_WORDS = ("驳回", "不收", "不能收", "重做", "重来", "打回", "不行", "退回去")
_REDIRECT_WORDS = ("改方向", "换个方向", "方向不对", "目标不对", "别做这个")


# ---------------------------------------------------------------- 候选集


def _anchored(item: dict) -> bool:
    return item.get("match_type") in ("exact", "fuzzy") or bool(item.get("rule_id"))


def candidates(review_id: str) -> dict:
    """对话里能被指到的东西，带编号。

    编号只发给"气泡里真的出现过"的条目：未锚定的疑点不会进开场白，
    给它编号会让"第二个疑点"指向一个用户看不见的东西。
    """
    findings = db.list_findings(review_id)
    anchored = [
        f for f in findings if f["kind"] in ("doubt", "risk") and _anchored(f)
    ]
    anchored.sort(key=lambda f: (SEV_RANK.get(f["severity"] or "low", 3), f["id"]))
    # 编号对全部锚定条目一次性发下去，被排除的也保留原号：
    # 否则排除掉第 1 条之后，"第二个疑点"在下一轮会指向另一个东西。
    for i, f in enumerate(anchored, 1):
        f["no"] = i
    doubts = [f for f in anchored if f["state"] != "dismissed"]

    decisions = [f for f in findings if f["kind"] == "decision"]
    for i, f in enumerate(decisions, 1):
        f["no"] = i

    action_items = [
        f for f in findings if f["kind"] == "action_item" and f["state"] == "open"
    ]
    for i, f in enumerate(action_items, 1):
        f["no"] = i

    dismissed = [f for f in anchored if f["state"] == "dismissed"]
    decided = [f for f in decisions if f["state"] == "decided" and f["chosen"]]
    return {
        "all": findings,
        "doubts": doubts,
        "doubts_all": anchored,
        "decisions": decisions,
        "actions": action_items,
        "dismissed": dismissed,
        "decided": decided,
    }


def _brief(list_: list[dict], with_options: bool = False) -> list[dict]:
    out = []
    for f in list_:
        item = {
            "id": f["id"],
            "no": f.get("no"),
            "kind": f["kind"],
            "title": f["title"],
            "detail": (f["detail"] or "")[:120],
            "state": f["state"],
        }
        if with_options:
            item["options"] = [o["label"] for o in f["options"]]
            item["chosen"] = f["chosen"]
        out.append(item)
    return out


# ---------------------------------------------------------------- 开场白


def ensure_opening(review_id: str) -> dict | None:
    return narrate.ensure_opening(review_id)


# ---------------------------------------------------------------- 一轮对话


def respond(review_id: str, user_text: str) -> dict:
    """用户说一句话 → 记录 → 解析 → 执行 → 回一句 → 给出下一步胶囊。"""
    review = db.get_review(review_id)
    if not review:
        raise ValueError("这次评审不存在。")
    text = (user_text or "").strip()
    if not text:
        raise ValueError("说点什么再发。")

    sid = review["session_id"]
    db.add_review_message(review_id, sid, text, role="user", kind="chat")

    cands = candidates(review_id)
    payload = {
        "numbers": narrate.numbers(review, cands["all"]),
        "doubts": _brief(cands["doubts"]),
        "decisions": _brief(cands["decisions"], with_options=True),
        "action_items": _brief(cands["actions"]),
        "history": [
            {"role": m["role"], "content": m["content"][:300]}
            for m in db.list_review_messages(review_id)[-6:]
        ],
        "user_text": text,
    }

    try:
        raw = llm.call(
            "review_reply",
            payload,
            prompts.REVIEW_REPLY_SYSTEM,
            prompts.review_reply_user(payload),
            temperature=0.2,
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001 模型挂了也不能让这句话凭空消失
        note = f"你这句话我存下了，但这次没能理解（{type(exc).__name__}）。等下再说一遍，或者直接用下面的按钮。"
        db.add_review_message(review_id, sid, note, role="assistant", kind="failure")
        return {"say": note, "applied": [], "actions": build_actions(review_id), "failed": True}

    say = str(raw.get("say") or "").strip() or "我记下了。"
    suggest = str(raw.get("suggest") or "").strip()
    applied = _apply(review, cands, raw.get("intents") or [], text)

    refs = [{"ref": "say", "text": say}]
    for item in applied:
        refs.append({"ref": item["ref"], "text": item["line"]})
    if suggest:
        refs.append({"ref": "suggest", "text": suggest})

    content = "\n\n".join(r["text"] for r in refs)
    db.add_review_message(review_id, sid, content, role="assistant", kind="chat", refs=refs)
    return {
        "say": say,
        "applied": applied,
        "actions": build_actions(review_id),
        "failed": False,
    }


def _apply(review: dict, cands: dict, intents: list, user_text: str) -> list[dict]:
    applied: list[dict] = []
    by_id = {f["id"]: f for f in cands["all"]}

    for intent in intents[:MAX_INTENTS]:
        if not isinstance(intent, dict):
            continue
        typ = str(intent.get("type") or "").strip()
        if typ in ("none", ""):
            continue

        if typ == "dismiss":
            item = _dismiss(by_id, intent, review)
            if item:
                applied.append(item)
        elif typ == "decide":
            item = _decide(by_id, intent)
            if item:
                applied.append(item)
        elif typ == "rule":
            item = _rule_from_intent(intent, review)
            if item:
                applied.append(item)
        elif typ in ("accept", "reject", "redirect"):
            item = _verdict(typ, review, user_text)
            if item:
                applied.append(item)

    if not applied:
        applied.append(
            {
                "ref": "none",
                "line": "这句我没读出一条能落地的指令，只当作讨论记在对话里了。要动手的话，用下面的按钮，或者点名第几条。",
            }
        )
    return applied


def _dismiss(by_id: dict, intent: dict, review: dict) -> dict | None:
    try:
        fid = int(intent.get("finding_id"))
    except (TypeError, ValueError):
        return None
    f = by_id.get(fid)
    if not f or f["kind"] not in ("doubt", "risk") or f["state"] in ("dismissed", "followed"):
        return None

    reason = str(intent.get("reason") or "用户说这条可以不管").strip()[:80]
    db.update_finding(fid, state="dismissed", chosen=reason)

    rule_id = ""
    if str(intent.get("rule_text") or "").strip():
        rule_id = _save_rule(intent, review, default_keywords=[f["title"]])
    line = f"好，「{f['title']}」这条我不再提了。"
    line += f"顺手记成规则 {rule_id}，下次见到同类问题我不再拦。" if rule_id else "这次先记在对话里。"
    return {"ref": f"doubt:{fid}", "line": line}


def _decide(by_id: dict, intent: dict) -> dict | None:
    try:
        fid = int(intent.get("finding_id"))
    except (TypeError, ValueError):
        return None
    f = by_id.get(fid)
    if not f or f["kind"] != "decision" or f["state"] != "open":
        return None
    labels = [o["label"] for o in f["options"]]
    pick = str(intent.get("option_label") or "").strip()
    if pick not in labels:
        return None  # 选项必须逐字对上，不让模型自己编一个

    db.update_finding(fid, state="decided", chosen=pick)
    return {
        "ref": f"decision:{fid}",
        "line": f"记下了：{f['title']} → 「{pick}」。这条只是记下来，委托卡我没动，改不改你说了算。",
    }


def _rule_from_intent(intent: dict, review: dict) -> dict | None:
    text = str(intent.get("rule_text") or "").strip()
    if len(text) < 4:
        return None
    rule_id = _save_rule(intent, review)
    if not rule_id:
        return None
    return {"ref": "rule", "line": f"归成规则 {rule_id}：{text}"}


def _save_rule(intent: dict, review: dict, default_keywords: list | None = None) -> str:
    text = str(intent.get("rule_text") or "").strip()
    if not text:
        return ""
    text = text[:80]
    scope = intent.get("scope") if intent.get("scope") in TRUSTED_SCOPES else "review"

    keywords = [str(k).strip() for k in (intent.get("keywords") or []) if str(k).strip()]
    for extra in default_keywords or []:
        keywords.extend(rules.derive_keywords(extra))
    if not keywords:
        keywords = rules.derive_keywords(text)

    seen: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.append(k)

    # 关键词必须真的能在"委托目标 + Agent 输出"里出现，否则这条规则永远不会被引用，
    # 只是规则库里多一行噪音。
    seen = [k for k in seen if k in _haystack(review)] or seen[:3]

    return db.create_rule(
        text=text,
        rationale=f"来自审阅对话：{text[:40]}",
        scope=scope,
        keywords=seen[:6],
        source="chat",
        source_review_id=review["id"],
        status="active",
    )


def _haystack(review: dict) -> str:
    result = db.get_result(review["result_id"])
    card = db.get_delegation(review["delegation_id"]) or {}
    return (card.get("goal") or "") + "\n" + ((result or {}).get("raw_output") or "")


def _verdict(typ: str, review: dict, user_text: str) -> dict | None:
    """接受/驳回/改方向：模型说了不算，用户得自己写到这几个词。"""
    from . import flow  # 延迟导入：flow 不依赖本模块，避免加载期成环

    rid = review["id"]
    if typ == "accept":
        if not any(w in user_text for w in _ACCEPT_WORDS):
            return None
        flow.accept(rid)
        return {"ref": "verdict", "line": "这次就收了。之后要接着看新一轮，直接贴回来。"}
    if typ == "reject":
        if not any(w in user_text for w in _REJECT_WORDS):
            return None
        if not user_text.strip():
            return None
        rule_id = flow.reject(rid, user_text.strip())
        line = "那就打回。你这句话我已经转成规则"
        line += f" {rule_id}，下一次审批会先按它看。" if rule_id else "，下次同类问题我会先提。"
        return {"ref": "verdict", "line": line}
    if typ == "redirect":
        if not any(w in user_text for w in _REDIRECT_WORDS):
            return None
        flow.redirect(rid, user_text.strip())
        return {"ref": "verdict", "line": "方向改了。回到澄清，直接说新方向，我重出一版委托卡。"}
    return None


# ---------------------------------------------------------------- 下一步胶囊


# ---------------------------------------------------------------- 快捷动作


def quick_dismiss(review_id: str, finding_id: int, reason: str = "") -> dict:
    """气泡上的「这条我允许」：等同于用户说了一句话，所以也照对话记一笔。

    走的是和对话完全相同的落库路径——否则"点按钮"和"打字"会沉淀出两种状态，
    以后复盘就分不清哪条是点的、哪条是说的。
    """
    review = db.get_review(review_id)
    f = db.get_finding(finding_id)
    if not review or not f or f["kind"] not in ("doubt", "risk"):
        return {}
    if f["state"] == "dismissed":
        return {"line": "这条已经排除过了。"}

    text = f"「{f['title']}」这条我允许，不用再提。"
    db.add_review_message(review_id, review["session_id"], text, role="user", kind="chat")

    picked = _dismiss(
        {finding_id: f},
        {
            "finding_id": finding_id,
            "rule_text": f"「{f['title']}」这类问题可以接受，不必再提",
            "keywords": rules.derive_keywords(f["title"]),
            "scope": "review",
            "reason": reason or "用户在气泡上点了「这条我允许」",
        },
        review,
    )
    refs = [{"ref": "say", "text": "好，这条划掉。"}]
    if picked:
        refs.append({"ref": picked["ref"], "text": picked["line"]})
    db.add_review_message(
        review_id,
        review["session_id"],
        "\n\n".join(r["text"] for r in refs),
        role="assistant",
        kind="chat",
        refs=refs,
    )
    return picked or {}


def build_actions(review_id: str) -> list[dict]:
    """按钮由状态算出来，不由模型给。所以每颗按钮点下去一定有事发生。"""
    review = db.get_review(review_id)
    if not review:
        return []
    pending = review["status"] == "pending"
    cands = candidates(review_id)
    has_material = bool(cands["dismissed"] or cands["decided"] or cands["actions"] or cands["doubts"])

    acts: list[dict] = []
    if pending:
        acts.append({"action": "accept", "label": "接受这次结果", "hint": "结案，不再改"})
        acts.append(
            {"action": "reject", "label": "整体驳回", "hint": "先在输入框写理由，理由会变成规则"}
        )
        if has_material:
            acts.append(
                {
                    "action": "followup",
                    "label": "打包成补充说明",
                    "hint": "把已排除 / 已定 / 还欠的整理成一段，你发给 Agent",
                }
            )
        if cands["actions"] and not db.list_branches(review_id):
            # 有分支入口卡之后，这一颗就多余了：待办就在上面，点卡片就能聊。
            # 只留给"还没物化成分支"的老评审。
            acts.append(
                {"action": "fork", "label": "挑一条开新会话", "hint": "待办单独立项去做"}
            )
    else:
        acts.append({"action": "next_round", "label": "贴下一轮结果", "hint": "同一个委托"})
    if len(acts) < 4:  # 胶囊最多四颗，再多就挤成一行看不清；卡片在页尾一直有入口
        acts.append({"action": "card", "label": "展开完整结果卡", "hint": "四格标尺 + 全部条目"})
    return acts


# ---------------------------------------------------------------- 补充说明


def build_followup(review_id: str) -> str:
    """把对话里已经定下的事，压成一段可以直接甩给工作 Agent 的话。"""
    review = db.get_review(review_id)
    if not review:
        return ""
    cands = candidates(review_id)
    card = db.get_delegation(review["delegation_id"]) or {}

    lines = [
        "【补充说明 · 基于上一版】",
        f"上一版我看过了，结论：{review['headline']}",
        "请按下面几点修改，没提到的部分保持原样，不要顺手改别的：",
        "",
    ]
    n = 1

    if cands["dismissed"]:
        lines.append(f"{n}. 下面这些我确认可以接受，不要再动：")
        for f in cands["dismissed"]:
            lines.append(f"   - {f['title']}")
        n += 1

    if cands["decided"]:
        lines.append(f"{n}. 下面几处已经定了，按这个来：")
        for f in cands["decided"]:
            lines.append(f"   - {f['title']} → {f['chosen']}")
        n += 1

    open_actions = cands["actions"]
    if open_actions:
        lines.append(f"{n}. 下面这些必须补上：")
        for f in open_actions:
            lines.append(f"   - {f['title']}" + (f"（{f['detail']}）" if f["detail"] else ""))
        n += 1

    if cands["doubts"]:
        lines.append(f"{n}. 下面这些还没解释清楚，请补一句依据（引用原文或数据来源）：")
        for f in cands["doubts"]:
            lines.append(f"   - {f['title']}")
        n += 1

    # 分支里聊清楚的结论，直接汇进这份说明 —— 用户不用自己把 8 段对话手工总结一遍
    merged = [b for b in db.list_branches(review_id) if b["state"] == "merged" and b["resolution"]]
    if merged:
        lines.append(f"{n}. 下面几条已经聊清楚了，按这些结论执行：")
        for b in merged:
            lines.append(f"   - {b['title']} → {b['resolution']}")
        n += 1

    criteria = card.get("success_criteria") or []
    if criteria:
        lines.append("")
        lines.append("验收口径不变，还是这几条：")
        for i, c in enumerate(criteria, 1):
            how = c.get("how_to_verify") or ""
            lines.append(f"   {i}. {c['text']}" + (f"（怎么验证：{how}）" if how else ""))

    lines.append("")
    lines.append("改完把完整输出再发我一遍，不要只发改动部分。")
    return "\n".join(lines)


def transcript_text(review_id: str, with_branches: bool = False) -> str:
    """整段对话的纯文本，方便导出或粘到别处。

    默认只给主干 —— 要看全必须显式 `with_branches=True`，
    和 `db.list_review_messages` 同一个规矩：不给"忘了过滤"留活口。
    """
    out = []
    for m in db.list_review_messages(review_id):
        who = "你" if m["role"] == "user" else "侧车"
        out.append(f"[{who}] {m['content']}")

    if with_branches:
        from .models import BRANCH_KINDS  # 循环导入的口子留在函数里

        for b in db.list_branches(review_id):
            msgs = db.list_branch_messages(b["id"])
            if not msgs:
                continue
            head = "待办" if b["kind"] in BRANCH_KINDS else b["kind"]
            state = {"merged": "已合并", "dropped": "已放弃"}.get(b["state"], "未合并")
            out.append("")
            out.append(f"—— 分支「{b['title']}」（{head} · {state}）——")
            for m in msgs:
                who = "你" if m["role"] == "user" else "侧车"
                out.append(f"[{who}] {m['content']}")
            if b["resolution"]:
                out.append(f"[结论] {b['resolution']}")
    return "\n\n".join(out)
