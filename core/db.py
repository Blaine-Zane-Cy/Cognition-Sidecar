"""SQLite 存储层。

约定（对应需求改动 ①）：业务状态只有一个真相来源——这里。
UI 的 session_state 只允许放 session_id 和纯界面态（例如“正在展开哪个面板”）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "app.db"


def db_file() -> Path:
    env = os.environ.get("COGNITION_DB")
    return Path(env) if env else DEFAULT_DB


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:10]


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    brief       TEXT NOT NULL,
    stage       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    parent_review_id  TEXT,
    parent_finding_id INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'chat',
    round_no    INTEGER NOT NULL DEFAULT 0,
    review_id   TEXT,
    phase       TEXT NOT NULL DEFAULT 'clarify',
    refs_json   TEXT NOT NULL DEFAULT '[]',
    branch_id   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clarify_state (
    session_id  TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    slots_json  TEXT NOT NULL,
    skipped_json TEXT NOT NULL,
    round_count INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delegations (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    goal        TEXT NOT NULL DEFAULT '',
    deliverable TEXT NOT NULL DEFAULT '',
    boundaries  TEXT NOT NULL DEFAULT '',
    forbidden   TEXT NOT NULL DEFAULT '',
    success_criteria_json TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    raw_output  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'paste',
    char_count  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    result_id   TEXT NOT NULL REFERENCES results(id),
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    headline    TEXT NOT NULL,
    completion_pct INTEGER,
    sub_goals_json TEXT NOT NULL DEFAULT '[]',
    certainty   TEXT NOT NULL,
    certainty_note TEXT NOT NULL DEFAULT '',
    deviation   TEXT NOT NULL,
    deviation_note TEXT NOT NULL DEFAULT '',
    next_options_json TEXT NOT NULL DEFAULT '[]',
    skipped_json TEXT NOT NULL DEFAULT '[]',
    unanchored_count INTEGER NOT NULL DEFAULT 0,
    low_confidence INTEGER NOT NULL DEFAULT 0,
    provider    TEXT NOT NULL DEFAULT 'mock',
    raw_json    TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    reject_reason TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id   TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    certainty   TEXT NOT NULL DEFAULT 'medium',
    severity    TEXT,
    verdict     TEXT,
    anchor_type TEXT NOT NULL DEFAULT 'none',
    quote       TEXT,
    char_start  INTEGER,
    char_end    INTEGER,
    match_type  TEXT,
    match_score REAL,
    rule_id     TEXT,
    options_json TEXT NOT NULL DEFAULT '[]',
    recommended TEXT NOT NULL DEFAULT '',
    why         TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'open',
    chosen      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 审阅分支：一条待办/决策 = 一支独立对话。主干（branch_id 为空）是只读底座。
CREATE TABLE IF NOT EXISTS branches (
    id          TEXT PRIMARY KEY,
    review_id   TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    finding_id  INTEGER,                -- 来源条目；可空 = 用户手工开的分支
    kind        TEXT NOT NULL,          -- action_item | decision | doubt | risk | free
    title       TEXT NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    quote       TEXT,
    char_start  INTEGER,
    char_end    INTEGER,
    state       TEXT NOT NULL DEFAULT 'open',   -- open | merged | dropped
    resolution  TEXT NOT NULL DEFAULT '',
    merged_at   TEXT,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    scope       TEXT NOT NULL DEFAULT 'both',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL,
    source_review_id TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_usages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    review_id   TEXT REFERENCES reviews(id) ON DELETE SET NULL,
    verdict     TEXT NOT NULL DEFAULT 'mentioned',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- 自由对话：像 LLM 那样随时开聊，和「贴结果→评审」那条流水线并列、互不串台。
-- 单独建表而不是复用 messages：messages 的 session_id/review_id/branch_id
-- 都是审阅语义，硬塞自由对话会污染主干查询。
CREATE TABLE IF NOT EXISTS chats (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新对话',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_review ON findings(review_id);
CREATE INDEX IF NOT EXISTS idx_usages_rule ON rule_usages(rule_id);
CREATE INDEX IF NOT EXISTS idx_usages_review ON rule_usages(review_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_reviews_session ON reviews(session_id);
CREATE INDEX IF NOT EXISTS idx_drafts_scope ON drafts(scope);
CREATE INDEX IF NOT EXISTS idx_branches_review ON branches(review_id);
"""

# 老库升级：只加列，不改类型、不删列。每条都可重复执行。
MIGRATIONS = [
    ("messages", "round_no", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "review_id", "TEXT"),
    ("messages", "phase", "TEXT NOT NULL DEFAULT 'clarify'"),
    ("messages", "refs_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("sessions", "parent_review_id", "TEXT"),
    ("sessions", "parent_finding_id", "INTEGER"),
    ("findings", "options_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("findings", "recommended", "TEXT NOT NULL DEFAULT ''"),
    ("findings", "why", "TEXT NOT NULL DEFAULT ''"),
    ("findings", "state", "TEXT NOT NULL DEFAULT 'open'"),
    ("findings", "chosen", "TEXT NOT NULL DEFAULT ''"),
    # 审阅分支：空 = 主干。老库里已有的消息天然全是主干，不需要回填。
    ("messages", "branch_id", "TEXT"),
]


@contextmanager
def connect():
    path = db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：老库直接加列，不丢数据。"""
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:  # 表还不存在（首次建库时会由 SCHEMA 建立）
            continue
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session_round ON messages(session_id, round_no)"
    )
    # 放在迁移之后建：老库里 messages 还没有 review_id，写在 SCHEMA 里会直接报错
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_review ON messages(review_id)"
    )
    # 同理：老库的 messages 在迁移前也没有 branch_id
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_branch ON messages(branch_id)"
    )
    _backfill_rounds(conn)
    _make_usage_review_nullable(conn)


def _make_usage_review_nullable(conn: sqlite3.Connection) -> None:
    """把 rule_usages.review_id 从「NOT NULL + CASCADE」改成「可空 + SET NULL」。

    起因：清空工作台历史要删 reviews，而这条外键是 CASCADE —— "这条规则被引用过
    几次"会被顺带删掉，而这恰恰是规则库最需要的反馈。

    改成可空后，删评审只是把归属置空：统计留下，也不会留下悬空外键
    （NOT NULL 时只能塞占位值，`PRAGMA foreign_key_check` 会一直报）。
    SQLite 改不了列约束，只能重建表；SQL 里那句 NULLIF 兼容更早的占位值写法。
    """
    info = {r["name"]: r for r in conn.execute("PRAGMA table_info(rule_usages)")}
    if "review_id" not in info or not info["review_id"]["notnull"]:
        return  # 表还不存在，或已经迁过
    conn.executescript(
        """
        CREATE TABLE rule_usages_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
            review_id   TEXT REFERENCES reviews(id) ON DELETE SET NULL,
            verdict     TEXT NOT NULL DEFAULT 'mentioned',
            reason      TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        INSERT INTO rule_usages_new (id, rule_id, review_id, verdict, reason, created_at)
            SELECT id, rule_id, NULLIF(review_id, 'purged'), verdict, reason, created_at
            FROM rule_usages;
        DROP TABLE rule_usages;
        ALTER TABLE rule_usages_new RENAME TO rule_usages;
        CREATE INDEX IF NOT EXISTS idx_usages_rule ON rule_usages(rule_id);
        CREATE INDEX IF NOT EXISTS idx_usages_review ON rule_usages(review_id);
        """
    )


def _backfill_rounds(conn: sqlite3.Connection) -> None:
    """给老消息补 round_no：brief 记第 0 轮，之后每遇到一条用户发言就 +1。"""
    rows = conn.execute(
        "SELECT id, session_id, role, kind FROM messages ORDER BY session_id, id"
    ).fetchall()
    if not rows:
        return
    if conn.execute("SELECT 1 FROM messages WHERE round_no > 0 LIMIT 1").fetchone():
        return  # 已经回填过
    cur: dict[str, int] = {}
    for row in rows:
        sid = row["session_id"]
        if row["kind"] == "brief":
            cur[sid] = 0
            conn.execute("UPDATE messages SET round_no=0 WHERE id=?", (row["id"],))
            continue
        if row["role"] == "user":
            cur[sid] = cur.get(sid, 0) + 1
        conn.execute("UPDATE messages SET round_no=? WHERE id=?", (cur.get(sid, 0), row["id"]))


def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _row(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params: Iterable[Any] = ()) -> None:
    with connect() as conn:
        conn.execute(sql, tuple(params))


def reset_all() -> None:
    """清空业务数据，保留表结构。"""
    tables = [
        "rule_usages",
        "chat_messages",
        "chats",
        "branches",
        "findings",
        "reviews",
        "results",
        "delegations",
        "messages",
        "clarify_state",
        "drafts",
        "sessions",
        "rules",
    ]
    with connect() as conn:
        for t in tables:
            conn.execute(f"DELETE FROM {t}")


# ---------------------------------------------------------------- 备份 / 清历史

# 一次"清空工作台"要删的表。顺序不重要：外键临时关掉，自己按依赖从下往上删。
HISTORY_TABLES = (
    "messages",
    "chat_messages",
    "chats",
    "branches",
    "findings",
    "reviews",
    "results",
    "delegations",
    "clarify_state",
    "sessions",
)
# 清历史时保留的"规则资产"表
KEPT_TABLES = ("rules", "rule_usages", "settings", "drafts")


def backup_db(dest: Optional[Path] = None) -> Path:
    """把当前库热备份成一个文件。

    不能用 shutil.copy：库是 WAL 模式，最近的写入可能还在 app.db-wal 里，
    光拷 .db 会得到一个"少最后一截"的库。走 sqlite3 的 backup API 才是完整快照。
    目标文件已存在时先改名留档，不静默覆盖上一份备份。
    """
    src = db_file()
    target = Path(dest) if dest else src.with_name(src.name + ".bak")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target.replace(target.with_name(f"{target.name}.{stamp}"))

    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(target))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return target


def counts(tables: Iterable[str] = HISTORY_TABLES) -> dict[str, int]:
    with connect() as conn:
        return {
            t: int(conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"])
            for t in tables
        }


def reset_history(keep_backup: bool = True) -> dict:
    """清空工作台历史：会话、消息、委托卡、结果、评审、疑点都删掉。

    保留 rules / rule_usages / settings / drafts —— 规则库和模型配置是资产，
    跟着测试数据一起清掉，等于每次都要重新教它一遍。

    rule_usages 是唯一有外键牵连的表：它挂在 reviews 上。列改成可空 + ON DELETE
    SET NULL 之后，删 reviews 只会把"是哪次评审引用的"置空，"被引用过几次"留下。
    """
    backup_path = backup_db() if keep_backup else None
    before = counts()
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")  # 必须在任何写语句开启事务之前
        detached = conn.execute(
            "UPDATE rule_usages SET review_id=NULL "
            "WHERE review_id IN (SELECT id FROM reviews)"
        ).rowcount
        for table in HISTORY_TABLES:
            conn.execute(f"DELETE FROM {table}")
        # PRAGMA foreign_keys 在事务里是空操作，得先落盘再打开
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
    return {
        "backup": str(backup_path) if backup_path else "",
        "detached_usages": detached,
        "before": before,
        "after": counts(),
    }


# ---------------------------------------------------------------- sessions


def create_session(
    brief: str,
    parent_review_id: Optional[str] = None,
    parent_finding_id: Optional[int] = None,
) -> str:
    sid = new_id("s-")
    ts = now()
    _exec(
        """INSERT INTO sessions (id, brief, stage, created_at, updated_at,
                                 parent_review_id, parent_finding_id)
           VALUES (?,?,?,?,?,?,?)""",
        (sid, brief, "brief", ts, ts, parent_review_id, parent_finding_id),
    )
    return sid


def get_session(sid: str) -> Optional[dict]:
    return _row("SELECT * FROM sessions WHERE id=?", (sid,))


def session_exists(sid: str) -> bool:
    return get_session(sid) is not None


def update_stage(sid: str, stage: str) -> None:
    _exec("UPDATE sessions SET stage=?, updated_at=? WHERE id=?", (stage, now(), sid))


def list_sessions(limit: int = 30, include_aborted: bool = False) -> list[dict]:
    if include_aborted:
        return _rows("SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    return _rows(
        "SELECT * FROM sessions WHERE stage<>'aborted' "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    )


def delete_session(sid: str) -> dict:
    """彻底删掉一个会话及其全部派生数据（像 LLM 里删掉一个话题）。

    规则库、规则引用统计、设置**一律不动**：删的是「这一次工作」，不是用户的偏好。
    外键 CASCADE 会带走 messages / clarify_state / delegations / results /
    reviews / findings / branches；rule_usages 是 ON DELETE SET NULL，只失去归属、
    「被引用过几次」保留。剩下三样没有外键，得手工摘：草稿、子会话的父指针、上传附件。
    """
    import shutil

    removed: dict[str, int] = {}
    with connect() as conn:
        rids = [
            r["id"]
            for r in conn.execute("SELECT id FROM reviews WHERE session_id=?", (sid,))
        ]
        bids: list[str] = []
        if rids:
            marks = ",".join("?" for _ in rids)
            bids = [
                r["id"]
                for r in conn.execute(
                    f"SELECT id FROM branches WHERE review_id IN ({marks})", rids
                )
            ]
        removed["reviews"] = len(rids)
        removed["branches"] = len(bids)
        removed["messages"] = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (sid,)
            ).fetchone()["c"]
        )

        # 草稿里可能有用户写过而没提交的字，不能留在库里
        conn.execute(
            "DELETE FROM drafts WHERE scope='workbench' AND key LIKE ?", (f"%{sid}%",)
        )
        for rid in rids:
            conn.execute(
                "DELETE FROM drafts WHERE scope='workbench' AND key=?", (f"reply:{rid}",)
            )
        for bid in bids:
            conn.execute(
                "DELETE FROM drafts WHERE scope='review_branch' AND key=?", (bid,)
            )

        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        # parent_review_id 没建外键：删完把悬空指针摘掉，页面上不留死链接
        conn.execute(
            "UPDATE sessions SET parent_review_id=NULL, parent_finding_id=NULL "
            "WHERE parent_review_id IS NOT NULL "
            "AND parent_review_id NOT IN (SELECT id FROM reviews)"
        )

    up = db_file().parent / "uploads" / sid
    if up.exists():
        shutil.rmtree(up, ignore_errors=True)
    return removed


def mark_aborted(sid: str) -> None:
    """建模失败留下的会话：标记而不是删除，便于事后查。"""
    _exec("UPDATE sessions SET stage='aborted', updated_at=? WHERE id=?", (now(), sid))


def abort_empty_sessions() -> list[str]:
    """把"只有 brief、没有任何问答"的历史会话标为 aborted。返回被标记的 id。"""
    rows = _rows(
        """SELECT s.id FROM sessions s
           WHERE s.stage NOT IN ('aborted')
             AND (SELECT COUNT(*) FROM messages m
                  WHERE m.session_id=s.id AND m.kind<>'brief') = 0"""
    )
    ids = [r["id"] for r in rows]
    for sid in ids:
        mark_aborted(sid)
    return ids


# ---------------------------------------------------------------- messages


def next_round(session_id: str) -> int:
    row = _row(
        "SELECT COALESCE(MAX(round_no), 0) AS r FROM messages WHERE session_id=?",
        (session_id,),
    )
    return int((row or {}).get("r") or 0) + 1


def add_message(
    session_id: str,
    role: str,
    content: str,
    kind: str = "chat",
    round_no: int = 0,
) -> None:
    _exec(
        """INSERT INTO messages (session_id, role, content, kind, round_no, created_at)
           VALUES (?,?,?,?,?,?)""",
        (session_id, role, content, kind, round_no, now()),
    )


def add_exchange(
    session_id: str,
    round_no: int,
    content: str,
    kind: str = "question",
) -> None:
    """侧车这一侧的一条发言。和用户发言配对，保证 user/assistant 严格交替。"""
    add_message(session_id, "assistant", content, kind=kind, round_no=round_no)


def clear_failures(session_id: str, round_no: int) -> None:
    """重试前撤掉该轮的失败占位，避免同一轮攒下两条失败说明。"""
    _exec(
        "DELETE FROM messages WHERE session_id=? AND round_no=? AND kind='failure'",
        (session_id, round_no),
    )


def list_messages(session_id: str) -> list[dict]:
    """澄清阶段的对话流。

    审阅对话（review_id 非空）是另一条线，故意不并进来：
    两段历史混在一条时间线上，"我现在走到哪一步"就看不出来了。
    """
    return _rows(
        "SELECT * FROM messages WHERE session_id=? AND review_id IS NULL ORDER BY id ASC",
        (session_id,),
    )


# ---------------------------------------------------------------- 审阅对话
# 复用 messages，不另起一张表：一旦分成两张，历史又会以另一个名字"分叉"。

def add_review_message(
    review_id: str,
    session_id: str,
    content: str,
    *,
    role: str = "assistant",
    kind: str = "chat",
    refs: list | None = None,
    branch_id: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (session_id, role, content, kind, round_no, review_id, phase, refs_json,
                branch_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                role,
                content,
                kind,
                0,
                review_id,
                "review",
                json.dumps(refs or [], ensure_ascii=False),
                branch_id,
                now(),
            ),
        )
        return int(cur.lastrowid)


def list_review_messages(
    review_id: str,
    *,
    branch_id: str | None = None,
    with_branches: bool = False,
) -> list[dict]:
    """审阅对话的消息。**默认只给主干**，这是防串台的唯一开关。

    branch_ref 式的平铺设计最危险的地方是"漏一个 WHERE 就串台"，而且不报错、
    只有用户觉得"我没说过这句"时才发现。所以这里反过来：默认主干，
    要看全部必须显式 `with_branches=True`，要看某一支必须点名 `branch_id`。
    """
    if with_branches:
        sql, params = "SELECT * FROM messages WHERE review_id=? ORDER BY id ASC", (review_id,)
    elif branch_id:
        sql, params = (
            "SELECT * FROM messages WHERE review_id=? AND branch_id=? ORDER BY id ASC",
            (review_id, branch_id),
        )
    else:
        sql, params = (
            "SELECT * FROM messages WHERE review_id=? AND branch_id IS NULL ORDER BY id ASC",
            (review_id,),
        )
    rows = _rows(sql, params)
    for row in rows:
        raw = row.pop("refs_json", None) or "[]"
        try:
            row["refs"] = json.loads(raw)
        except json.JSONDecodeError:
            row["refs"] = []
    return rows


def review_message_count(review_id: str) -> int:
    row = _row(
        "SELECT COUNT(*) AS c FROM messages WHERE review_id=? AND branch_id IS NULL",
        (review_id,),
    )
    return int((row or {}).get("c") or 0)


# ---------------------------------------------------------------- 审阅分支
#
# 主干 = 结论 + 原文 + 委托卡，只读；分支 = 一条待办/决策的独立对话沙盒。
# 分支有独立身份（表）而不只是一个标签（列），因为它有生命周期：
# open → merged / dropped，还要存"聊完了的结论"。

BRANCH_STATES = ("open", "merged", "dropped")
BRANCH_TYPES = ("action_item", "decision", "doubt", "risk", "free")


def create_branch(
    review_id: str,
    *,
    kind: str,
    title: str,
    question: str = "",
    finding_id: int | None = None,
    quote: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
) -> str:
    bid = new_id("BR")
    ts = now()
    _exec(
        """INSERT INTO branches
           (id, review_id, finding_id, kind, title, question, quote, char_start, char_end,
            state, resolution, merged_at, msg_count, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,'open','',NULL,0,?,?)""",
        (bid, review_id, finding_id, kind, title, question, quote, char_start, char_end, ts, ts),
    )
    return bid


def get_branch(branch_id: str) -> Optional[dict]:
    return _row("SELECT * FROM branches WHERE id=?", (branch_id,))


def list_branches(review_id: str, state: Optional[str] = None) -> list[dict]:
    """按"待办在前、决策在后、手工分支垫底"的顺序给。"""
    sql = "SELECT * FROM branches WHERE review_id=?"
    params: list[Any] = [review_id]
    if state:
        sql += " AND state=?"
        params.append(state)
    sql += (
        " ORDER BY CASE WHEN finding_id IS NULL THEN 1 ELSE 0 END,"
        " CASE kind WHEN 'action_item' THEN 0 WHEN 'decision' THEN 1 ELSE 2 END,"
        " finding_id, created_at"
    )
    return _rows(sql, params)


def branch_for_finding(review_id: str, finding_id: int) -> Optional[dict]:
    return _row(
        "SELECT * FROM branches WHERE review_id=? AND finding_id=? ORDER BY created_at LIMIT 1",
        (review_id, finding_id),
    )


def update_branch(branch_id: str, **fields) -> None:
    allowed = {"title", "question", "state", "resolution", "merged_at", "msg_count"}
    picks = {k: v for k, v in fields.items() if k in allowed}
    if not picks:
        return
    picks["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in picks)
    _exec(f"UPDATE branches SET {cols} WHERE id=?", (*picks.values(), branch_id))


def bump_branch(branch_id: str) -> None:
    _exec(
        "UPDATE branches SET msg_count=msg_count+1, updated_at=? WHERE id=?",
        (now(), branch_id),
    )


def add_branch_message(
    branch_id: str,
    review_id: str,
    session_id: str,
    content: str,
    *,
    role: str = "assistant",
    kind: str = "chat",
    refs: list | None = None,
) -> int:
    """分支里的一行。走同一条落库路径，只是多带 branch_id —— 顺便把 msg_count 顶上去。"""
    mid = add_review_message(
        review_id, session_id, content, role=role, kind=kind, refs=refs, branch_id=branch_id
    )
    bump_branch(branch_id)
    return mid


def list_branch_messages(branch_id: str, limit: int | None = None) -> list[dict]:
    rows = _rows(
        "SELECT * FROM messages WHERE branch_id=? ORDER BY id ASC", (branch_id,)
    )
    for row in rows:
        raw = row.pop("refs_json", None) or "[]"
        try:
            row["refs"] = json.loads(raw)
        except json.JSONDecodeError:
            row["refs"] = []
    return rows[-limit:] if limit else rows


def materialize_branches(review_id: str) -> list[dict]:
    """把这次的待办/决策物化成分支入口。幂等：同一条 finding 只建一次。

    为什么要物化而不是每次现算：分支要有自己的状态（聊了几轮、合没合并、
    结论是什么），现算就无处存。老评审第一次打开审阅页时自动补齐，不需要迁移脚本。
    """
    made: list[dict] = []
    known = {b["finding_id"] for b in list_branches(review_id) if b["finding_id"]}
    for f in list_findings(review_id):
        if f["kind"] not in ("action_item", "decision") or f["id"] in known:
            continue
        question = (f["why"] if f["kind"] == "decision" else f["detail"]) or f["title"]
        bid = create_branch(
            review_id,
            kind=f["kind"],
            title=f["title"],
            question=question,
            finding_id=f["id"],
            quote=f["quote"],
            char_start=f["char_start"],
            char_end=f["char_end"],
        )
        made.append(get_branch(bid))
    return made


def branch_progress(review_id: str) -> dict:
    """主干卡片上那个"你自己挣来的进度"。"""
    rows = list_branches(review_id)
    total = len(rows)
    merged = sum(1 for b in rows if b["state"] == "merged")
    touched = sum(1 for b in rows if b["msg_count"] > 0)
    return {"total": total, "merged": merged, "touched": touched, "open": total - merged}


def drop_branch(branch_id: str, reason: str = "") -> None:
    """放弃这一支：卡片留在主干上划掉，"处理过什么"本身是信息。"""
    update_branch(branch_id, state="dropped", resolution=reason or "已放弃")


# ---------------------------------------------------------------- clarify


def save_clarify_state(
    session_id: str,
    slots: dict,
    skipped: list,
    round_count: int = 0,
) -> None:
    _exec(
        """INSERT INTO clarify_state (session_id, slots_json, skipped_json, round_count, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
             slots_json=excluded.slots_json,
             skipped_json=excluded.skipped_json,
             round_count=excluded.round_count,
             updated_at=excluded.updated_at""",
        (
            session_id,
            json.dumps(slots, ensure_ascii=False),
            json.dumps(skipped, ensure_ascii=False),
            round_count,
            now(),
        ),
    )


def get_clarify_state(session_id: str) -> dict:
    row = _row("SELECT * FROM clarify_state WHERE session_id=?", (session_id,))
    if not row:
        return {"slots": {}, "skipped": [], "round_count": 0}
    return {
        "slots": json.loads(row["slots_json"] or "{}"),
        "skipped": json.loads(row["skipped_json"] or "[]"),
        "round_count": row["round_count"],
    }


def update_clarify_slots(session_id: str, slots: dict, round_count: Optional[int] = None) -> None:
    state = get_clarify_state(session_id)
    save_clarify_state(
        session_id,
        slots,
        state["skipped"],
        state["round_count"] if round_count is None else round_count,
    )


# ---------------------------------------------------------------- delegations


def next_version(session_id: str) -> int:
    row = _row(
        "SELECT MAX(version) AS v FROM delegations WHERE session_id=?", (session_id,)
    )
    return int((row or {}).get("v") or 0) + 1


def create_delegation(
    session_id: str,
    goal: str,
    deliverable: str,
    boundaries: str,
    forbidden: str,
    success_criteria: list,
    keywords: list,
) -> str:
    did = new_id("d-")
    _exec(
        """INSERT INTO delegations
           (id, session_id, version, goal, deliverable, boundaries, forbidden,
            success_criteria_json, keywords_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            did,
            session_id,
            next_version(session_id),
            goal,
            deliverable,
            boundaries,
            forbidden,
            json.dumps(success_criteria, ensure_ascii=False),
            json.dumps(keywords, ensure_ascii=False),
            now(),
        ),
    )
    return did


def get_delegation(did: str) -> Optional[dict]:
    row = _row("SELECT * FROM delegations WHERE id=?", (did,))
    return _expand_delegation(row)


def latest_delegation(session_id: str) -> Optional[dict]:
    row = _row(
        "SELECT * FROM delegations WHERE session_id=? ORDER BY version DESC LIMIT 1",
        (session_id,),
    )
    return _expand_delegation(row)


def _expand_delegation(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    row = dict(row)
    row["success_criteria"] = json.loads(row.pop("success_criteria_json") or "[]")
    row["keywords"] = json.loads(row.pop("keywords_json") or "[]")
    return row


# ---------------------------------------------------------------- results


def create_result(session_id: str, delegation_id: str, raw_output: str, source: str = "paste") -> str:
    rid = new_id("res-")
    _exec(
        """INSERT INTO results (id, session_id, delegation_id, raw_output, source, char_count, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (rid, session_id, delegation_id, raw_output, source, len(raw_output), now()),
    )
    return rid


def get_result(rid: str) -> Optional[dict]:
    return _row("SELECT * FROM results WHERE id=?", (rid,))


# ---------------------------------------------------------------- reviews


def create_review(**kw) -> str:
    rid = new_id("rv-")
    _exec(
        """INSERT INTO reviews
           (id, session_id, result_id, delegation_id, headline, completion_pct, sub_goals_json,
            certainty, certainty_note, deviation, deviation_note, next_options_json,
            skipped_json, unanchored_count, low_confidence, provider, raw_json,
            status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rid,
            kw["session_id"],
            kw["result_id"],
            kw["delegation_id"],
            kw["headline"],
            kw.get("completion_pct"),
            json.dumps(kw.get("sub_goals", []), ensure_ascii=False),
            kw["certainty"],
            kw.get("certainty_note", ""),
            kw["deviation"],
            kw.get("deviation_note", ""),
            json.dumps(kw.get("next_options", []), ensure_ascii=False),
            json.dumps(kw.get("skipped", []), ensure_ascii=False),
            kw.get("unanchored_count", 0),
            1 if kw.get("low_confidence") else 0,
            kw.get("provider", "mock"),
            json.dumps(kw.get("raw", {}), ensure_ascii=False),
            "pending",
            now(),
        ),
    )
    return rid


def get_review(rid: str) -> Optional[dict]:
    row = _row("SELECT * FROM reviews WHERE id=?", (rid,))
    return _expand_review(row)


def latest_review(session_id: str) -> Optional[dict]:
    row = _row(
        "SELECT * FROM reviews WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (session_id,),
    )
    return _expand_review(row)


def list_reviews(limit: int = 50, session_id: Optional[str] = None) -> list[dict]:
    if session_id:
        rows = _rows(
            "SELECT * FROM reviews WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        )
    else:
        rows = _rows(
            "SELECT * FROM reviews ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )
    return [_expand_review(r) for r in rows]


def decide_review(rid: str, status: str, reject_reason: Optional[str] = None) -> None:
    _exec(
        "UPDATE reviews SET status=?, reject_reason=?, decided_at=? WHERE id=?",
        (status, reject_reason, now(), rid),
    )


def _expand_review(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    row = dict(row)
    for src, dst in (
        ("sub_goals_json", "sub_goals"),
        ("next_options_json", "next_options"),
        ("skipped_json", "skipped"),
        ("raw_json", "raw"),
    ):
        raw = row.pop(src, None)
        row[dst] = json.loads(raw) if raw else []
    return row


# ---------------------------------------------------------------- findings


def add_finding(review_id: str, **kw) -> int:
    return _insert_finding(review_id, kw)


def _insert_finding(review_id: str, kw: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO findings
               (review_id, kind, title, detail, certainty, severity, verdict,
                anchor_type, quote, char_start, char_end, match_type, match_score,
                rule_id, options_json, recommended, why, state, chosen, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                review_id,
                kw.get("kind", "doubt"),
                kw.get("title", ""),
                kw.get("detail", ""),
                kw.get("certainty", "medium"),
                kw.get("severity"),
                kw.get("verdict"),
                kw.get("anchor_type", "none"),
                kw.get("quote"),
                kw.get("char_start"),
                kw.get("char_end"),
                kw.get("match_type"),
                kw.get("match_score"),
                kw.get("rule_id"),
                json.dumps(kw.get("options") or [], ensure_ascii=False),
                kw.get("recommended") or "",
                kw.get("why") or "",
                kw.get("state") or "open",
                kw.get("chosen") or "",
                now(),
            ),
        )
        return int(cur.lastrowid)


def list_findings(review_id: str) -> list[dict]:
    rows = _rows(
        "SELECT * FROM findings WHERE review_id=? ORDER BY id ASC", (review_id,)
    )
    for row in rows:
        raw = row.pop("options_json", None)
        try:
            row["options"] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            row["options"] = []
    return rows


def get_finding(finding_id: int) -> Optional[dict]:
    rows = list_findings_by_id(finding_id)
    return rows[0] if rows else None


def list_findings_by_id(finding_id: int) -> list[dict]:
    row = _row("SELECT * FROM findings WHERE id=?", (finding_id,))
    if not row:
        return []
    raw = row.pop("options_json", None)
    try:
        row["options"] = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        row["options"] = []
    return [row]


def update_finding(finding_id: int, **kw) -> None:
    fields, params = [], []
    for name in ("state", "chosen"):
        if name in kw:
            fields.append(f"{name}=?")
            params.append(kw[name])
    if not fields:
        return
    params.append(finding_id)
    _exec(f"UPDATE findings SET {', '.join(fields)} WHERE id=?", params)


# ---------------------------------------------------------------- 自由对话


def create_chat(title: str = "新对话") -> str:
    cid = new_id("c-")
    ts = now()
    _exec(
        "INSERT INTO chats (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (cid, title, ts, ts),
    )
    return cid


def get_chat(cid: str) -> Optional[dict]:
    return _row("SELECT * FROM chats WHERE id=?", (cid,))


def list_chats(limit: int = 30) -> list[dict]:
    return _rows("SELECT * FROM chats ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,))


def rename_chat(cid: str, title: str) -> None:
    _exec("UPDATE chats SET title=?, updated_at=? WHERE id=?", (title[:40], now(), cid))


def touch_chat(cid: str) -> None:
    _exec("UPDATE chats SET updated_at=? WHERE id=?", (now(), cid))


def delete_chat(cid: str) -> None:
    """删一个自由对话（级联带走消息）。规则库、工作台会话都不受影响。"""
    _exec("DELETE FROM chats WHERE id=?", (cid,))


def add_chat_message(cid: str, role: str, content: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (cid, role, content, now()),
        )
        msg_id = int(cur.lastrowid)
        conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now(), cid))
        chat = conn.execute("SELECT title FROM chats WHERE id=?", (cid,)).fetchone()
        # 第一句用户话就是标题，像 LLM 的话题列表那样自动命名
        if role == "user" and chat and (chat["title"] or "") == "新对话":
            conn.execute(
                "UPDATE chats SET title=? WHERE id=?",
                (content.strip().replace("\n", " ")[:18] or "新对话", cid),
            )
        return msg_id


def list_chat_messages(cid: str, limit: int = 60) -> list[dict]:
    """最近 N 条，但仍按时间正序返回（喂给模型和渲染都要正序）。"""
    return _rows(
        "SELECT * FROM (SELECT * FROM chat_messages WHERE chat_id=? "
        "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (cid, limit),
    )


def chat_message_count(cid: str) -> int:
    row = _row("SELECT COUNT(*) AS c FROM chat_messages WHERE chat_id=?", (cid,))
    return int((row or {}).get("c") or 0)


# ---------------------------------------------------------------- rules


def _next_rule_id() -> str:
    rows = _rows("SELECT id FROM rules")
    mx = 0
    for r in rows:
        try:
            mx = max(mx, int(str(r["id"]).split("-")[1]))
        except (IndexError, ValueError):
            continue
    return f"R-{mx + 1:03d}"


def create_rule(
    text: str,
    rationale: str,
    scope: str,
    keywords: list,
    source: str,
    source_review_id: Optional[str] = None,
    status: str = "active",
    rule_id: Optional[str] = None,
) -> str:
    rid = rule_id or _next_rule_id()
    ts = now()
    _exec(
        """INSERT INTO rules
           (id, text, rationale, scope, keywords_json, source, source_review_id, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            rid,
            text.strip(),
            rationale.strip(),
            scope,
            json.dumps(keywords, ensure_ascii=False),
            source,
            source_review_id,
            status,
            ts,
            ts,
        ),
    )
    return rid


def update_rule(rule_id: str, **kw) -> None:
    fields = []
    params: list[Any] = []
    if "text" in kw:
        fields.append("text=?")
        params.append(kw["text"].strip())
    if "rationale" in kw:
        fields.append("rationale=?")
        params.append(kw["rationale"].strip())
    if "scope" in kw:
        fields.append("scope=?")
        params.append(kw["scope"])
    if "keywords" in kw:
        fields.append("keywords_json=?")
        params.append(json.dumps(kw["keywords"], ensure_ascii=False))
    if "status" in kw:
        fields.append("status=?")
        params.append(kw["status"])
    if not fields:
        return
    fields.append("updated_at=?")
    params.append(now())
    params.append(rule_id)
    _exec(f"UPDATE rules SET {', '.join(fields)} WHERE id=?", params)


def delete_rule(rule_id: str) -> None:
    _exec("DELETE FROM rules WHERE id=?", (rule_id,))


def get_rule(rule_id: str) -> Optional[dict]:
    row = _row("SELECT * FROM rules WHERE id=?", (rule_id,))
    return _expand_rule(row)


def list_rules(status: Optional[str] = None) -> list[dict]:
    if status:
        rows = _rows(
            "SELECT * FROM rules WHERE status=? ORDER BY id ASC", (status,)
        )
    else:
        rows = _rows("SELECT * FROM rules ORDER BY id ASC")
    return [_expand_rule(r) for r in rows]


def rules_map() -> dict:
    return {r["id"]: r for r in list_rules()}


def _expand_rule(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    row = dict(row)
    row["keywords"] = json.loads(row.pop("keywords_json", "[]") or "[]")
    return row


# ---------------------------------------------------------------- usages


def add_usage(rule_id: str, review_id: str, verdict: str, reason: str) -> None:
    _exec(
        """INSERT INTO rule_usages (rule_id, review_id, verdict, reason, created_at)
           VALUES (?,?,?,?,?)""",
        (rule_id, review_id, verdict, reason, now()),
    )


def usages_for_review(review_id: str) -> list[dict]:
    return _rows(
        """SELECT u.*, r.text AS rule_text FROM rule_usages u
           LEFT JOIN rules r ON r.id = u.rule_id
           WHERE u.review_id=? ORDER BY u.id ASC""",
        (review_id,),
    )


def usages_for_rule(rule_id: str, limit: int = 10) -> list[dict]:
    return _rows(
        """SELECT u.*, rv.headline AS review_headline FROM rule_usages u
           LEFT JOIN reviews rv ON rv.id = u.review_id
           WHERE u.rule_id=? ORDER BY u.id DESC LIMIT ?""",
        (rule_id, limit),
    )


def usage_count(rule_id: str) -> int:
    row = _row("SELECT COUNT(*) AS c FROM rule_usages WHERE rule_id=?", (rule_id,))
    return int((row or {}).get("c") or 0)


# ---------------------------------------------------------------- settings


def get_setting(key: str, default: str = "") -> str:
    row = _row("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    _exec(
        """INSERT INTO settings (key, value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, value),
    )


# ---------------------------------------------------------------- drafts
# 草稿必须比 widget 活得久：Streamlit 在控件卸载时会删掉它的 key，
# 切换页面 / 刷新浏览器都会丢，所以落库。


def save_draft(scope: str, key: str, content: str, limit: int = 20000) -> None:
    text = (content or "")[:limit]
    if not text.strip():
        clear_draft(scope, key)
        return
    _exec(
        """INSERT INTO drafts (scope, key, content, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(scope, key) DO UPDATE SET
             content=excluded.content, updated_at=excluded.updated_at""",
        (scope, key, text, now()),
    )


def load_draft(scope: str, key: str) -> str:
    row = _row("SELECT content FROM drafts WHERE scope=? AND key=?", (scope, key))
    return row["content"] if row else ""


def clear_draft(scope: str, key: str) -> None:
    _exec("DELETE FROM drafts WHERE scope=? AND key=?", (scope, key))


def list_drafts(scope: Optional[str] = None) -> list[dict]:
    if scope:
        return _rows("SELECT * FROM drafts WHERE scope=? ORDER BY updated_at DESC", (scope,))
    return _rows("SELECT * FROM drafts ORDER BY updated_at DESC")


def draft_chars(scope: Optional[str] = None) -> int:
    return sum(len(d["content"]) for d in list_drafts(scope))


def clear_drafts(scope: Optional[str] = None) -> None:
    if scope:
        _exec("DELETE FROM drafts WHERE scope=?", (scope,))
    else:
        _exec("DELETE FROM drafts")


# ---------------------------------------------------------------- export


def export_all() -> dict:
    data: dict[str, Any] = {}
    for table in (
        "sessions",
        "messages",
        "clarify_state",
        "delegations",
        "results",
        "reviews",
        "findings",
        "rules",
        "rule_usages",
        "drafts",
    ):
        data[table] = _rows(f"SELECT * FROM {table}")
    return data
