"""无 UI、离线跑通完整闭环。

    python demo.py            # 在 data/app.db 上跑一遍
    python demo.py --reset    # 先清空业务数据

演示模式不联网，但锚点校验作用在真实的 Agent 输出上，
所以“疑点可追溯”不是摆拍。
"""
from __future__ import annotations

import os
import sys

from core import anchoring, db, delegation, flow, llm, review_chat, rules
from core.models import CERTAINTY_LABELS, DEVIATION_LABELS, SLOT_LABELS, VERDICT_LABELS
from seeds import demo_data as D

W = 74


def hr(title: str) -> None:
    print("\n" + "═" * W)
    print(title)
    print("═" * W)


def step(n: str, text: str) -> None:
    print(f"\n── {n} {text}")


def main() -> int:
    os.environ["COGNITION_FORCE_MOCK"] = "1"  # 只影响本次进程，不改你在设置里的选择
    db.init_db()
    if "--reset" in sys.argv:
        db.reset_all()

    print(f"数据库：{db.db_file()}")
    print(f"模型：{llm.provider()}（演示模式，不联网）")

    # ---------------------------------------------------------------- 0
    hr("0 / 准备规则库")
    if db.list_rules():
        print(f"已有 {len(db.list_rules())} 条规则，保留。")
    else:
        for item in D.SEED_RULES:
            db.create_rule(
                text=item["text"],
                rationale=item["rationale"],
                scope=item["scope"],
                keywords=item["keywords"],
                source="manual",
                status=item["status"],
            )
        print(f"写入 {len(D.SEED_RULES)} 条种子规则（2 条启用 / 1 条归档）。")
    for r in db.list_rules():
        print(f"  {r['id']} [{r['status']}] {r['text']}  关键词={rules.dump_keywords(r['keywords'])}")

    # ---------------------------------------------------------------- 1
    hr("1 / 模糊构想进入侧车")
    print(f"[你] {D.BRIEF}")
    sid = flow.start_session(D.BRIEF)
    msgs = db.list_messages(sid)
    print(f"[侧车] {msgs[-1]['content']}")

    # ---------------------------------------------------------------- 2
    hr("2 / 多轮澄清（每轮只问一个缺口）")
    for answer in D.CLARIFY_ANSWERS:
        if db.get_session(sid)["stage"] != "clarifying":
            break
        print(f"\n[你] {answer}")
        flow.submit_answer(sid, answer)
        last = db.list_messages(sid)[-1]
        print(f"[侧车] {last['content']}")

    state = db.get_clarify_state(sid)
    step("2.1", "槽位看板")
    for name, value in state["slots"].items():
        shown = value if isinstance(value, str) else "；".join(c["text"] for c in value)
        print(f"  ● {SLOT_LABELS[name]}：{shown}")
    print(f"  跳过槽位：{state['skipped'] or '无'}")

    # ---------------------------------------------------------------- 3
    hr("3 / 委托卡（复制给工作 Agent）")
    card = db.latest_delegation(sid)
    if not card:
        print("✗ 没有生成委托卡，检查槽位是否齐全。")
        return 1
    print(delegation.render_text(card))

    # ---------------------------------------------------------------- 4
    hr("4 / 贴回结果 → 结果程度卡（第一轮）")
    result_id = flow.submit_result(sid, D.AGENT_OUTPUT_V1)
    rev = db.get_review(result_id)
    findings = db.list_findings(rev["id"])
    usages = db.usages_for_review(rev["id"])
    print_review(rev, findings, usages, D.AGENT_OUTPUT_V1)

    # ---------------------------------------------------------------- 4.5
    hr("4.5 / 审阅专家主动开口（第一人称，不是一张卡）")
    opening = db.list_review_messages(rev["id"])[0]
    for para in opening["refs"]:
        print(f"  〔{para['ref']}〕{para['text']}")
    print(f"\n  共 {len(opening['refs'])} 段。每段都挂着一个 ref —— 界面上就是这句话旁边的「依据」按钮，")
    print("  点开能看到它引用的原文位置或规则。段落是代码拼的骨架，模型只负责改写成口语。")

    # ---------------------------------------------------------------- 4.6
    hr("4.6 / 在对话里注入意志：排除一条 + 立一条通用偏好")
    for line in (D.CHAT_DISMISS, D.CHAT_RULE):
        print(f"\n[你] {line}")
        turn = review_chat.respond(rev["id"], line)
        for item in turn["applied"]:
            print(f"[侧车] {item['line']}")
    chat_rules = [
        r for r in db.list_rules()
        if r["source"] == "chat" and r["source_review_id"] == rev["id"]
    ]
    cands = review_chat.candidates(rev["id"])
    print(f"\n  剩余疑点 {len(cands['doubts'])} 条，已排除 {len(cands['dismissed'])} 条")
    print(f"  对话沉淀的规则：{[(r['id'], r['text'][:16], rules.dump_keywords(r['keywords'])) for r in chat_rules]}")

    followup = review_chat.build_followup(rev["id"])
    print("\n  点「打包成补充说明」得到的就是这段，可直接甩给 Agent：")
    for line in followup.splitlines():
        print("    " + line)

    # ---------------------------------------------------------------- 5
    hr("5 / 驳回 → 理由沉淀成规则")
    print(f"[驳回理由] {D.REJECT_REASON}")
    new_rule_id = flow.reject(rev["id"], D.REJECT_REASON)
    new_rule = db.get_rule(new_rule_id)
    print(f"\n新规则 {new_rule['id']}：{new_rule['text']}")
    print(f"  来源：驳回（评审 {new_rule['source_review_id']}）")
    print(f"  适用范围：{new_rule['scope']}  关键词：{rules.dump_keywords(new_rule['keywords'])}")
    print("  → 这条规则立即可在「规则库」页编辑/归档/删除。")

    # ---------------------------------------------------------------- 6
    hr("6 / 再跑一次评审 → 验证新规则被引用")
    flow.to_awaiting(sid)
    result2 = flow.submit_result(sid, D.AGENT_OUTPUT_V2)
    rev2 = db.get_review(result2)
    findings2 = db.list_findings(rev2["id"])
    usages2 = db.usages_for_review(rev2["id"])
    print_review(rev2, findings2, usages2, D.AGENT_OUTPUT_V2)

    cited = [u for u in usages2 if u["rule_id"] == new_rule_id]
    cited_chat = [u for u in usages2 if u["rule_id"] in {r["id"] for r in chat_rules}]
    cand2 = rules.candidates(db.latest_delegation(sid), D.AGENT_OUTPUT_V2)
    chat_in_cand = [c for c in cand2 if c["id"] in {r["id"] for r in chat_rules}]

    # ---------------------------------------------------------------- 7
    hr("7 / 分支：待办与决策（未锚定的不进首屏）")
    branches = [f for f in findings if f["kind"] in ("action_item", "decision")]
    branch_ok = [f for f in branches if f["match_type"] in ("exact", "fuzzy") or f["rule_id"]]
    branch_sink = [f for f in branches if f not in branch_ok]
    print(f"  入库 {len(branches)} 条：可追溯 {len(branch_ok)} 条，未锚定 {len(branch_sink)} 条")
    for f in branch_sink:
        print(f"  · 未锚定「{f['title']}」→ 只进折叠区，且不压确定性（卡上确定性仍是 {rev['certainty']}）")

    fork_sid = ""
    fork_sess = {}
    if branch_ok:
        target = next((f for f in branch_ok if f["kind"] == "decision"), branch_ok[0])
        fork_sid = flow.fork_from_finding(target["id"])
        fork_sess = db.get_session(fork_sid)
        src = db.get_finding(target["id"])
        print(f"\n  以「{target['title']}」新建会话：{fork_sid}")
        print(f"    父评审：{fork_sess['parent_review_id']}　父条目：{fork_sess['parent_finding_id']}")
        print(f"    原条目状态：{src['state']}　新会话阶段：{fork_sess['stage']}")
        tail = db.list_messages(fork_sid)[-1]
        print(f"    新会话首问：{tail['content'][:56]}")

    # ---------------------------------------------------------------- 8
    hr("8 / 断链测试：模型挂了会怎样")
    real_call = llm.call

    def boom(*_a, **_kw):
        raise llm.LLMError("模拟失败：接口不可用")

    before = {s["id"] for s in db.list_sessions(200, include_aborted=True)}
    llm.call = boom
    try:
        flow.start_session("这条构想会撞上模型故障")
    except Exception as exc:
        print(f"  ✓ 启动失败如实抛出：{type(exc).__name__}")
    finally:
        llm.call = real_call
    created = [s for s in db.list_sessions(200, include_aborted=True) if s["id"] not in before]
    orphan = created[0] if created else {}
    print(f"  ✓ 那条会话被标记为 stage={orphan.get('stage')}（不会再出现在工作台下拉里）")
    print(f"  ✓ 从下拉能看到的会话条数：{len(db.list_sessions(200))}（已排除中止会话）")

    llm.call = boom
    try:
        flow.submit_answer(sid, "这句回答会撞上模型故障")
    except Exception:
        pass
    finally:
        llm.call = real_call
    tail = db.list_messages(sid)[-2:]
    print(
        "  ✓ 历史尾部：" + " → ".join(f"{m['role']}/{m['kind']}" for m in tail)
        + "（用户发言后面必有侧车一格，不断层）"
    )
    msgs = db.list_messages(sid)
    rounds = [(m["role"], m["round_no"]) for m in msgs if m["kind"] != "brief"]
    print(f"  ✓ 消息共 {len(msgs)} 行，round 分布：{[r for _, r in rounds]}")

    # ---------------------------------------------------------------- 9
    hr("9 / 闭环验收")
    ok = True

    unanchored = [
        f for f in findings
        if f["kind"] in ("doubt", "risk") and f["match_type"] == "unanchored"
    ]
    anchored = [
        f for f in findings
        if f["kind"] in ("doubt", "risk") and f["match_type"] in ("exact", "fuzzy")
    ]
    checks = [
        ("疑点可锚回原文", len(anchored) >= 2, f"{len(anchored)} 条精确/近似命中"),
        ("存在锚定失败并被移出疑点区", len(unanchored) >= 1, f"{len(unanchored)} 条未锚定"),
        ("驳回后规则库新增一条可编辑规则", bool(new_rule), f"{new_rule_id}"),
        ("下一次评审引用了这条新规则", bool(cited), f"命中 {len(cited)} 次"),
        ("完成度由成功标准逐条算出", rev["completion_pct"] is not None, f"{rev['completion_pct']}%"),
        ("待办/决策已入库并带锚定状态", len(branches) >= 2, f"{len(branches)} 条分支"),
        ("存在未锚定的待办（默认不进首屏）", len(branch_sink) >= 1, f"{len(branch_sink)} 条下沉"),
        ("以分支新建会话留了血缘", bool(fork_sess.get("parent_review_id")), fork_sid or "未建"),
        ("模型失败不留孤儿会话", orphan.get("stage") == "aborted", str(orphan.get("stage"))),
        (
            "失败后历史仍是 user→assistant",
            bool(tail) and tail[-1]["kind"] == "failure",
            "→".join(m["kind"] for m in tail),
        ),
        (
            "开场白每段都挂了可追溯的 ref",
            len(opening["refs"]) >= 3 and all(p.get("ref") for p in opening["refs"]),
            f"{len(opening['refs'])} 段",
        ),
        (
            "对话里排除了疑点并留了理由",
            len(cands["dismissed"]) >= 1,
            cands["dismissed"][0]["chosen"][:16] if cands["dismissed"] else "无",
        ),
        (
            "对话里的话沉淀成规则（source=chat）",
            bool(chat_rules),
            "、".join(r["id"] for r in chat_rules) or "无",
        ),
        (
            "对话产生的规则进入下一轮候选",
            bool(chat_in_cand),
            (
                f"候选第 {[c['id'] for c in cand2].index(chat_in_cand[0]['id']) + 1} 位，"
                f"本轮被引用 {len(cited_chat)} 次"
            )
            if chat_in_cand
            else "没进候选",
        ),
        (
            "补充说明带上了已排除/已定项",
            "我确认可以接受" in followup
            and (cands["dismissed"][0]["title"] in followup if cands["dismissed"] else True),
            f"{len(followup)} 字",
        ),
        (
            "对话与澄清是两条线，互不污染",
            all(m["review_id"] is None for m in db.list_messages(sid)),
            f"澄清 {len(db.list_messages(sid))} 条 / 审阅 {len(db.list_review_messages(rev['id']))} 条",
        ),
    ]
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  {'✓' if passed else '✗'} {name}（{detail}）")

    print("\n闭环跑通。" if ok else "\n有检查项未通过，看上面标 ✗ 的位置。")
    print("用 python -m streamlit run app.py 打开界面，这些数据已经在了。")
    return 0 if ok else 1


def print_review(rev: dict, findings: list, usages: list, text: str) -> None:
    completion = "无法核定（成功标准被跳过）" if rev["completion_pct"] is None else f"{rev['completion_pct']}%"
    print(f"\n结论：{rev['headline']}")
    print(f"完成度：{completion}")
    print(f"确定性：{CERTAINTY_LABELS.get(rev['certainty'], rev['certainty'])}")
    if rev["certainty_note"]:
        print(f"  降级说明：{rev['certainty_note']}")
    print(f"偏差：{DEVIATION_LABELS.get(rev['deviation'], rev['deviation'])} —— {rev['deviation_note']}")

    subs = [f for f in findings if f["kind"] == "sub_goal"]
    if subs:
        print("\n成功标准逐条：")
        for i, f in enumerate(subs, 1):
            print(f"  {i}. [{VERDICT_LABELS.get(f['verdict'], f['verdict'])}] {f['title']}")
            if f["detail"]:
                print(f"     依据：{f['detail']}")

    good = [f for f in findings if f["kind"] in ("doubt", "risk") and f["match_type"] in ("exact", "fuzzy")]
    bad = [f for f in findings if f["kind"] in ("doubt", "risk") and f["match_type"] not in ("exact", "fuzzy")]

    print(f"\n疑点与风险（{len(good)} 条已锚定）：")
    for f in good:
        tag = "疑点" if f["kind"] == "doubt" else "风险"
        print(f"  · [{tag}] {f['title']}")
        print(f"     依据：{f['detail']}")
        print(f"     原文：{f['quote']}")
        print(
            f"     定位：第 {f['char_start']}–{f['char_end']} 字"
            f"（{f['match_type']}，相似度 {f['match_score']}）"
        )
        rebuilt = anchoring.verify(text, f["char_start"], f["char_end"])
        print(f"     回读：{rebuilt!r}")

    if bad:
        print(f"\n未锚定的条目（{len(bad)} 条，已移出疑点区，不作为驳回依据）：")
        for f in bad:
            print(f"  · {f['title']}")
            print(f"     它声称的原文：{f['quote']}")
            print("     校验结果：这句话在 Agent 输出里找不到")
            if f.get("rule_id"):
                print(f"     但它引用了规则 {f['rule_id']}，可作为规则依据")

    if usages:
        print("\n按哪条规则判的：")
        for u in usages:
            print(f"  · {u['rule_id']} [{u['verdict']}] {u['rule_text']}")
            if u["reason"]:
                print(f"     理由：{u['reason']}")
    else:
        print("\n本次没有可引用的规则（规则库为空或没有关键词命中）。")

    items = [f for f in findings if f["kind"] in ("action_item", "decision")]
    if items:
        print(f"\n待处理分支（{len(items)} 条）：")
        for f in items:
            tag = "待办" if f["kind"] == "action_item" else "决策"
            if f["match_type"] in ("exact", "fuzzy"):
                where = f"原文第 {f['char_start']}–{f['char_end']} 字（{f['match_type']}）"
            elif f["rule_id"]:
                where = f"引用规则 {f['rule_id']}"
            else:
                where = "未锚定（不进首屏）"
            print(f"  [{tag}] {f['title']}　（{where}）")
            if f["kind"] == "decision":
                if f["recommended"]:
                    print(f"     推荐：{f['recommended']}" + (f" · {f['why']}" if f["why"] else ""))
                for o in f["options"]:
                    print(f"       - {o['label']}　代价：{o['cost'] or '未说明'}")
            elif f["detail"]:
                print(f"     {f['detail']}")
            if f["char_start"] is not None:
                print(f"     回读：{anchoring.verify(text, f['char_start'], f['char_end'])!r}")

    print("\n下一步选项：")
    for i, opt in enumerate(rev["next_options"], 1):
        print(f"  {i}. {opt['label']}")
        if opt.get("cost"):
            print(f"     代价：{opt['cost']}")


if __name__ == "__main__":
    raise SystemExit(main())
