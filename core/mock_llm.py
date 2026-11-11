"""演示模式的替身模型。

它不是“假数据”——锚点校验仍然作用在真实的 Agent 输出上，
所以演示模式也能证明追溯机制是真的。
"""
from __future__ import annotations

import re

from . import clarify
from .models import HARD_SLOTS, QUESTIONS

# 一条“看起来很像原文、其实原文里没有”的结论，用来演示锚定失败
_FABRICATED = [
    "所有页面均已在 iPhone 与安卓真机上逐项验证通过。",
    "视觉稿经过两轮用户测试，主要问题已全部修复。",
    "页面加载速度经实测稳定在 1.2 秒以内。",
]


def handle(task: str, payload: dict) -> dict:
    table = {
        "clarify": _clarify,
        "delegation": _delegation,
        "review": _review,
        "rule_extract": _rule_extract,
        "review_brief": _review_brief,
        "review_reply": _review_reply,
        "branch_reply": _branch_reply,
        "free_chat": _free_chat,
    }
    if task not in table:
        raise ValueError(f"演示模式不支持任务：{task}")
    return table[task](payload)


# ---------------------------------------------------------------- 澄清


def _clarify(payload: dict) -> dict:
    slots = dict(payload.get("slots") or {})
    skipped = payload.get("skipped") or []
    missing = payload.get("missing") or []
    user_text = (payload.get("user_text") or "").strip()

    updates: dict = {}
    if user_text and missing:
        slot = missing[0]
        if slot == "success_criteria":
            items = _split_criteria(user_text)
            if items:
                updates[slot] = items
        elif slot == "forbidden" and user_text in ("无", "没有", "暂无", "none"):
            updates[slot] = "无"
        else:
            updates[slot] = user_text

    merged = clarify.merge(slots, updates)
    left = clarify.missing(merged, skipped)
    return {
        "slot_updates": updates,
        "next_question": QUESTIONS[left[0]] if left else "",
    }


def _split_criteria(text: str) -> list[dict]:
    parts = re.split(r"[；;。\n]+", text)
    items = []
    for part in parts:
        part = part.strip(" ，,")
        if len(part) < 4:
            continue
        inner = re.findall(r"[（(]([^）)]{4,})[）)]", part)
        how = inner[0] if inner else f"打开产物逐项核对：{part}"
        items.append({"text": part, "how_to_verify": how})
    return items[:5]


# ---------------------------------------------------------------- 委托卡


def _delegation(payload: dict) -> dict:
    slots = payload.get("slots") or {}
    criteria = slots.get("success_criteria") or []
    goal = str(slots.get("goal") or "").strip()
    keywords = []
    blob = goal + " ".join(c.get("text", "") for c in criteria)
    for word in ("移动端", "手机", "适配", "测试", "数据来源", "截图", "配色", "文案", "排版", "研究方向"):
        if word in blob:
            keywords.append(word)
    return {
        "goal": goal,
        "deliverable": slots.get("deliverable", ""),
        "boundaries": slots.get("boundaries", ""),
        "forbidden": slots.get("forbidden") or "无",
        "success_criteria": criteria,
        "keywords": keywords[:8] or ["完成度", "验证"],
    }


# ---------------------------------------------------------------- 结果卡


def _review(payload: dict) -> dict:
    text = payload.get("agent_output") or ""
    cands = payload.get("candidate_rules") or []
    criteria = (payload.get("delegation") or {}).get("success_criteria") or []
    sents = _sentences(text)

    sub_goals = []
    passed = 0
    for i, crit in enumerate(criteria):
        ctext = str(crit.get("text") or "").split("（")[0]
        sent, score = _best_sentence(ctext, sents)
        verified = sent and any(m in sent for m in _MARKERS)
        if verified:
            verdict, note = "pass", "有对应段落，也能看到验证痕迹"
            passed += 1
        elif sent:
            verdict, note = "partial", "有对应段落，但没有验证过程"
        else:
            verdict, note = "fail", "输出里找不到对应内容"
        sub_goals.append(
            {
                "criterion": i,
                "verdict": verdict,
                "note": note,
                "evidence": sent[:40],
            }
        )

    fabricated = _pick_unseen(text)
    findings = [
        {
            "kind": "doubt",
            "title": "结构说清了，取舍理由缺失",
            "detail": "分段和导航的结论都在，但为什么这样分没有依据，无法判断结构是不是最优。",
            "quote": _quote(sents, 0),
            "rule_id": None,
            "severity": "medium",
        },
        {
            "kind": "doubt",
            "title": "验证结论没有原文支撑",
            "detail": "这条结论在原文里找不到对应片段，属于无法核对的断言。",
            "quote": fabricated,
            "rule_id": None,
            "severity": "high",
        },
        {
            "kind": "risk",
            "title": "自述完成度高于实际交付",
            "detail": "最后一段用了整体性描述，但没有逐条对照成功标准，容易让人误判完成度。",
            "quote": _quote(sents, len(sents) - 1) if sents else "",
            "rule_id": None,
            "severity": "medium",
        },
    ]
    findings = [f for f in findings if f["quote"]]

    # 待办：两条能锚回原文，一条故意锚不上（演示"未锚定建议"的处理）
    action_items = [
        {
            "title": "补做移动端排版与真机测试",
            "detail": "原文只说做了宽度自适应，排版没专门调过。",
            "quote": _find(sents, "移动端目前只做了宽度自适应"),
            "rule_id": None,
            "severity": "high",
        },
        {
            "title": "补一张版式视觉稿",
            "detail": "原文自己承认没做视觉稿，版式只能靠肉眼在浏览器里看。",
            "quote": _find(sents, "没有做视觉稿"),
            "rule_id": None,
            "severity": "medium",
        },
        {
            "title": "补一份内容维护说明",
            "detail": "以后怎么改内容没交代（演示：故意给不出原文，展示未锚定怎么处理）。",
            "quote": "说明文档里已经写清了以后怎么改内容。",
            "rule_id": None,
            "severity": "medium",
        },
    ]
    action_items = [a for a in action_items if a["quote"]]

    decisions = [
        {
            "title": "先修移动端还是先补部署步骤",
            "why": "两件事互斥，先做哪件决定下一轮的验收口",
            "options": [
                {"label": "先修移动端排版", "cost": "部署步骤继续悬着，还是没法给别人用"},
                {"label": "先补部署步骤", "cost": "手机上打开仍是乱的，演示当场扣分"},
            ],
            "recommended": "先修移动端排版",
            "quote": _find(sents, "移动端目前只做了宽度自适应"),
        }
    ]

    rule_checks = []
    for top in cands[:4]:  # 真模型会逐条比对前 10 条候选；替身只按关键词命中取前四条
        kws = top.get("matched_keywords") or []
        rule_checks.append(
            {
                "rule_id": top["id"],
                "verdict": "satisfied",
                "reason": f"命中关键词「{'、'.join(kws)}」，按该规则记为满足（演示模式只做关键词命中，真实性需你确认）。",
            }
        )

    total = len(criteria) or 1
    rate = passed / total
    return {
        "headline": f"{passed}/{total} 条成功标准有验证痕迹，另有 1 处结论在原文里找不到依据。",
        "sub_goals": sub_goals,
        "certainty": "high" if rate >= 0.8 else "medium",
        "findings": findings,
        "action_items": action_items,
        "decisions": decisions,
        "rule_checks": rule_checks,
        "deviation": "minor" if rate >= 0.6 else "major",
        "deviation_note": "范围基本对准，但验证与取舍依据缺位，照着往下走会攒下未验证的假设。",
        "next_options": [
            {"label": "让 Agent 补一版：逐条对照成功标准并给验证方式", "cost": "多一轮时间，输出会更长更难读"},
            {"label": "只让它补上移动端与数据来源两处", "cost": "范围小、快，但结构上的取舍问题仍留着"},
            {"label": "接受结构，自己在本地验证移动端", "cost": "省一轮对话，验证成本转嫁到你身上"},
            {"label": "改方向：先把网站目标收窄到一页", "cost": "当前进度作废，但目标更可核查"},
        ],
    }


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])|\n+", text)
    return [p.strip() for p in parts if p and len(p.strip()) >= 10]


_MARKERS = ("验证", "测了", "测试", "检查", "截图", "链接", "核对", "看过")


def _best_sentence(crit: str, sents: list[str]) -> tuple[str, float]:
    """用二元字重叠找最贴近这条标准的句子，避免用关键词硬套。"""
    grams = {crit[i : i + 2] for i in range(max(0, len(crit) - 1))}
    if not grams:
        return "", 0.0
    best, best_score = "", 0.0
    for sent in sents:
        sg = {sent[i : i + 2] for i in range(max(0, len(sent) - 1))}
        score = len(grams & sg) / len(grams)
        if score > best_score:
            best, best_score = sent, score
    if best_score < 0.2:
        return "", best_score
    return best, best_score


def _quote(sents: list[str], idx: int) -> str:
    if not sents:
        return ""
    s = sents[max(0, min(idx, len(sents) - 1))]
    return s[:42]


def _find(sents: list[str], needle: str) -> str:
    """找含这句话的原文片段；找不到返回空串（让锚定自然失败）。"""
    for s in sents:
        if needle in s:
            return s[:42]
    return ""


def _pick_unseen(text: str) -> str:
    for cand in _FABRICATED:
        if cand not in text:
            return cand
    return _FABRICATED[0]


# ---------------------------------------------------------------- 规则抽取

_VOCAB = [
    "移动端", "手机", "响应式", "适配", "真机", "平板",
    "数据来源", "出处", "编造", "引用", "数字",
    "测试", "验证", "截图", "无障碍", "加载速度",
    "配色", "字体", "排版", "文案", "版权", "隐私",
]


def _rule_extract(payload: dict) -> dict:
    reason = (payload.get("reason") or "").strip()
    keywords = [w for w in _VOCAB if w in reason][:6]
    if not keywords:
        keywords = re.findall(r"[\u4e00-\u9fa5]{2,4}", reason)[:4]
    # 只保留“要求”部分，去掉后半句的情绪化说明
    core = re.split(r"—{2}|这版|但|因为", reason)[0].strip(" ，。,.")
    text = core or reason
    if not any(word in text for word in ("必须", "不要", "别", "先", "把")):
        text = "交付结果必须满足：" + text
    return {
        "text": text[:60],
        "rationale": f"来自一次驳回：{reason[:50]}",
        "scope": "both",
        "keywords": keywords or ["交付质量"],
    }


# ---------------------------------------------------------------- 审阅对话

_OPENERS = {
    "verdict": "我仔细看了一遍。",
    "doubt": "有一处我没法替你确认：",
    "risk": "有个风险我觉得得先说：",
    "decision": "有件事得你自己定。",
    "action_item": "另外还有一步得你亲手做：",
}
_DISMISS_WORDS = ("不用管", "允许", "可以忽略", "没关系", "不管了", "放过", "这条接受")
_DECIDE_WORDS = ("选", "决定", "就要", "按方案", "用方案")
_RULE_WORDS = ("以后", "必须", "不要", "别", "每次", "一律")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def _review_brief(payload: dict) -> dict:
    """替身只做“加连接词”，事实一个字不加：它证明的是结构，不是文采。"""
    out = []
    for seg in payload.get("segments") or []:
        ref = str(seg.get("ref") or "")
        fact = str(seg.get("fact") or "")
        head = _OPENERS.get(ref.split(":")[0], "")
        out.append({"ref": ref, "text": (head + fact)[:160]})
    return {"paragraphs": out}


def _ordinal(text: str) -> int:
    m = re.search(r"第?\s*([一二三四五1-5])\s*[个条项处]", text)
    if not m:
        return 0
    token = m.group(1)
    return _CN_NUM.get(token, int(token) if token.isdigit() else 0)


def _kw(text: str) -> list[str]:
    """粗略切词：按标点断开后取前 2~3 字。

    比任意 2 字滑窗好——"结构说清了，取舍理由缺失" 会得到「结构」「取舍」，
    而不是「构说」「清了」这种永远匹配不上的碎片。
    """
    out = [w for w in _VOCAB if w in text]
    for part in re.split(r"[，,。；;、（）()：:\s]+", text):
        part = part.strip()
        if len(part) < 2:
            continue
        out.append(part[:2])
        if len(part) >= 4:
            out.append(part[:3])
    return list(dict.fromkeys(out))[:6] or ["交付质量"]


def _review_reply(payload: dict) -> dict:
    text = (payload.get("user_text") or "").strip()
    doubts = payload.get("doubts") or []
    decisions = payload.get("decisions") or []
    idx = _ordinal(text)
    intents: list[dict] = []

    # 1. 排除某条疑点 —— 优先级最高：句中常同时出现“接受”，别判成结案
    if any(w in text for w in _DISMISS_WORDS) and doubts:
        target = doubts[idx - 1] if 1 <= idx <= len(doubts) else doubts[0]
        intents.append(
            {
                "type": "dismiss",
                "finding_id": target["id"],
                "rule_text": f"「{target['title']}」这类问题可以接受，不必再提",
                "keywords": _kw(target["title"]),
                "scope": "review",
                "reason": "用户说这条可以不管",
            }
        )

    # 2. 对决策做选择
    elif any(w in text for w in _DECIDE_WORDS) and decisions:
        dec = decisions[idx - 1] if 1 <= idx <= len(decisions) else decisions[0]
        options = dec.get("options") or []
        pick = next((o for o in options if o and o in text), options[0] if options else "")
        if pick:
            intents.append({"type": "decide", "finding_id": dec["id"], "option_label": pick})

    # 3. 一条新的通用偏好
    elif any(w in text for w in _RULE_WORDS):
        intents.append(
            {
                "type": "rule",
                "rule_text": text[:50],
                "keywords": _kw(text),
                "scope": "both",
            }
        )

    # 4. 结案类动作（替身只识别词，具体执行由服务端再核一遍）
    for typ, words in (
        ("accept", ("接受", "可以收", "收下", "就这样")),
        ("reject", ("驳回", "不收", "重做", "打回")),
        ("redirect", ("改方向", "换个方向", "方向不对")),
    ):
        if any(w in text for w in words):
            intents.append({"type": typ})
            break

    if intents:
        first = intents[0]
        if first["type"] == "dismiss":
            say = "明白了，这条我划掉，不再当疑点。"
        elif first["type"] == "decide":
            say = f"记下了，就按「{first['option_label']}」走。"
        elif first["type"] == "rule":
            say = "这条我记住了，以后先按它看。"
        elif first["type"] == "accept":
            say = "那这次就收了。"
        elif first["type"] == "reject":
            say = "行，打回，理由我记下了。"
        else:
            say = "方向换了，那就重来一版委托。"
    else:
        say = "这句我先记着，不过没看出要动哪一条。"

    if doubts:
        suggest = "要不要我把已经定下的几条打包成一份补充说明，你直接发给 Agent 去改？"
    elif decisions:
        suggest = "还有一条决策没定，定了我就把补充说明写出来。"
    else:
        suggest = "没有悬着的事了，可以收，也可以接着看下一轮。"

    return {"say": say, "intents": intents[:3], "suggest": suggest}


def _branch_reply(payload: dict) -> dict:
    """分支对话替身：只认"选哪个"和"就这么办"，其余当讨论。

    刻意保持和真模型同一套意图协议 —— 这样 UI 在演示模式下走的也是真路径。
    """
    text = (payload.get("用户刚说的话") or "").strip()
    opts = payload.get("可选项（决策只能从这里选）") or []
    intents: list[dict] = []

    if opts and any(w in text for w in _DECIDE_WORDS):
        pick = next((o for o in opts if o and o in text), opts[0])
        intents.append({"type": "decide", "option_label": pick})
        say = f"记下了，就按「{pick}」。"
    elif any(w in text for w in ("就这么办", "就这么定", "我确认", "结论是", "定了", "就按这个")):
        intents.append({"type": "conclude", "resolution": text[:60]})
        say = "那我把这句记成结论，等你点合并。"
    else:
        say = "这一条我对着原文看了，你说的和那段对得上。还想确认哪一点？"

    return {
        "say": say,
        "intents": intents,
        "suggest": "聊完点下面那颗按钮，把结论并回主干。",
    }


def _free_chat(payload: dict) -> dict:
    """自由对话替身：演示模式下也要像在聊天，而不是甩一句固定文案。

    和真模型共用 FREE_CHAT_SYSTEM 的规矩：短、一次只推一步、不替用户拍板。
    """
    text = (payload.get("用户刚说的话") or "").strip()
    hist = payload.get("最近对话") or []
    rules = payload.get("用户偏好规则") or []
    turn = sum(1 for h in hist if h.get("role") == "user")

    if any(w in text for w in (
        "委托", "交给", "让 Agent", "让agent", "打算让", "去做这件事", "开工"
    )):
        say = (
            "想交给工作 Agent 去做的话，我建议先把「交付物长什么样」说死一句——"
            "这一步最容易含糊，后面审阅全靠它对齐。\n\n"
            "你打算让它交回来的是什么？一个文件、一个页面，还是一段可以直接用的文字？"
        )
    elif any(w in text for w in ("贴回", "结果", "交付", "返回", "审")):
        say = (
            "Agent 交回来的东西可以贴进工作台走审阅：侧车会逐条对回原文，"
            "把待办和待决策拆成独立分支，你一次只处理一条。\n\n"
            "这次它交回来的，你最不放心的是哪一处？"
        )
    elif turn <= 1:
        snippet = text[:32] + ("…" if len(text) > 32 else "")
        say = (
            f"先接住你这句「{snippet}」。\n\n"
            "我这边先把范围收一下：这件事最后要落到什么上，才算真的做完了？"
        )
    else:
        say = (
            "顺着刚才说的，我记下的重点是这一句：" + text[:36] + "。\n\n"
            "还有一处我没把握：这一步如果做砸了，最可能是卡在哪儿？"
        )

    if rules and turn <= 1:
        say += f"\n\n（我看到你已经沉淀了 {len(rules)} 条偏好规则，后面我会按这些来提醒你。）"
    return {"say": say}
