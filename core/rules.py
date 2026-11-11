"""偏好规则：匹配与沉淀。

匹配只做 scope + 关键词打分取 top10（需求改动 ③：不做时间/命中次数加权，
避免“最近用过的规则更可能被引用”这种自我强化）。
"""
from __future__ import annotations

import re

from . import db, llm, prompts

REVIEW_SCOPES = ("review", "both")
SCOPE_LABELS = {"delegate": "委托阶段", "review": "评审阶段", "both": "全程"}
MAX_CANDIDATES = 10


def candidates(delegation: dict, agent_output: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    """打分 = 命中的关键词个数；只保留命中 > 0。"""
    rows = [r for r in db.list_rules(status="active") if r["scope"] in REVIEW_SCOPES]
    haystack = (delegation.get("goal") or "") + "\n" + (agent_output or "")
    scored = []
    for rule in rows:
        keywords = [k for k in rule.get("keywords", []) if k]
        hits = [k for k in keywords if k in haystack]
        if hits:
            item = dict(rule)
            item["score"] = len(hits)
            item["matched_keywords"] = hits
            scored.append(item)
    scored.sort(key=lambda r: (-r["score"], r["id"]))
    return scored[:limit]


def candidate_brief(candidates_list: list[dict]) -> list[dict]:
    """喂给模型的精简版，只给 id / 文本 / 命中的词。"""
    return [
        {
            "id": c["id"],
            "rule": c["text"],
            "matched_keywords": c.get("matched_keywords", []),
        }
        for c in candidates_list
    ]


def create_from_reason(reason: str, review: dict, doubts: list) -> dict:
    """驳回理由 → 一条规则草稿。模型只做转述，不做判断。"""
    payload = {
        "reason": reason,
        "headline": review.get("headline", ""),
        "doubts": [d.get("title", "") for d in doubts],
    }
    try:
        raw = llm.call(
            "rule_extract",
            payload,
            prompts.RULE_EXTRACT_SYSTEM,
            prompts.rule_extract_user(payload),
            temperature=0.1,
        )
    except llm.LLMError:
        raw = {}

    text = str(raw.get("text") or reason).strip()
    if len(text) > 80:
        text = text[:80]
    keywords = raw.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        keywords = derive_keywords(reason)
    keywords = [str(k).strip() for k in keywords if str(k).strip()][:6]
    scope = raw.get("scope") if raw.get("scope") in SCOPE_LABELS else "review"
    return {
        "text": text,
        "rationale": str(raw.get("rationale") or f"来自一次驳回：{reason[:60]}").strip(),
        "scope": scope,
        "keywords": keywords,
    }


_VOCAB = [
    "移动端", "手机", "响应式", "适配", "真机", "平板",
    "数据来源", "出处", "编造", "引用", "数字",
    "测试", "验证", "截图", "无障碍", "加载速度",
    "配色", "字体", "排版", "文案", "版权", "隐私",
]


def derive_keywords(text: str) -> list[str]:
    hits = [w for w in _VOCAB if w in text]
    if hits:
        return hits[:6]
    chunks = re.findall(r"[\u4e00-\u9fa5]{2,4}", text)
    return chunks[:4] or ["交付质量"]


def save_from_reject(review_id: str, draft: dict) -> str:
    return db.create_rule(
        text=draft["text"],
        rationale=draft["rationale"],
        scope=draft["scope"],
        keywords=draft["keywords"],
        source="reject",
        source_review_id=review_id,
        status="active",
    )


def save_manual(text: str, rationale: str, scope: str, keywords: list) -> str:
    return db.create_rule(
        text=text,
        rationale=rationale or "用户手动添加",
        scope=scope,
        keywords=keywords,
        source="manual",
        status="active",
    )


def parse_keywords(raw: str) -> list[str]:
    parts = re.split(r"[,，、;；\s]+", raw or "")
    return [p.strip() for p in parts if p.strip()][:10]


def describe(rule: dict) -> str:
    return f"{rule['id']}（{SCOPE_LABELS.get(rule['scope'], rule['scope'])}）{rule['text']}"


def dump_keywords(keywords: list) -> str:
    return "、".join(keywords or [])
