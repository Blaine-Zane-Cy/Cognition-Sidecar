"""窄栏模式（`?embed=1`）：一个开关、一层样式、一套列布局。

为什么不用 `st.set_page_config(layout=...)`：
它必须是第一个 Streamlit 命令，而且同一会话里改不了 —— 拿它当开关，
用户想从窄栏退回宽屏就只能重启会话。所以宽屏布局永远不变，
窄栏完全由这里注入的一层 CSS 决定：加参数刷新就窄，去参数刷新就宽。

三个约定：
- `narrow()` 每次现读 query_params，不缓存（缓存会死在"改 URL 不重启"的场景上）；
- `cols()` 在窄栏返回竖向堆叠容器，宽屏原样透传 —— 业务代码只换调用点，
  不在页面里散装 `if narrow():`；
- 需要按钮并排的地方直接 `st.columns(2)`：两颗按钮在 400px 下仍然可读，
  四颗就会挤成每颗 88px，那才是真的坏。
"""
from __future__ import annotations

import streamlit as st

_TRUE = {"1", "true", "yes", "on"}

# 为什么不用 `?embed=1` 当开关：`embed` 是 Streamlit 自己的保留参数
# （`?embed=true` 会进原生嵌入模式），它在送给后端的 query_params 里被**整个吃掉**，
# Python 一侧根本读不到。所以窄栏开关叫 `narrow`，`embed=true` 留着给 Streamlit
# 隐藏 Deploy 按钮，两者各管一件事：
#     http://localhost:8501/?embed=true&narrow=1
_KEYS = ("narrow", "side", "panel", "embed_mode", "embed")

# 窄栏基准宽度：设计目标 400–480px，实际兼容 320–600px（侧边栏可拖动）
NARROW_CSS = """
/* ---------- 1. 砍壳：窄栏里浏览器自己的边距已经很贵了 ---------- */
[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stAppDeployButton"],
[data-testid="stHeaderActionElements"],
#MainMenu,
footer {
  display: none !important;
}
[data-testid="stAppViewContainer"],
[data-testid="stMain"] {
  padding-top: 0 !important;
}

/* ---------- 2. 砍空：padding / gap / 分割线一起收紧 ---------- */
/* Streamlit 在 640px 以下给每个 stColumn 加了 min-width:calc(100% - 21px)，
   等于把所有列无条件竖排（420px 下连两颗按钮并排都做不到）。
   窄栏里排法已经由代码按屏决定，这里把那个下限解开，让 flex 权重重新生效。 */
[data-testid="stColumn"] { min-width: 0 !important; }
[data-testid="stMainBlockContainer"],
.block-container {
  max-width: 100% !important;
  padding: 0.5rem 0.7rem 1.5rem 0.7rem !important;
}
[data-testid="stVerticalBlock"] { gap: 0.5rem; }
[data-testid="stHorizontalBlock"] { gap: 0.42rem; }
[data-testid="stElementContainer"] { margin: 0; }
hr, [data-testid="stDivider"] { margin: 9px 0 !important; }
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 12px; }
[data-testid="stVerticalBlockBorderWrapper"] > div > [data-testid="stVerticalBlock"] {
  padding: 0.55rem 0.6rem !important;
  gap: 0.4rem;
}

/* ---------- 3. 压字号：基准 16→14，正文 13.5 ---------- */
html { font-size: 14px; }
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li {
  font-size: 13.5px;
  line-height: 1.72;
  overflow-wrap: anywhere;
}
[data-testid="stMarkdownContainer"] h1 { font-size: 19px !important; }
[data-testid="stMarkdownContainer"] h2 { font-size: 17px !important; }
[data-testid="stMarkdownContainer"] h3 { font-size: 15px !important; }
[data-testid="stMarkdownContainer"] h4,
[data-testid="stMarkdownContainer"] h5 { font-size: 13.5px !important; }
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] p { font-size: 11.5px !important; line-height: 1.6; }

/* ---------- 4. 不溢出：这四条比藏侧边栏重要 ---------- */
pre, [data-testid="stCode"] pre {
  white-space: pre-wrap !important;
  word-break: break-word !important;
  overflow-x: hidden !important;
  font-size: 12px !important;
}
/* 真正的横行源头是 pre 里的 code：它自己带 white-space:pre + display:inline-block +
   内容宽度（1019px），只压 pre 是压不住的，文字会被裁掉而不是折行。 */
pre code, [data-testid="stCode"] pre code {
  white-space: pre-wrap !important;
  word-break: break-word !important;
  display: block !important;
  width: auto !important;
  min-width: 0 !important;
  font-size: 12px !important;
}
/* 行内 code（设置页的本地路径、Key 文件路径）同样不能撑宽段落：
   它默认 white-space:pre，一条长 Windows 路径就足以让 p 横向溢出。
   只打 p 里的 code，不碰上面 pre code 那条规则。 */
.stMarkdown p code, [data-testid="stMarkdownContainer"] p code {
  white-space: pre-wrap !important;
  word-break: break-all !important;
  overflow-wrap: anywhere !important;
}
[data-testid="stCode"] { max-width: 100% !important; }
[data-testid="stPopoverBody"] {
  max-width: calc(100vw - 22px) !important;
  min-width: min(300px, calc(100vw - 22px)) !important;
}
[data-testid="stPopoverBody"] [data-testid="stMarkdownContainer"] p { font-size: 12.5px; }
img, table { max-width: 100% !important; }

/* ---------- 5. 气泡：藏头像，省 40px 横向 ---------- */
[data-testid="stChatMessage"] {
  padding: 9px 11px !important;
  border-radius: 12px;
  gap: 0.45rem !important;
}
[data-testid="stChatMessageAvatarUser"],
[data-testid="stChatMessageAvatarAssistant"] { display: none !important; }
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {
  flex: 1 1 auto;
  min-width: 0;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
  background: #F7F6F2;
  border-left: 2px solid #D3D1C7;
}
/* 头像藏了之后，用户气泡会变成"一行没有主的字"；用淡蓝底把谁是说话人标回来 */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
  background: #EEF5FC;
  border-left: 2px solid #B9D3EF;
}
[data-testid="stChatMessage"] p { margin-bottom: 0.4rem; }
[data-testid="stChatMessage"] p:last-child { margin-bottom: 0; }

/* ---------- 6. 触控尺寸：窄栏鼠标精度低，按钮不能太小 ---------- */
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-primary"] {
  min-height: 34px;
  padding: 4px 10px;
  font-size: 13px;
}
[data-testid="stExpander"] summary {
  font-size: 13px;
  padding: 0.35rem 0.5rem;
  min-height: 34px;
}
[data-testid="stTabs"] button { font-size: 13px; padding: 5px 8px; }
[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
  min-height: 36px;
  padding: 2px 8px;
  font-size: 13px;
}
[data-testid="stPopover"] button {
  min-height: 30px !important;
  font-size: 12px !important;
  font-weight: 400;
  color: #5F5E5A;
}
[data-testid="stTextArea"] textarea { font-size: 13px; line-height: 1.6; }
[data-testid="stButtonGroup"] button { font-size: 12.5px; padding: 4px 6px; }
"""


def narrow() -> bool:
    """窄栏开关。只认 1/true/yes/on，`?narrow=0` 一律当宽屏。

    同时接受 side / panel / embed_mode 这几个别名（`embed` 是给 Streamlit 的，
    正常情况下读不到，留着只是防它哪天改主意不再吞这个参数）。
    """
    for key in _KEYS:
        raw = st.query_params.get(key)
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        if raw is None:
            continue
        if str(raw).strip().lower() in _TRUE:
            return True
    return False


def cols(spec, gap: str = "small", vertical_alignment: str = "center"):
    """窄栏 → 竖向堆叠的等宽容器；宽屏 → `st.columns` 原样。

    返回的都是可以 `with` / 直接 `.button()` 的对象，所以调用点只改函数名。
    spec 既可以是 `3` 也可以是 `[1.1, 1]`（和 `st.columns` 一样）。
    """
    if narrow():
        n = spec if isinstance(spec, int) else len(spec)
        return [st.container() for _ in range(n)]
    return st.columns(spec, gap=gap, vertical_alignment=vertical_alignment)


def inject_css() -> None:
    if narrow():
        st.html(f"<style>{NARROW_CSS}</style>")
