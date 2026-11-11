"""共享常量与轻量类型。

这个模块不依赖 streamlit、不依赖数据库，任何脚本都能安全导入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------- 槽位

HARD_SLOTS = ["goal", "deliverable", "success_criteria", "boundaries", "forbidden"]

MIN_CRITERIA = 3

SLOT_LABELS = {
    "goal": "目标",
    "deliverable": "交付物",
    "success_criteria": "成功标准",
    "boundaries": "边界",
    "forbidden": "禁止项",
}

SLOT_HINTS = {
    "goal": "这个产物为什么存在、给谁解决什么问题",
    "deliverable": "做完时你手里具体有什么",
    "success_criteria": "至少 3 条，每条都要能验证（怎么检查算过）",
    "boundaries": "做什么、不做什么，范围到哪儿为止",
    "forbidden": "你不想要的东西（可以明确写“无”）",
}

QUESTIONS = {
    "goal": "这个网站做完之后，最主要是给谁看？用一句话说清它为什么值得存在。",
    "deliverable": "做完时你手里具体拿到什么？是一个能打开的页面、还是源码加说明？",
    "success_criteria": "怎样算做完了？请给 3 条以上你能亲自检查的标准，用“；”隔开。",
    "boundaries": "这次做到哪一步就够，哪部分明确先不做？",
    "forbidden": "有没有你明确不想要的东西？没有就回“无”。",
}

# ---------------------------------------------------------------- 标尺

CERTAINTY_LABELS = {"high": "确定", "medium": "推测", "low": "存疑"}
CERTAINTY_ORDER = {"low": 0, "medium": 1, "high": 2}

DEVIATION_LABELS = {
    "none": "无偏差",
    "minor": "轻微偏差",
    "major": "明显偏差",
    "off_track": "跑偏",
}
DEVIATION_ORDER = {"none": 0, "minor": 1, "major": 2, "off_track": 3}

VERDICT_LABELS = {"pass": "达标", "partial": "部分达标", "fail": "未达标"}

STAGE_LABELS = {
    "brief": "构想",
    "clarifying": "澄清中",
    "delegation_ready": "委托卡就绪",
    "awaiting_result": "等待贴回结果",
    "reviewing": "评审中",
    "accepted": "已接受",
    "rejected": "已驳回",
    "redirected": "已改方向",
    "aborted": "已中止（未建成）",
}

# ---------------------------------------------------------------- 分支
# 结果卡上的条目类型。前三个是"这张卡说的事"，后两个是"你要做的事"。
FINDING_KINDS = ("sub_goal", "doubt", "risk", "action_item", "decision")
BRANCH_KINDS = ("action_item", "decision")

BRANCH_LABELS = {"action_item": "待办", "decision": "决策"}
BRANCH_MARKS = {"action_item": "☐", "decision": "⌥"}

FINDING_KIND_LABELS = {
    "sub_goal": "成功标准",
    "doubt": "疑点",
    "risk": "风险",
    "action_item": "待办",
    "decision": "决策",
}

BRANCH_STATE_LABELS = {
    "open": "待处理",
    "revisited": "已回溯",
    "decided": "已选",
    "dismissed": "已排除",
    "followed": "已开新会话",
    "merged": "已合并",
    "skipped": "已跳过",
}

ANCHOR_LABELS = {
    "exact": "逐字命中",
    "fuzzy": "近似匹配",
    "rule": "引用规则",
    "unanchored": "未锚定",
}

MESSAGE_KIND_LABELS = {
    "brief": "构想",
    "question": "提问",
    "chat": "回答",
    "note": "系统",
    "failure": "失败",
    "system": "系统",
}

REVIEW_STATUS_LABELS = {
    "pending": "待决定",
    "accepted": "已接受",
    "rejected": "已驳回",
    "redirected": "已改方向",
}


def label_certainty(level: str) -> str:
    return CERTAINTY_LABELS.get(level, level)


def label_deviation(level: str) -> str:
    return DEVIATION_LABELS.get(level, level)


# ---------------------------------------------------------------- 锚点


@dataclass
class Anchor:
    """一个疑点/证据在 Agent 原文中的落点。

    match_type:
      exact      逐字命中
      fuzzy      局部窗口近似命中（原文被改写）
      unanchored 没有锚定成功，不能作为疑似结论的依据
    """

    match_type: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.match_type in ("exact", "fuzzy") and self.char_start is not None

    def as_dict(self) -> dict:
        return {
            "match_type": self.match_type,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "match_score": round(self.score, 3),
        }
