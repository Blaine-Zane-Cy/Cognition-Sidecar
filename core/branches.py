"""分支对话：一条待办/决策 = 一支只聊这一件事的对话。

分工与主干一致：模型负责「听懂这句话想干什么」和「把话说人话」，
改什么状态、能不能合并，全部由代码判定（见 `_apply` / `merge`）。

这个模块真正的难点不是对话，是**上下文怎么组装**：
主干可能有 3 万字，不可能每轮都塞给模型。所以分四层给：

  ① 委托卡（目标/交付物/验收标准/禁止项）—— 不截断，缺一条就答偏
  ② 锚点窗口（该条 quote ±500 字）—— 判断只能基于这段
  ③ 全文骨架（段号 + 每段首句）—— 只用来定位"这事在全文哪儿"
  ④ 分支历史（最近 6 轮）

超预算时按 历史 → 骨架 → 锚点 的顺序裁剪，委托卡永远不动。
全文并没有被丢掉：`find_in_source` 允许用户按需把某段取回来，
而且**先渲染给用户看，再由用户决定要不要塞进下一轮** —— 扩不扩上下文，
决策权在用户手里，不在模型手里。
"""
from __future__ import annotations

import re

from . import db, llm, prompts

# ---------------------------------------------------------------- 预算
CARD_CAP = 900              # 委托卡整层上限（正常委托卡 400~800 字，很少触发）
ANCHOR_CTX = 500            # 锚点前后各取多少字
ANCHOR_CAP = 1200
SKELETON_SEGS = 30          # 骨架最多几段
SKELETON_HEAD = 40          # 每段取首句多少字
SKELETON_CAP = 800
HISTORY_TURNS = 6
HISTORY_CHARS = 300
TOTAL_BUDGET = 3500         # 中文 1 字 ≈ 1 token，约 3.5k 输入 token

# "就这么办"这类话才允许生成结论；其余一律只当讨论
_CONCLUDE_WORDS = (
    "就这么办", "就这么定", "就这样吧", "就按这个", "按这个来",
    "我确认", "确认了", "结论是", "定了", "可以结了",
)


# ---------------------------------------------------------------- 分支入口


def ensure(review_id: str) -> list[dict]:
    """确认这次评审的分支都已物化，返回全部分支（含已合并的）。"""
    db.materialize_branches(review_id)
    return db.list_branches(review_id)


def numbered(review_id: str) -> list[dict]:
    """带序号的分支列表。序号按显示顺序一次性发下去，和主干里的 #N 对得上。"""
    rows = ensure(review_id)
    for i, b in enumerate(rows, 1):
        b["no"] = i
    return rows


def manual(review_id: str, title: str, question: str = "") -> str:
    """手工开一支：找不到对应待办时，用户想就整体方向单独问一嘴。

    `kind='free'`、`finding_id` 留空 —— 合并时不会去改任何条目状态。
    """
    title = (title or "").strip()[:40] or "我想问的事"
    return db.create_branch(
        review_id, kind="free", title=title, question=(question or title).strip()[:200]
    )


# ---------------------------------------------------------------- 上下文组装


def _card(review: dict) -> dict:
    d = db.get_delegation(review["delegation_id"]) or {}
    criteria = [str(c.get("text") or "") for c in d.get("success_criteria") or []]
    return {
        "goal": (d.get("goal") or "")[:200],
        "deliverable": (d.get("deliverable") or "")[:200],
        "criteria": [c[:120] for c in criteria],
        "forbidden": (d.get("forbidden") or "")[:200],
        "boundaries": (d.get("boundaries") or "")[:160],
    }


def _segments(text: str) -> list[tuple[int, int, str]]:
    """按空行切段，保留字符偏移（锚点要按偏移对回去）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"\n[ \t]*\n", text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))

    out = []
    for s, e in spans:
        chunk = text[s:e]
        if len(chunk.strip()) < 2:
            continue
        head = re.split(r"[。！？\n]", chunk.strip(), 1)[0]
        out.append((s, e, head))
    return out


def _anchor_window(text: str, branch: dict) -> dict:
    """这一条对应的原文窗口。没锚上就退回关键词找一次。"""
    cs, ce = branch.get("char_start"), branch.get("char_end")
    if cs is None and branch.get("quote"):
        from . import anchoring

        found = anchoring.locate(branch["quote"], text)
        if found.ok:
            cs, ce = found.char_start, found.char_end
    if cs is None:
        return {"start": None, "end": None, "text": "", "how": "none"}

    cs, ce = int(cs), int(ce)
    left = max(0, cs - ANCHOR_CTX)
    right = min(len(text), ce + ANCHOR_CTX)
    body = text[left:right]
    if len(body) > ANCHOR_CAP:
        body = body[:ANCHOR_CAP]
        right = left + ANCHOR_CAP
    return {
        "start": left,
        "end": right,
        "hit": [cs, ce],
        "text": body,
        "how": "quote",
    }


def _skeleton(text: str, span: list[int] | None) -> list[dict]:
    """全文骨架：段号 + 首句。这是"这件事在全文哪儿"的地图，不是内容本身。"""
    segs = _segments(text)
    head_len = SKELETON_HEAD if len(segs) <= SKELETON_SEGS else 20
    keep = segs if len(segs) <= SKELETON_SEGS else _thin(segs, span, SKELETON_SEGS)
    out = []
    for i, (s, e, head) in enumerate(keep, 1):
        mark = ""
        if span and span[0] is not None and not (e < span[0] or s > span[1]):
            mark = "★"      # ★ = 锚点所在段
        out.append({"no": i, "chars": e - s, "head": head[:head_len], "mark": mark})
    return out


def _thin(segs: list, span: list[int] | None, limit: int) -> list:
    """>30 段时保留：锚点所在段 + 每段等距抽样，让模型对"全文有多长"有概念。"""
    picked: list[tuple[int, int, str]] = []
    if span and span[0] is not None:
        for seg in segs:
            if not (seg[1] < span[0] or seg[0] > span[1]):
                picked.append(seg)
                break
    step = max(1, len(segs) // max(1, limit - len(picked)))
    for i in range(0, len(segs), step):
        if segs[i] not in picked:
            picked.append(segs[i])
        if len(picked) >= limit:
            break
    return picked[:limit]


def _history(branch_id: str) -> list[dict]:
    rows = db.list_branch_messages(branch_id, limit=HISTORY_TURNS)
    return [
        {"role": m["role"], "content": (m["content"] or "")[:HISTORY_CHARS]}
        for m in rows
        if m["kind"] not in ("failure",)
    ]


def _options(branch: dict) -> list[str]:
    """决策分支必须把选项原样给模型，否则它没法逐字命中，只能自己编一个。"""
    if not branch.get("finding_id"):
        return []
    f = db.get_finding(branch["finding_id"])
    if not f or f["kind"] != "decision":
        return []
    return [o["label"] for o in f["options"]]


def context_for(branch: dict, review: dict, extra: list[dict] | None = None) -> dict:
    """一次请求要给模型看的东西，以及"这次看了多少、省了多少"的账。

    `extra` 是用户在页面上手动勾进来的原文片段（`find_in_source` 的结果）。
    这是本模块唯一的"上下文可扩张"入口 —— 由用户给，不由模型要。
    """
    result = db.get_result(review["result_id"]) or {}
    text = result.get("raw_output") or ""
    card = _card(review)

    card_chars = len(card["goal"]) + len(card["deliverable"]) + len(card["forbidden"])
    card_chars += sum(len(c) for c in card["criteria"])
    if card_chars > CARD_CAP:  # 极少数委托卡写爆了，先砍边界描述，目标不动
        card["boundaries"] = ""
        card["forbidden"] = card["forbidden"][:80]

    anchor = _anchor_window(text, branch)
    span = [anchor["start"], anchor["end"]] if anchor["start"] is not None else None
    skeleton = _skeleton(text, span)
    history = _history(branch["id"])

    picked = [
        {"start": e.get("start"), "end": e.get("end"), "text": (e.get("text") or "")[:1200]}
        for e in (extra or [])
    ]

    stats = {
        "card": card_chars,
        "anchor": len(anchor["text"]),
        "extra": sum(len(p["text"]) for p in picked),
        "skeleton": sum(len(s["head"]) + 12 for s in skeleton),
        "history": sum(len(h["content"]) for h in history),
        "source": len(text),
    }
    stats["total"] = sum(stats[k] for k in ("card", "anchor", "extra", "skeleton", "history"))
    stats["omitted"] = max(0, len(text) - stats["anchor"] - stats["extra"])

    # 超预算：远历史 → 骨架段数 → 锚点窗口。委托卡不动。
    while stats["total"] > TOTAL_BUDGET and len(history) > 1:
        stats["history"] -= len(history.pop(0)["content"])
        stats["total"] = sum(stats[k] for k in ("card", "anchor", "extra", "skeleton", "history"))
    while stats["total"] > TOTAL_BUDGET and len(skeleton) > 8:
        stats["skeleton"] -= len(skeleton.pop()["head"]) + 12
        stats["total"] = sum(stats[k] for k in ("card", "anchor", "extra", "skeleton", "history"))
    if stats["total"] > TOTAL_BUDGET and anchor["text"]:
        cut = min(len(anchor["text"]) - 400, stats["total"] - TOTAL_BUDGET)
        if cut > 0:
            anchor["text"] = anchor["text"][: len(anchor["text"]) - cut]
            stats["anchor"] -= cut
            stats["total"] = sum(stats[k] for k in ("card", "anchor", "extra", "skeleton", "history"))

    return {
        "card": card,
        "anchor": anchor,
        "extra": picked,
        "skeleton": skeleton,
        "history": history,
        "stats": stats,
        "budget": TOTAL_BUDGET,
    }


def receipt(ctx: dict) -> str:
    """给用户看的那张账。不是装饰：产品说"可追溯"，就得能查到模型看见/没看见什么。"""
    s = ctx["stats"]
    a = ctx["anchor"]
    where = f"原文第 {a['start']}–{a['end']} 字" if a["start"] is not None else "未锚上原文"
    extra = f" · 你追加 {len(ctx['extra'])} 段 {s['extra']} 字" if ctx["extra"] else ""
    return (
        f"委托卡 {s['card']} 字 · 锚点 {s['anchor']} 字（{where}）{extra} · "
        f"骨架 {len(ctx['skeleton'])} 段 {s['skeleton']} 字 · 历史 {len(ctx['history'])} 轮 {s['history']} 字\n"
        f"合计 {s['total']} 字 ≈ {round(s['total'] / 1000, 1)}k tokens ｜ "
        f"已省略全文其余 {s['omitted']} 字"
    )


# ---------------------------------------------------------------- 一轮对话


def respond(branch_id: str, user_text: str, extra: list[dict] | None = None) -> dict:
    b = db.get_branch(branch_id)
    if not b:
        raise ValueError("这一支不存在，可能已被清空。")
    if b["state"] != "open":
        raise ValueError("这一支已经合并或放弃了，不能再聊。要重开就回主干再开一支。")
    review = db.get_review(b["review_id"])
    if not review:
        raise ValueError("这次评审不存在。")

    text = (user_text or "").strip()
    if not text:
        raise ValueError("说点什么再发。")
    sid = review["session_id"]
    db.add_branch_message(branch_id, review["id"], sid, text, role="user", kind="chat")

    ctx = context_for(b, review, extra)
    payload = {
        "分支问题": b["question"] or b["title"],
        "委托卡（边界，不许越界）": ctx["card"],
        "原文锚点（判断只能基于这段）": ctx["anchor"]["text"],
        "你追加的原文片段（用户手动带进来的）": [p["text"] for p in ctx["extra"]],
        "全文骨架（只用来定位，不许据此推断细节）": ctx["skeleton"],
        "可选项（决策只能从这里选）": _options(b),
        "这一支的对话": ctx["history"],
        "用户刚说的话": text,
    }

    try:
        raw = llm.call(
            "branch_reply",
            payload,
            prompts.BRANCH_REPLY_SYSTEM,
            prompts.branch_reply_user(payload),
            temperature=0.2,
            max_tokens=700,
        )
    except Exception as exc:  # noqa: BLE001 模型挂了也不能让这句话凭空消失
        note = f"这句话我存下了，但这次没能理解（{type(exc).__name__}）。等下再说一遍。"
        db.add_branch_message(branch_id, review["id"], sid, note, role="assistant", kind="failure")
        return {"say": note, "applied": [], "failed": True, "context": ctx}

    say = str(raw.get("say") or "").strip() or "我记下了。"
    applied = _apply(b, review, raw.get("intents") or [], text)
    refs = [{"ref": "say", "text": say}]
    for item in applied:
        refs.append({"ref": item["ref"], "text": item["line"]})
    db.add_branch_message(
        branch_id,
        review["id"],
        sid,
        "\n\n".join(r["text"] for r in refs),
        role="assistant",
        kind="chat",
        refs=refs,
    )
    return {"say": say, "applied": applied, "failed": False, "context": ctx}


def _apply(branch: dict, review: dict, intents: list, user_text: str) -> list[dict]:
    """能改什么，由这里说了算。模型只能"提议"。"""
    applied: list[dict] = []
    fid = branch.get("finding_id")
    f = db.get_finding(fid) if fid else None

    for intent in intents[:2]:
        if not isinstance(intent, dict):
            continue
        typ = str(intent.get("type") or "").strip()

        if typ == "decide" and f and f["kind"] == "decision" and f["state"] == "open":
            labels = [o["label"] for o in f["options"]]
            pick = str(intent.get("option_label") or "").strip()
            if pick not in labels:
                continue  # 选项必须逐字对上，不让模型自己编一个
            db.update_finding(f["id"], state="decided", chosen=pick)
            applied.append(
                {
                    "ref": "decide",
                    "line": f"记下了：这条决策选「{pick}」。点下面「标记完成并合并」把它写回主干。",
                }
            )

        elif typ == "conclude":
            # 结论必须用户自己说到那几个词，模型说了不算
            if not any(w in user_text for w in _CONCLUDE_WORDS):
                continue
            res = str(intent.get("resolution") or "").strip()[:120] or user_text.strip()[:120]
            if len(res) < 2:
                continue
            db.update_branch(branch["id"], resolution=res)
            applied.append(
                {
                    "ref": "conclude",
                    "line": f"结论记成：「{res}」。确认无误就点「标记完成并合并到主干」。",
                }
            )

    return applied


# ---------------------------------------------------------------- 合并 / 放弃


def merge(branch_id: str, resolution: str = "") -> dict:
    """把这一支的结论写回主干。合并后不可逆 —— 只留一个真相源。

    条目状态的写法是刻意区分的：
    - decision：状态保持 decided，**不覆盖**用户选过的选项（结论只是把它写回主干）
    - action_item：新状态 merged，chosen 存结论（不能复用 followed，那是"已开新会话"）
    - free（手工分支）：不动任何条目，只留主干那行字
    """
    b = db.get_branch(branch_id)
    if not b:
        raise ValueError("这一支不存在。")
    if b["state"] == "merged":
        return {"already": True, "resolution": b["resolution"]}

    res = (resolution or b["resolution"] or "").strip()
    if len(res) < 2:
        raise ValueError("先写一句结论再合并 —— 主干要留的是结论，不是「聊过了」三个字。")

    review = db.get_review(b["review_id"])
    if not review:
        raise ValueError("这次评审不存在。")

    f = db.get_finding(b["finding_id"]) if b["finding_id"] else None
    if f:
        if f["kind"] == "decision":
            if f["state"] != "decided":
                db.update_finding(f["id"], state="decided", chosen=res[:30])
        else:
            db.update_finding(f["id"], state="merged", chosen=res[:60])

    db.update_branch(branch_id, state="merged", resolution=res[:120], merged_at=db.now())
    db.add_review_message(
        b["review_id"],
        review["session_id"],
        f"分支「{b['title']}」已合并：{res}",
        kind="note",
    )
    return {"already": False, "resolution": res}


def merged_notes(review_id: str) -> list[dict]:
    return [b for b in db.list_branches(review_id) if b["state"] == "merged" and b["resolution"]]


# ---------------------------------------------------------------- 按需取全文


def find_in_source(branch: dict, review: dict, query: str, limit: int = 3) -> list[dict]:
    """在 Agent 全文里找用户问的那句，返回命中最密的窗口。

    这不是给模型加上下文，是**先给用户看**。用户确认要了，才进下一轮 ②层。
    """
    text = (db.get_result(review["result_id"]) or {}).get("raw_output") or ""
    terms = _terms(query)
    if not text or not terms:
        return []

    buckets: dict[int, dict] = {}
    for t in terms:
        for m in re.finditer(re.escape(t), text):
            key = m.start() // 400
            slot = buckets.setdefault(key, {"hits": 0, "terms": set(), "start": m.start()})
            slot["hits"] += 1
            slot["terms"].add(t)

    ranked = sorted(buckets.values(), key=lambda s: (-s["hits"], s["start"]))[:limit]
    out = []
    for slot in ranked:
        s = max(0, slot["start"] - 200)
        e = min(len(text), slot["start"] + 400)
        out.append(
            {
                "start": s,
                "end": e,
                "text": text[s:e],
                "terms": sorted(slot["terms"]),
            }
        )
    return out


def _terms(query: str) -> list[str]:
    """把一句话切成能落地检索的词。中文不切词，用"整句包含"兜底。"""
    query = (query or "").strip()
    if not query:
        return []
    parts = [p.strip() for p in re.split(r"[，。、；：！？\s「」“”（）()]+", query) if len(p.strip()) >= 2]
    if not parts:
        parts = [query[:12]]
    return list(dict.fromkeys(parts))[:6]
