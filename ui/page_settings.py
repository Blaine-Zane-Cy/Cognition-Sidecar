"""设置页：模型接入 + 匹配阈值 + 数据位置。"""
from __future__ import annotations

import html
import json

import streamlit as st

from core import db, drafts, llm, settings
from ui import components, layout


def render() -> None:
    if not layout.narrow():  # 窄栏顶栏已有同名选项卡，标题只占高度
        st.markdown("## 设置")
    prov = llm.provider()
    if prov == "mock":
        st.info(
            "当前是演示模式：不联网，结果卡由本地替身生成，但原文锚点校验是真的。"
            "填好 Key 并把模式改成「自动」或「真实模型」即可切换。"
        )
    else:
        st.success(f"当前接的是真实模型：{settings.get('model')} @ {settings.get('base_url')}")

    with st.form("llm_settings"):
        st.markdown("#### 模型接入（OpenAI 兼容）")
        c1, c2 = layout.cols(2)
        base_url = c1.text_input("Base URL", settings.get("base_url"), help="例如 https://api.deepseek.com/v1")
        model = c2.text_input("模型名", settings.get("model"), help="例如 deepseek-chat / gpt-4o-mini / qwen2.5:7b")
        api_key = st.text_input(
            "API Key",
            value=settings.get_api_key(),
            type="password",
            help="只存在本地 data/secrets.json，不写进数据库、不上传。",
        )
        mode = st.radio(
            "模式",
            ["auto", "live", "mock"],
            index=["auto", "live", "mock"].index(settings.get("provider_mode") or "auto"),
            horizontal=not layout.narrow(),
            format_func=lambda x: {"auto": "自动（有 Key 就用真实模型）", "live": "强制真实模型", "mock": "演示替身"}[x],
        )

        st.markdown("#### 判据")
        threshold = st.slider(
            "模糊匹配阈值",
            0.50,
            0.95,
            float(settings.get_float("fuzzy_threshold", 0.75)),
            0.01,
            help="疑点引文与原文的相似度低于这个值，就判定为“未锚定”，不进入疑点区。调高更严，但会有更多疑点被判掉。",
        )
        max_chars = st.number_input(
            "单次贴回文本上限（字）",
            min_value=2000,
            max_value=200000,
            value=settings.get_int("max_output_chars", 30000),
            step=1000,
        )
        saved = st.form_submit_button(
            "保存设置", type="primary", use_container_width=layout.narrow()
        )

    if saved:
        settings.set_value("base_url", base_url.strip())
        settings.set_value("model", model.strip())
        settings.set_value("provider_mode", mode)
        settings.set_value("fuzzy_threshold", threshold)
        settings.set_value("max_output_chars", int(max_chars))
        settings.set_api_key(api_key)
        st.success("已保存")
        st.rerun()

    st.divider()
    key_line = (
        f"Key 状态：{settings.mask_key(settings.get_api_key())} ｜ "
        f"实际生效模式：{'演示替身' if prov == 'mock' else '真实模型'}"
    )
    if layout.narrow():
        if st.button("测试连接", use_container_width=True):
            _test_conn()
        st.caption(key_line)
    else:
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("测试连接"):
                _test_conn()
        with c2:
            st.caption(key_line)

    st.divider()
    st.markdown("#### 草稿")
    rows = db.list_drafts()
    st.caption(
        f"共 {len(rows)} 条，合计 {sum(len(r['content']) for r in rows)} 字。"
        "草稿存在本地库里：切换页面、刷新浏览器都不会丢，提交成功后自动清掉。"
    )
    if rows:
        with st.expander("看有哪些草稿", expanded=False):
            for r in rows:
                st.markdown(
                    f"- `{r['scope']} / {r['key']}` · {r['updated_at']} · {len(r['content'])} 字"
                )
    c1, c2 = st.columns(2)
    if c1.button("清空全部草稿", use_container_width=True):
        drafts.clear_all()
        st.success("已清空草稿。")
        st.rerun()
    with c2:
        aborted = [s for s in db.list_sessions(limit=200, include_aborted=True) if s["stage"] == "aborted"]
        if aborted:
            if st.button(f"清理中止会话（{len(aborted)} 条）", use_container_width=True):
                db.abort_empty_sessions()
                st.success(f"已整理 {len(aborted)} 条中止会话，它们不再出现在工作台下拉里。")
                st.rerun()
        else:
            st.caption("没有中止会话。建模失败留下的空会话会出现在这里。")
    if aborted:
        with st.expander(f"中止会话（{len(aborted)} 条，已从下拉隐藏）", expanded=False):
            st.caption("这些会话只写了构想、没跑起来（通常是模型调用失败）。留着是为了不丢你已经写过的字。")
            for s in aborted:
                st.markdown(f"- `{s['id']}` {s['created_at']} · {s['brief'][:44]}")

    st.divider()
    st.markdown("#### 数据")
    # 完整路径很长（中文目录 + 深层级），inline code 默认 white-space:pre 会把 p 撑宽。
    # 这里自己控制折行，不靠全局 CSS —— Streamlit 的 markdown DOM 层级会变，
    # 靠选择器命中的规则容易某天悄悄失效（400px 审计就抓到过一次）。
    for label, path, note in (
        ("数据库", str(db.db_file()), "本地 SQLite，不上传"),
        ("Key 文件", str(settings.SECRETS_PATH), "明文，别提交到仓库或发给别人"),
    ):
        st.html(
            '<div style="font-size:12.5px;line-height:1.6;'
            'overflow-wrap:anywhere;word-break:break-all;margin-bottom:2px;">'
            f'{label}：<code style="white-space:pre-wrap;word-break:break-all;">'
            f'{html.escape(path)}</code>　（{note}）</div>'
        )
    st.download_button(
        "导出全部数据（JSON）",
        data=json.dumps(db.export_all(), ensure_ascii=False, indent=2),
        file_name="cognition-sidecar-export.json",
        mime="application/json",
        use_container_width=layout.narrow(),
    )

    st.divider()
    st.markdown("#### 清空工作台历史")
    # 成功提示放到下一轮渲染：这里 rerun 之后才能看到清零后的条数
    if st.session_state.get("purge_done"):
        backup = st.session_state.pop("purge_done")
        st.session_state.pop("confirm_purge_history", None)  # 免得复选框还勾着
        st.success(f"已清空。原库备份在 `{backup}`（要回滚就用它覆盖 app.db）。")

    hist = db.counts()
    st.caption(
        f"现有：会话 {hist['sessions']} · 消息 {hist['messages']} · "
        f"委托卡 {hist['delegations']} · 结果 {hist['results']} · "
        f"评审 {hist['reviews']} · 疑点 {hist['findings']}"
    )
    st.caption(
        "会保留：规则库、规则引用统计、模型与 API 设置、草稿。"
        "执行前自动把当前库备份成 `data/app.db.bak`（已有同名备份则先按时间戳改名留档）。"
    )
    confirm_hist = st.checkbox("我确认清空上面这些历史（规则库和设置不动）")
    if st.button(
        "清空工作台历史",
        disabled=not confirm_hist,
        use_container_width=layout.narrow(),
        type="primary" if layout.narrow() else "secondary",
    ):
        result = db.reset_history()
        st.session_state["session_id"] = None
        st.session_state["purge_done"] = result["backup"]
        st.rerun()

    with st.expander("连规则库一起清空（不推荐）", expanded=False):
        st.caption(
            "适合把项目交给别人之前彻底还库。规则、引用统计、草稿都会没，"
            "模型设置保留。同样会先备份。"
        )
        confirm_all = st.checkbox("我确认连规则和草稿一起清空", key="confirm_all_purge")
        if st.button("全部清空", disabled=not confirm_all, use_container_width=layout.narrow()):
            db.reset_all()
            st.session_state["session_id"] = None
            st.success("已清空，包括规则库。")
            st.rerun()


def _test_conn() -> None:
    if llm.provider() == "mock":
        st.info("当前是演示替身，没有发出请求。要真的测，先把模式改成「强制真实模型」并填好 Key。")
        return
    ok, reply = components.call(llm.complete_text, "只回复：连接正常", spinner="正在请求…")
    if ok:
        st.success(f"接口可用，返回：{str(reply)[:60]}")
