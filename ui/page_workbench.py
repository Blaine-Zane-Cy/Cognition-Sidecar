"""工作台：构想 → 澄清 → 委托卡 → 贴回结果 → 结果卡 → 决定。

三条约束（v2）：
- session_state 只放 session_id + UI 临时态（展开/聚焦），业务状态一律从 DB 读；
- 所有输入控件的 key 带会话维度，A 的草稿不可能出现在 B 的框里；
- 输入不进 st.form：form 的值在点提交前不发给后端，草稿就捕不到"打完字直接切页"。
"""
from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

from core import clarify, db, delegation, drafts, flow
from core.models import SLOT_LABELS, STAGE_LABELS
from ui import components, layout, page_review_chat


def render() -> None:
    # 窄栏里顶部已经有「工作台」选项卡，再顶一个大标题纯属浪费 40px
    if not layout.narrow():
        st.markdown("## 工作台")
    _session_bar()

    sid = st.session_state.get("session_id")
    if sid:
        _delete_confirm(sid)
    if not sid or not db.session_exists(sid):
        _start_form()
        return

    sess = db.get_session(sid)
    stage = sess["stage"]
    if stage not in ("reviewing", "accepted", "rejected"):
        st.html(
            f'<div style="font-size:12px;color:{components.MUTED};padding-bottom:6px;'
            f'border-bottom:1px solid {components.BORDER};margin-bottom:12px;">'
            f"会话 {sid} ｜ 当前阶段：{STAGE_LABELS.get(stage, stage)}</div>",
        )
    if sess.get("parent_review_id"):
        st.html(
            f'<div style="font-size:12px;color:#185FA5;background:#E6F1FB;border-radius:8px;'
            f'padding:6px 10px;margin-bottom:10px;">↳ 承接自结果卡 {sess["parent_review_id"]}'
            f" 的第 {sess['parent_finding_id']} 条分支（有意的分叉，不是重复会话）</div>",
        )

    if stage == "clarifying":
        _clarifying(sid)
    elif stage == "delegation_ready":
        _delegation_stage(sid)
    elif stage == "awaiting_result":
        _awaiting(sid)
    elif stage == "reviewing":
        _review_stage(sid)
    elif stage in ("accepted", "rejected"):
        _decided(sid, stage)
    else:
        _start_form()


# ---------------------------------------------------------------- 顶栏


def _session_bar() -> None:
    sessions = db.list_sessions(30)
    labels = {}
    for s in sessions:
        mark = "↳ " if s.get("parent_review_id") else ""
        labels[s["id"]] = f"{mark}{s['created_at'][5:16]} · {s['brief'][:20]}"
    labels["__new__"] = "＋ 新建会话"
    options = [s["id"] for s in sessions] + ["__new__"]

    current = st.session_state.get("session_id") or "__new__"
    if current not in options:
        current = "__new__"
    # session_id 是唯一真相：这里只把下拉框对齐到它，绝不在 render 期间反向覆盖
    if st.session_state.get("sess_pick") != current:
        st.session_state["sess_pick"] = current

    c1, c2 = layout.cols([0.76, 0.24])
    with c1:
        st.selectbox(
            "会话",
            options,
            format_func=lambda x: labels.get(x, x),
            key="sess_pick",
            on_change=_on_session_pick,
            label_visibility="collapsed",
        )
    with c2:
        sid = st.session_state.get("session_id")
        if sid and sid != "__new__" and st.button(
            "删除记录", key=f"askdel_{sid}", use_container_width=True
        ):
            st.session_state["confirm_del"] = sid


def _delete_confirm(sid: str) -> None:
    """删记录要两下：第一下问，第二下才真删。规则库和设置不跟着走。"""
    if st.session_state.get("confirm_del") != sid:
        return
    sess = db.get_session(sid) or {}
    brief = (sess.get("brief") or "")[:32]
    st.warning(
        f"删掉「{brief}」这条记录？它的对话、结果卡、分支和上传附件会一起消失；"
        "规则库和设置不受影响。"
    )
    a, b = layout.cols(2)
    with a:
        go = st.button("确认删除", type="primary", use_container_width=True,
                       key=f"delyes_{sid}")
    with b:
        cancel = st.button("取消", use_container_width=True, key=f"delno_{sid}")
    if cancel:
        st.session_state.pop("confirm_del", None)
        st.rerun()
    if go:
        db.delete_session(sid)
        st.session_state["session_id"] = None
        st.session_state["sess_pick"] = "__new__"
        st.session_state.pop("confirm_del", None)
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and sid in k and (
                k.startswith(drafts.PREFIX) or k.startswith("upl_")
            ):
                st.session_state.pop(k, None)
        st.rerun()


def _on_session_pick() -> None:
    pick = st.session_state.get("sess_pick")
    st.session_state["session_id"] = None if pick == "__new__" else pick


def _start_form() -> None:
    st.markdown("### 你现在想让工作 Agent 做什么？")
    st.caption("写一句就够，模糊没关系——侧车的第一个工作就是把模糊变成可核查的委托。")
    brief = components.draft_text_area(
        "原始构想",
        "workbench",
        "brief",
        height=120,
        placeholder="例如：帮我做个个人网站，展示我的研究方向。",
        label_visibility="collapsed",
    )
    st.caption("写了一半切到别的页面不会丢，草稿存在本地库。")
    if st.button("开始澄清", type="primary", use_container_width=layout.narrow()):
        if len(brief.strip()) < 4:
            st.error("再多写几个字，我才能问出有用的问题。")
            return
        ok, sid = components.call(flow.start_session, brief.strip(), spinner="正在读你的构想…")
        if ok and sid:
            drafts.clear("workbench", "brief")
            st.session_state["session_id"] = sid
            st.rerun()

    rules_count = len(db.list_rules(status="active"))
    history = db.list_reviews(limit=3)
    if rules_count or history:
        st.divider()
        c1, c2 = st.columns(2)
        c1.metric("启用的偏好规则", rules_count)
        c2.metric("历史结果卡", len(db.list_reviews(limit=200)))


# ---------------------------------------------------------------- 对话流


def _transcript(sid: str) -> None:
    """唯一的消息渲染入口：每次 rerun 全量重绘，不做任何"追加"。

    failure 也占一格 —— 模型挂了不能表现成"你的话没人接"。
    """
    for msg in db.list_messages(sid):
        kind = msg["kind"]
        if kind == "brief":
            with st.chat_message("user"):
                st.markdown(msg["content"])
            st.html(
                f'<div style="font-size:11px;color:{components.MUTED};margin:-8px 0 6px 2px;">原始构想</div>'
            )
        elif kind == "failure":
            st.warning(msg["content"])
        elif kind == "note":
            st.html(
                f'<div style="font-size:12px;color:{components.MUTED};margin:2px 0 8px;">'
                f"{html.escape(msg['content'])}</div>",
            )
        else:
            role = "user" if msg["role"] == "user" else "assistant"
            with st.chat_message(role):
                st.markdown(msg["content"])


def _needs_retry(sid: str) -> bool:
    msgs = db.list_messages(sid)
    if not msgs:
        return False
    last = msgs[-1]
    return last["role"] == "user" or last["kind"] == "failure"


# ---------------------------------------------------------------- 澄清


def _clarifying(sid: str) -> None:
    state = db.get_clarify_state(sid)
    missing = clarify.missing(state["slots"], state["skipped"])

    if layout.narrow():
        # 窄栏：两栏并排在 400px 下会把对话压成两字一行。槽位面板收进折叠区，
        # 首屏留给对话；想看进度点一下，展开后内容一模一样。
        _clarify_main(sid, missing)
        filled = sum(1 for r in clarify.slot_panel(state["slots"], state["skipped"]) if r["filled"])
        with st.expander(f"待澄清槽位 {filled}/{len(state['slots'])}", expanded=False):
            _slot_panel(sid, state, missing)
        return

    left, right = st.columns([1.7, 1], gap="large")
    with left:
        _clarify_main(sid, missing)
    with right:
        st.markdown("#### 待澄清槽位")
        _slot_panel(sid, state, missing)


def _clarify_main(sid: str, missing: list[str]) -> None:
    st.markdown("#### 澄清")
    _transcript(sid)

    if missing:
        text = components.draft_text_area(
            "你的回答",
            "workbench",
            f"answer:{sid}",
            height=96,
            placeholder="直接说，不用组织语言",
            label_visibility="collapsed",
        )

        def _send_answer() -> None:
            if not text.strip():
                st.warning("写点东西再发。")
                return
            ok, _ = components.call(
                flow.submit_answer, sid, text.strip(), spinner="正在记下…"
            )
            # 不管成没成，这句话已经进库了；清掉草稿，失败就点重试
            drafts.clear("workbench", f"answer:{sid}")
            if ok:
                st.rerun()
            else:
                st.info("你的回答已经存下来了，点「重试上一步」继续。")

        retry = _needs_retry(sid)
        if layout.narrow() and not retry:
            # 窄栏：没有重试按钮时别让「发送」只占半行，右边留一块空白很难看
            if st.button("发送", type="primary", use_container_width=True):
                _send_answer()
        else:
            c1, c2 = st.columns([1, 1])
            if c1.button("发送", type="primary", use_container_width=True):
                _send_answer()
            if retry and c2.button(
                "重试上一步", key=f"retry_{sid}", use_container_width=True
            ):
                ok, _ = components.call(flow.retry_ask, sid, spinner="重试中…")
                if ok:
                    st.rerun()
        return

    st.caption("槽位已经齐了。要调整就直接说，我会重出一版委托卡。")
    text = components.draft_text_area(
        "要改什么",
        "workbench",
        f"revise:{sid}",
        height=80,
        placeholder="例如：把成功标准第 2 条换成“导师能在三十秒内说出我的两条主线”",
        label_visibility="collapsed",
    )
    if st.button("更新委托卡", type="primary", use_container_width=layout.narrow()):
        if text.strip():
            ok, _ = components.call(
                flow.submit_answer, sid, text.strip(), spinner="正在更新…"
            )
            drafts.clear("workbench", f"revise:{sid}")
            if ok:
                st.rerun()


def _slot_panel(sid: str, state: dict, missing: list[str]) -> None:
    for row in clarify.slot_panel(state["slots"], state["skipped"]):
        if row["skipped"]:
            st.html(
                f'<div style="font-size:13px;color:{components.MUTED};margin-bottom:6px;">'
                f'<s>{row["label"]}</s> · 已跳过（{row["skip_reason"]}）</div>',
            )
        elif row["filled"]:
            value = row["value"] if len(row["value"]) <= 46 else row["value"][:46] + "…"
            st.html(
                f'<div style="font-size:13px;margin-bottom:6px;">'
                f'<span style="color:#0F6E56;">●</span> <b>{row["label"]}</b>'
                f'<div style="font-size:12px;color:{components.MUTED};padding-left:12px;">'
                f"{html.escape(value)}</div></div>",
            )
        else:
            st.html(
                f'<div style="font-size:13px;margin-bottom:6px;">'
                f'<span style="color:#B4B2A9;">○</span> <b>{row["label"]}</b>'
                f'<div style="font-size:12px;color:#888780;padding-left:12px;">{row["hint"]}</div></div>',
            )

    st.divider()
    with st.expander("跳过槽位（结果卡会降级）", expanded=False):
        st.caption("跳过 = 这一项不进委托卡。结果卡的确定性会被压到「推测」，成功标准被跳过则完成度无法核定。")
        names = st.multiselect(
            "要跳过哪些",
            missing,
            format_func=lambda x: SLOT_LABELS.get(x, x),
            key=f"skip_names_{sid}",
        )
        reason = st.text_input(
            "跳过原因（会写在结果卡上）",
            placeholder="例如：这个我暂时想不清，先看它怎么做",
            key=f"skip_reason_{sid}",
        )
        if st.button("确认跳过", disabled=not names, key=f"skip_btn_{sid}"):
            flow.skip_slots(sid, names, reason)
            st.rerun()
        if missing and st.button("把剩下的全部跳过，直接出卡", key=f"skip_all_btn_{sid}"):
            flow.skip_slots(sid, missing, reason or "用户选择先跳过，直接看结果")
            st.rerun()


# ---------------------------------------------------------------- 委托卡


def _delegation_stage(sid: str) -> None:
    card = db.latest_delegation(sid)
    if not card:
        st.error("委托卡丢失，请回到澄清阶段重新生成。")
        return
    state = db.get_clarify_state(sid)

    st.markdown("#### 委托卡")
    issues = delegation.validate(card, state["skipped"])
    if issues:
        st.warning("这份卡还有硬问题，先修再交出去：" + "；".join(issues[:4]))
    if state["skipped"]:
        st.info("本次跳过：" + clarify.summarize_skip(state["skipped"]) + "（结果卡确定性会降级）")

    st.markdown("##### 复制给工作 Agent")
    st.code(delegation.render_text(card), language=None)

    c1, c2 = st.columns([1, 1], gap="small")
    if c1.button("定稿，去贴结果", type="primary", use_container_width=True):
        flow.to_awaiting(sid)
        st.rerun()
    if c2.button("让模型重写一版", use_container_width=True, key=f"rewrite_{sid}"):
        ok, _ = components.call(flow.ensure_delegation, sid, spinner="重写中…")
        if ok:
            st.rerun()

    with st.expander("编辑委托卡（可选）", expanded=False):
        with st.form(f"edit_delegation_{sid}"):
            goal = st.text_area("目标", card["goal"], height=68)
            a, b = layout.cols(2)
            deliverable = a.text_area("交付物", card["deliverable"], height=92)
            boundaries = b.text_area("边界", card["boundaries"], height=92)
            forbidden = st.text_area("禁止项", card["forbidden"], height=62)
            st.markdown("**成功标准**")
            rows = []
            for i, crit in enumerate(card["success_criteria"]):
                x, y = layout.cols([1.15, 1])
                text = x.text_input(f"标准 {i + 1}", crit["text"], key=f"ct_{sid}_{i}")
                how = y.text_input(f"验证方式 {i + 1}", crit.get("how_to_verify", ""), key=f"cv_{sid}_{i}")
                rows.append({"text": text, "how_to_verify": how})
            kw = st.text_input("关键词", "、".join(card["keywords"]))
            st.caption("只给侧车自己用来在 Agent 原文里定位段落，不会出现在复制给 Agent 的委托卡里。")
            saved = st.form_submit_button("保存为新版本")
        if saved:
            flow.update_delegation(
                sid,
                {
                    "goal": goal,
                    "deliverable": deliverable,
                    "boundaries": boundaries,
                    "forbidden": forbidden,
                    "success_criteria": rows,
                    "keywords": [k for k in kw.replace("，", "、").split("、") if k.strip()],
                },
            )
            st.rerun()


# ---------------------------------------------------------------- 贴回结果


# Agent 的交付物经常不是聊天框里的一段话，而是一个 .md、一份代码、一堆日志。
# 这些纯文本格式才是审阅要逐字定位的「原文」，所以直接读进输入框。
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".csv", ".tsv",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".sql", ".sh", ".bat", ".ps1", ".java", ".go",
    ".rs", ".c", ".cpp", ".h", ".diff", ".patch", ".tex", ".svg",
}


def _decode_upload(raw: bytes) -> str | None:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _docx_text(raw: bytes) -> str | None:
    """docx 就是 zip + word/document.xml，标准库能抽，不必为此多引一个依赖。"""
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paras = []
        for p in root.iter(ns + "p"):
            line = "".join(t.text or "" for t in p.iter(ns + "t"))
            if line.strip():
                paras.append(line)
        return "\n".join(paras) or None
    except Exception:  # noqa: BLE001
        return None


def _ingest_uploads(sid: str, uploads) -> list[str]:
    """把新上传的文件并进「结果原文」草稿，返回要给用户看的提示。

    用 file_id 去重：file_uploader 每次 rerun 都会把文件再交一遍，不去重会重复追加。
    二进制文件只留档、不并进原文 —— 审阅的 quote 必须能在原文里逐字找到，
    塞一段「这是张图片」进去只会污染锚定。
    """
    if not uploads:
        return []
    done: set = st.session_state.setdefault(f"upl_done_{sid}", set())
    wkey = drafts.widget_key("workbench", f"paste:{sid}")
    current = st.session_state.get(wkey) or db.load_draft("workbench", f"paste:{sid}") or ""
    remain = max(0, delegation.max_chars() - len(current))
    chunks: list[str] = []
    notes: list[str] = []

    for f in uploads:
        fid = getattr(f, "file_id", None) or f"{f.name}:{f.size}"
        if fid in done:
            continue
        done.add(fid)
        raw = f.read()
        ext = Path(f.name).suffix.lower()
        body = _docx_text(raw) if ext == ".docx" else (
            _decode_upload(raw) if (ext in TEXT_EXT or not ext) else None
        )
        if body is None:
            dest = db.db_file().parent / "uploads" / sid
            dest.mkdir(parents=True, exist_ok=True)
            (dest / Path(f.name).name).write_bytes(raw)
            notes.append(
                f"「{f.name}」已留档到 data/uploads，但本版只解析文本类文件，"
                "它的内容不会成为审阅依据。需要的话请让 Agent 用文字描述它。"
            )
            continue
        chunk = f"── 附件：{f.name} ──\n{body.strip()}"
        if len(chunk) > remain:
            chunk = chunk[: max(0, remain - 40)] + "\n…（已达原文上限，其余未并入）"
            notes.append(f"「{f.name}」超过剩余字数上限，只并入了前一部分。")
        chunks.append(chunk)
        remain = max(0, remain - len(chunk))

    if chunks:
        # append 只落库 + 打复位标记；下面 draft_text_area 的 prepare() 会在
        # 控件实例化之前把合并值取出来，所以**同一轮**就能看到，不必 rerun。
        drafts.append("workbench", f"paste:{sid}", "\n\n".join(chunks))
        st.session_state[f"upl_used_{sid}"] = True
    return notes


def _awaiting(sid: str) -> None:
    card = db.latest_delegation(sid)
    if card:
        with st.expander("委托卡（复制用）", expanded=False):
            st.code(delegation.render_text(card), language=None)

    st.markdown("#### 把工作 Agent 的完整输出贴回来")
    st.caption(
        f"可以直接传文件，也可以粘贴文字（上限 {delegation.max_chars()} 字）。"
        "疑点要靠原文定位，文本越准越好。"
    )
    uploads = st.file_uploader(
        "上传 Agent 交付的文件（可多选）",
        accept_multiple_files=True,
        key=f"upl_{sid}",
        help="文本 / Markdown / 代码 / 日志 / JSON / CSV / docx 会读成原文；PDF、图片等只留档。",
        label_visibility="collapsed" if layout.narrow() else "visible",
    )
    for note in _ingest_uploads(sid, uploads):
        st.caption(note)

    text = components.draft_text_area(
        "结果原文",
        "workbench",
        f"paste:{sid}",
        height=280,
        placeholder="把 Agent 的输出原样粘进来，不要改写、不要摘要。也可以从上面传文件。",
        label_visibility="collapsed",
    )
    if st.button("发起审阅", type="primary", use_container_width=layout.narrow()):
        if len(text.strip()) < 40:
            st.error("太短了，至少贴一段完整输出。")
            return
        if len(text) > delegation.max_chars():
            st.error(f"超过 {delegation.max_chars()} 字，先裁一下。")
            return
        source = "upload" if st.session_state.get(f"upl_used_{sid}") else "paste"
        ok, _ = components.call(
            flow.submit_result, sid, text.strip(), source=source,
            spinner="正在核对原文位置、生成结果卡…",
        )
        if ok:
            drafts.clear("workbench", f"paste:{sid}")
            st.session_state.pop(f"upl_done_{sid}", None)
            st.session_state.pop(f"upl_used_{sid}", None)
            st.rerun()


# ---------------------------------------------------------------- 评审


def _review_stage(sid: str) -> None:
    """评审阶段不再是"一张卡"，而是跟审阅专家的一段对话。

    完整卡片没有删：它收在对话末尾的折叠区里，需要核对时点开。
    """
    page_review_chat.render(sid)


def _decided(sid: str, stage: str) -> None:
    """结案之后仍然留在对话里：能回看自己当时说了什么，也方便接着贴下一轮。"""
    page_review_chat.render(sid)
