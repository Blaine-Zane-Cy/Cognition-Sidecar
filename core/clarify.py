"""澄清引擎：槽位驱动，不靠模型自觉。

硬槽位：goal / deliverable / success_criteria(≥3 且每条有 how_to_verify) / boundaries / forbidden。
每个槽位都可以被用户“显式跳过”，但跳过会被记录并降级结果卡的确定性（需求改动 ②）。
"""
from __future__ import annotations

from .models import HARD_SLOTS, MIN_CRITERIA, QUESTIONS, SLOT_HINTS, SLOT_LABELS


def missing(slots: dict, skipped: list) -> list[str]:
    skip = {s.get("slot") for s in skipped}
    out: list[str] = []
    for slot in HARD_SLOTS:
        if slot in skip:
            continue
        value = slots.get(slot)
        if slot == "success_criteria":
            if not isinstance(value, list) or len(value) < MIN_CRITERIA:
                out.append(slot)
            elif any(not str(c.get("how_to_verify") or "").strip() for c in value):
                out.append(slot)
        else:
            if not str(value or "").strip():
                out.append(slot)
    return out


def merge(slots: dict, updates: dict) -> dict:
    """把模型给的 slot_updates 合进槽位。只接受已知槽位，并做类型收敛。"""
    out = dict(slots)
    for key, value in (updates or {}).items():
        if key not in HARD_SLOTS or value is None:
            continue
        if key == "success_criteria":
            items = []
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str):
                    text, how = item.strip(), ""
                elif isinstance(item, dict):
                    text = str(item.get("text") or "").strip()
                    how = str(item.get("how_to_verify") or "").strip()
                else:
                    continue
                if text:
                    items.append({"text": text, "how_to_verify": how})
            if items:
                out[key] = items[:6]
        elif isinstance(value, list):
            joined = "；".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                out[key] = joined
        else:
            text = str(value).strip()
            if text:
                out[key] = text
    return out


def is_ready(slots: dict, skipped: list) -> bool:
    return not missing(slots, skipped)


def question_for(slot: str) -> str:
    return QUESTIONS.get(slot, f"关于「{SLOT_LABELS.get(slot, slot)}」，能再具体一点吗？")


def slot_panel(slots: dict, skipped: list) -> list[dict]:
    """给 UI 的槽位看板数据。"""
    skip_map = {s.get("slot"): s.get("reason", "") for s in skipped}
    rows = []
    for slot in HARD_SLOTS:
        value = slots.get(slot)
        if slot == "success_criteria" and isinstance(value, list):
            shown = f"{len(value)} 条：" + "；".join(c["text"] for c in value)
        else:
            shown = str(value or "")
        rows.append(
            {
                "slot": slot,
                "label": SLOT_LABELS[slot],
                "hint": SLOT_HINTS[slot],
                "value": shown,
                "filled": bool(shown),
                "skipped": slot in skip_map,
                "skip_reason": skip_map.get(slot, ""),
            }
        )
    return rows


def summarize_skip(skipped: list) -> str:
    if not skipped:
        return ""
    parts = []
    for s in skipped:
        label = SLOT_LABELS.get(s.get("slot"), s.get("slot"))
        reason = s.get("reason") or "用户选择跳过"
        parts.append(f"{label}（{reason}）")
    return "；".join(parts)
