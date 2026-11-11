"""把结构化结果卡讲成一段人话。

为什么要分两层：
- 纯模板：稳、可追溯，但读起来像系统通知；
- 纯模型自由发挥：像人，但说的每句话都没法核对，等于把追溯机制拆了。

所以代码先拼**段落骨架**（每段绑一个 finding id），模型只负责把每段改写成口语。
模型不许新增段落、不许换 ref；一旦它越界，这一段就丢弃；整体不够用就退回骨架。
这样"像真人说话"和"每句话都能点开看依据"可以同时成立。
"""
from __future__ import annotations

from . import db, llm, prompts
from .models import CERTAINTY_LABELS, DEVIATION_LABELS

MAX_DOUBT_PARAS = 2
MAX_BRANCH_PARAS = 2
# 开场白最多 6 段：再多就要滚动才看得到按钮，"30 秒决定"就没了
# 确定性为什么降级收进首个气泡的 popover，下一步选项交给胶囊


def numbers(review: dict, findings: list[dict]) -> dict:
    """四格里最该被一眼看到的三个数字。UI 的数字条也读这里，保证和开场白一致。"""
    subs = [f for f in findings if f["kind"] == "sub_goal"]
    passed = sum(1 for f in subs if f["verdict"] == "pass")
    partial = sum(1 for f in subs if f["verdict"] == "partial")
    if review["completion_pct"] is None:
        comp = "无法核定"
    else:
        comp = f"{review['completion_pct']}%"
        comp += f"（{passed} 达标 / {partial} 部分 / {len(subs) - passed - partial} 未达标）"
    return {
        "完成度": comp,
        "确定性": CERTAINTY_LABELS.get(review["certainty"], review["certainty"]),
        "偏差": DEVIATION_LABELS.get(review["deviation"], review["deviation"]),
    }


def _anchored(item: dict) -> bool:
    return item.get("match_type") in ("exact", "fuzzy") or bool(item.get("rule_id"))


def segments(review: dict, findings: list[dict]) -> list[dict]:
    """段落骨架。ref 是这一段的追溯入口，前端拿它去挂「依据」按钮。"""
    segs: list[dict] = []
    num = numbers(review, findings)
    headline = review["headline"].rstrip("。.")
    segs.append(
        {
            "ref": "verdict",
            "fact": (
                f"结论是：{headline}。"
                f"完成度 {num['完成度']}，确定性{num['确定性']}，与目标{num['偏差']}。"
            ),
        }
    )

    doubts = [
        f for f in findings
        if f["kind"] in ("doubt", "risk") and _anchored(f) and f["state"] != "dismissed"
    ]
    doubts.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"] or "low", 3))
    for f in doubts[:MAX_DOUBT_PARAS]:
        fact = f"{f['title']}。{f['detail'] or '原文里能找到对应句子，但没有解释。'}"
        segs.append({"ref": f"{f['kind']}:{f['id']}", "fact": fact})

    branches = [
        f for f in findings
        if f["kind"] in ("action_item", "decision") and _anchored(f) and f["state"] == "open"
    ]
    # 决策优先：要用户拍板的，比要他动手的更该先讲
    branches.sort(key=lambda f: 0 if f["kind"] == "decision" else 1)
    for f in branches[:MAX_BRANCH_PARAS]:
        if f["kind"] == "decision":
            opts = "还是".join(f"「{o['label']}」" for o in f["options"][:2]) or "两条路线"
            fact = f"{f['title']}——{opts}。"
            if f["recommended"]:
                fact += f"我倾向「{f['recommended']}」。"
        else:
            fact = f"{f['title']}。{f['detail'] or ''}"
        segs.append({"ref": f"{f['kind']}:{f['id']}", "fact": fact.strip()})

    return segs


def _skeleton(segs: list[dict]) -> list[dict]:
    """模型不可用时的兜底：事实直接成段。不带任何修饰，但也不丢信息。"""
    return [{"ref": s["ref"], "text": s["fact"]} for s in segs]


def ensure_opening(review_id: str) -> dict | None:
    """幂等：一个 review 只开场一次。重复调用只读不写。"""
    review = db.get_review(review_id)
    if not review:
        return None
    rows = db.list_review_messages(review_id)
    for row in rows:
        if row["kind"] == "opening":
            return row

    findings = db.list_findings(review_id)
    segs = segments(review, findings)
    paras, used_llm = _polish(review, segs)

    content = "\n\n".join(p["text"] for p in paras)
    db.add_review_message(
        review_id,
        review["session_id"],
        content,
        role="assistant",
        kind="opening",
        refs=[dict(p, polished=used_llm) for p in paras],
    )
    return db.list_review_messages(review_id)[-1]


def _polish(review: dict, segs: list[dict]) -> tuple[list[dict], bool]:
    """返回 (段落, 是否用了模型)。任何异常都退回骨架，绝不留空气泡。"""
    result = db.get_result(review["result_id"])
    payload = {
        "goal": (db.get_delegation(review["delegation_id"]) or {}).get("goal", ""),
        "numbers": numbers(review, db.list_findings(review["id"])),
        "segments": segs,
        "head": (result or {}).get("raw_output", "")[:800],
    }
    try:
        raw = llm.call(
            "review_brief",
            payload,
            prompts.REVIEW_BRIEF_SYSTEM,
            prompts.review_brief_user(payload),
            temperature=0.4,
            max_tokens=900,
        )
    except Exception:  # noqa: BLE001 开场白不允许把整个评审拖垮
        return _skeleton(segs), False

    allowed = {s["ref"] for s in segs}
    order = {s["ref"]: i for i, s in enumerate(segs)}
    out: list[dict] = []
    for item in raw.get("paragraphs") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        text = str(item.get("text") or "").strip()
        if ref not in allowed or not text:
            continue  # 越界的段落直接丢：不能出现没有出处的断言
        out.append({"ref": ref, "text": text[:160]})

    if len(out) < max(2, len(segs) - 2):
        return _skeleton(segs), False
    out.sort(key=lambda p: order.get(p["ref"], 99))
    return out, True
