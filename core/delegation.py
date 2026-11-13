"""委托卡：生成、校验、渲染成可复制的文本。"""
from __future__ import annotations

from . import llm, prompts, settings
from .models import SLOT_LABELS

REQUIRED = ("goal", "deliverable", "success_criteria")


def generate(slots: dict, skipped: list, brief: str = "") -> dict:
    payload = {"slots": slots, "skipped": skipped, "brief": brief}
    raw = llm.call(
        "delegation",
        payload,
        prompts.DELEGATION_SYSTEM,
        prompts.delegation_user(payload),
        temperature=0.2,
    )
    card = normalize(raw, slots, brief)
    issues = validate(card, skipped)
    if issues:
        card["issues"] = issues
    return card


def from_slots(slots: dict, brief: str = "") -> dict:
    """不经过模型，直接用槽位拼一张卡（模型失败时的降级路径）。"""
    return normalize({}, slots, brief)


def normalize(raw: dict, slots: dict, brief: str = "") -> dict:
    goal = str(raw.get("goal") or slots.get("goal") or "").strip()
    deliverable = str(raw.get("deliverable") or slots.get("deliverable") or "").strip()
    boundaries = str(raw.get("boundaries") or slots.get("boundaries") or "").strip()
    forbidden = str(raw.get("forbidden") or slots.get("forbidden") or "").strip()

    criteria = raw.get("success_criteria")
    if not isinstance(criteria, list) or not criteria:
        criteria = slots.get("success_criteria") or []
    clean = []
    for item in criteria:
        if isinstance(item, str):
            text, how = item.strip(), ""
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            how = str(item.get("how_to_verify") or "").strip()
        else:
            continue
        if text:
            clean.append({"text": text, "how_to_verify": how})

    keywords = raw.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        keywords = _derive_keywords(goal, clean)
    keywords = [str(k).strip() for k in keywords if str(k).strip()][:10]

    return {
        "goal": goal,
        "deliverable": deliverable,
        "boundaries": boundaries,
        "forbidden": forbidden or "无",
        "success_criteria": clean,
        "keywords": keywords,
        "brief": (brief or "").strip(),
        "issues": [],
    }


def _derive_keywords(goal: str, criteria: list) -> list:
    text = goal + " " + " ".join(c["text"] for c in criteria)
    words = [w for w in ("移动端", "手机", "适配", "测试", "数据来源", "截图", "配色", "文案", "排版") if w in text]
    return words[:8] or ["完成度", "验证"]


def validate(card: dict, skipped: list) -> list[str]:
    """硬校验：不合格的委托卡不允许交给工作 Agent。"""
    skip = {s.get("slot") for s in skipped}
    issues: list[str] = []
    for key in REQUIRED:
        if key in skip:
            continue
        if key == "success_criteria":
            items = card.get("success_criteria") or []
            if len(items) < 3:
                issues.append(f"成功标准只有 {len(items)} 条，至少需要 3 条")
            for item in items:
                if not item.get("how_to_verify"):
                    issues.append(f"成功标准「{item['text'][:16]}」缺少验证方式")
        elif not str(card.get(key) or "").strip():
            issues.append(f"{SLOT_LABELS.get(key, key)}为空")
    return issues


def render_text(card: dict) -> str:
    """只输出要发给工作 Agent 的内容，一个字都不多。

    两样东西刻意不在这里：
    - 「完成后贴回来」是提醒**用户**的话，Agent 看了只会当成任务的一部分；
    - keywords 是侧车内部用来在原文里定位段落的检索词（core/review.py 要用），
      不是委托内容，发给 Agent 纯属噪音。
    数据模型里仍保留 keywords，编辑表单也还在，只是不进这份可复制文本。
    """
    lines = [f"【委托卡 v{card.get('version', 1)}】", ""]
    brief = (card.get("brief") or "").strip()
    if brief:
        lines.append("—— 原始构想（你的需求背景，务必先读这一段）——")
        lines.append(brief)
        lines.append("")
    lines.append(f"目标：{card.get('goal', '')}")
    lines.append(f"交付物：{card.get('deliverable', '')}")
    lines.append(f"边界：{card.get('boundaries', '') or '（未限定）'}")
    lines.append(f"禁止项：{card.get('forbidden', '') or '无'}")
    lines.append("")
    lines.append("成功标准：")
    for i, item in enumerate(card.get("success_criteria") or [], 1):
        how = item.get("how_to_verify") or "（未给验证方式）"
        lines.append(f"{i}. {item['text']}")
        lines.append(f"   验证：{how}")
    return "\n".join(lines).rstrip()


def to_payload(card: dict) -> dict:
    return {
        "goal": card.get("goal", ""),
        "deliverable": card.get("deliverable", ""),
        "boundaries": card.get("boundaries", ""),
        "forbidden": card.get("forbidden", ""),
        "success_criteria": card.get("success_criteria", []),
        "keywords": card.get("keywords", []),
        "brief": card.get("brief", ""),
    }


def save(session_id: str, card: dict) -> str:
    from . import db

    return db.create_delegation(
        session_id,
        goal=card.get("goal", ""),
        deliverable=card.get("deliverable", ""),
        boundaries=card.get("boundaries", ""),
        forbidden=card.get("forbidden", "无"),
        success_criteria=card.get("success_criteria", []),
        keywords=card.get("keywords", []),
        brief=card.get("brief", ""),
    )


def max_chars() -> int:
    return settings.get_int("max_output_chars", 30000)
