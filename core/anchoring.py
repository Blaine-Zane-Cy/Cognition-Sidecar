"""原文锚点定位：把模型给的 quote 钉回 Agent 输出的具体字符区间。

三道闸门（对应需求改动 ④）：
1. 精确命中：归一化后 find。
2. 局部窗口近似命中：先用引文头部/尾部做种子，用 str.find 找到候选起点，
   只在候选起点附近的开窗里做 difflib 相似度比较——不做全文滑窗。
3. 都不中：unanchored。这类疑点不允许出现在默认疑点区。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .models import Anchor

# 全角标点折成半角，保证原文与引文的排版差异不影响匹配
_FOLD = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "、": ",",
        "；": ";",
        "：": ":",
        "？": "?",
        "！": "!",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "—": "-",
        "–": "-",
        "　": " ",
    }
)

# markdown 语法字符：不影响语义，直接丢弃，避免 `**加粗**` 之类干扰匹配
_MD_CHARS = set("*`~_#>|")

MIN_QUOTE_LEN = 4       # 太短的片段不足以作为依据
SEED_LENGTHS = (16, 12, 8, 6, 4)
MAX_SEED_HITS = 8       # 每个种子最多看几个候选位置，防止病态长文本拖慢
WINDOW_PAD = 8          # 局部窗口在候选起点前后各留的字数


def normalize(text: str) -> tuple[str, list[int]]:
    """返回 (归一化文本, 归一化下标 -> 原文下标)。"""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch in _MD_CHARS:
            continue
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                idx.append(i)
            continue
        folded = ch.translate(_FOLD)
        for c in folded:
            if c.isascii() and c.isupper():
                c = c.lower()
            out.append(c)
            idx.append(i)
    while out and out[0] == " ":
        out.pop(0)
        idx.pop(0)
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def locate(quote: str, text: str, threshold: float = 0.75) -> Anchor:
    """把 quote 钉到 text 上。threshold 来自设置页（需求改动 ④）。"""
    if not quote or not text:
        return Anchor("unanchored")

    nquote, _ = normalize(quote)
    nquote = nquote.strip()
    if len(nquote) < MIN_QUOTE_LEN:
        return Anchor("unanchored")

    ntext, idx = normalize(text)
    if not ntext:
        return Anchor("unanchored")

    # 闸门 1：逐字命中
    pos = ntext.find(nquote)
    if pos >= 0:
        return _build(idx, pos, pos + len(nquote), "exact", 1.0)

    # 闸门 2：种子定位 + 局部窗口比对
    # 只在“种子命中的位置”开一个小窗，在窗内滑动比对；
    # 绝不对全文做滑窗（长文本会把 O(n) 变成 O(n·m)，且容易误命中无关段落）。
    L = len(nquote)
    best_ratio = 0.0
    best_span: tuple[int, int] | None = None
    for seed_len in SEED_LENGTHS:
        if seed_len > L:
            continue
        for offset in _seed_offsets(L, seed_len):
            seed = nquote[offset : offset + seed_len]
            if len(seed) < MIN_QUOTE_LEN:
                continue
            for pos in _seed_positions(ntext, seed):
                cand = pos - offset
                if cand <= -WINDOW_PAD or cand >= len(ntext):
                    continue
                win_start = max(0, cand - WINDOW_PAD)
                win = ntext[win_start : min(len(ntext), cand + L + WINDOW_PAD)]
                min_len = max(MIN_QUOTE_LEN, int(L * 0.6))
                for i in range(0, max(1, len(win) - L + 1)):
                    piece = win[i : i + L]
                    if len(piece) < min_len:
                        continue
                    ratio = SequenceMatcher(None, nquote, piece).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        s, e = win_start + i, win_start + i + len(piece)
                        while s < e and ntext[s] == " ":
                            s += 1
                        while e > s and ntext[e - 1] == " ":
                            e -= 1
                        best_span = (s, e)
        if best_ratio >= threshold:
            break

    if best_span and best_ratio >= threshold:
        return _build(idx, best_span[0], best_span[1], "fuzzy", best_ratio)
    return Anchor("unanchored", score=round(best_ratio, 3))


def _seed_offsets(length: int, seed_len: int) -> list[int]:
    """种子在引文里的取样位置：头、尾、1/4、1/2、3/4。"""
    span = max(0, length - seed_len)
    raw = [0, span, length // 4, length // 2, (3 * length) // 4]
    out: list[int] = []
    for item in raw:
        item = max(0, min(item, span))
        if item not in out:
            out.append(item)
    return out


def _seed_positions(ntext: str, seed: str) -> list[int]:
    found = []
    start = ntext.find(seed)
    count = 0
    while start >= 0 and count < MAX_SEED_HITS:
        found.append(start)
        count += 1
        start = ntext.find(seed, start + 1)
    return found


def _build(idx: list[int], start: int, end: int, match_type: str, score: float) -> Anchor:
    if start >= len(idx):
        return Anchor("unanchored", score=score)
    end = min(end, len(idx))
    char_start = idx[start]
    char_end = idx[end - 1] + 1
    return Anchor(match_type, char_start, char_end, score)


def snippet(text: str, start: int, end: int, context: int = 70) -> tuple[str, str, str]:
    """给 UI 用的三段式片段：前文 / 命中 / 后文。"""
    a = max(0, start - context)
    b = min(len(text), end + context)
    return text[a:start], text[start:end], text[end:b]


def verify(text: str, start: int, end: int) -> str:
    """自检工具：返回命中的原文片段，用于 demo 里证明追溯是真的。"""
    if start is None or end is None:
        return ""
    return text[start:end]
