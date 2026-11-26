"""SQLite 数据访问层。

节目时间线由从 0 开始、每格 2 秒的节目格（slot）组成，格号即整数序号。

核心表
------
``sessions``    一场口译会议；工作人员持有 ``staff_token``，重启后仍可管理。
``duties``      一次"当班"（duty）。同一名口译员可多次当班、掉线重连后继续用
                同一个 duty（token 不随连接状态变化），再次当班则产生新 duty。
                上传一律凭 duty token 识别到具体当班，而不是人名。
``submissions`` 某当班对某格提交的音频。每个当班对同一格至多有一条 *active*
                提交（部分唯一索引保证）；因换班失去该格资格时，提交被标记为
                ``disqualified`` 并永久保留，即使之后重新拿回该格也不会自动
                恢复，必须由当班重新明确提交。
``slot_marks``  工作人员对空格的人工裁决。目前只有 ``silence``：一旦记为静音，
                该格封闭，不再接受任何补传。

官方精选表
----------
``edition_reviewers``        每场一名素材审核员的 token（不建账号体系）。
``edition_approvals``        对原节目已发布格区间的批准记录，可重叠、可撤回。
``edition_drafts``           每场一份草稿，``version`` 为已接受的编辑批次数。
``edition_excerpts``         草稿中的一段摘录，引用原节目一段连续格
                             [start_slot, end_slot]；同一段可重复引用、可重排。
                             软删除，旧 id 不复用。
``edition_gaps``             草稿中相邻摘录之间（及首尾边界）的缝隙，边界用
                             NULL 表示。被填充即永久关闭，删空后旧缝隙不会重开。
``edition_releases`` /
``edition_release_entries``  发行版及其冻结的逐格内容（多格摘录展平为多个
                             position）；每场至多一个 live。

发布模型
--------
听众只能看到从格 0 开始、没有缺口的连续前缀（finalized frontier）。前缀中每格
要么有归属当班的 active 音频，要么被工作人员记为静音。前缀之后的格可以先收
音频，但不发布。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_token   TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    slot_seconds  INTEGER NOT NULL CHECK (slot_seconds > 0),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS duties (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    interpreter  TEXT NOT NULL,
    -- 该当班负责区间的起始格（含）。区间结束于下一个当班的 start_slot（不含）。
    start_slot   INTEGER NOT NULL CHECK (start_slot >= 0),
    duty_token   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    -- 跨会场补班换人的班段：旧当班被取代但行保留（凭据与历史提交/核账可追溯）。
    -- 同一 (session, start_slot) 同一时刻至多一个未被取代的当班。
    superseded_at TEXT
);

-- 取代旧库的表级 UNIQUE(session_id, start_slot)：被 superseded 的旧当班保留，
-- 只有当前当班占用换人点。
CREATE UNIQUE INDEX IF NOT EXISTS ux_duties_active_start
    ON duties(session_id, start_slot) WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    duty_id      INTEGER NOT NULL REFERENCES duties(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL CHECK (slot >= 0),
    audio        BLOB NOT NULL,
    -- active：当前有效（只要该 duty 仍负责此格，它就是该格的节目声音）
    -- disqualified：换班/改班后该 duty 不再负责此格，永久失去资格
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'disqualified')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 同一当班对同一格至多一条 active 提交：
--   * 不能用重试替换尚未发布格的声音；
--   * 被取消资格的历史提交保留下来，重新负责该格后需要全新一次明确提交。
CREATE UNIQUE INDEX IF NOT EXISTS ux_submissions_active
    ON submissions(duty_id, slot)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ix_submissions_session_slot
    ON submissions(session_id, slot);

CREATE TABLE IF NOT EXISTS slot_marks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL CHECK (slot >= 0),
    kind         TEXT NOT NULL DEFAULT 'silence' CHECK (kind = 'silence'),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, slot)
);

-- ------------------------------------------------------------------------- --
-- 官方精选（curated edition）
-- ------------------------------------------------------------------------- --

-- 素材审核员的凭据。不建立账号体系，只在建场次时签发一个本场专用的 token。
CREATE TABLE IF NOT EXISTS edition_reviewers (
    session_id     INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    reviewer_token TEXT NOT NULL UNIQUE
);

-- 对原节目已发布格的一段批准。区间可重叠；revoked_at 非空即失效。
-- 一个格只要仍被任一未撤回的批准覆盖就可用。
CREATE TABLE IF NOT EXISTS edition_approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    start_slot   INTEGER NOT NULL CHECK (start_slot >= 0),
    end_slot     INTEGER NOT NULL CHECK (end_slot >= start_slot),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS ix_edition_approvals_session
    ON edition_approvals(session_id, revoked_at);

-- 每场一份草稿；version 是已接受的编辑批次序号（0 = 空草稿）。
CREATE TABLE IF NOT EXISTS edition_drafts (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL DEFAULT 0
);

-- 草稿中的一段摘录（excerpt）：引用原节目里一段连续格 [start_slot, end_slot]。
-- 摘录在草稿里可以重排、可以重复引用同一段原声；每次出现是一行独立的记录，
-- 有稳定的 excerpt id，彼此不能替代。只做软删除，旧 id 永不复用。
CREATE TABLE IF NOT EXISTS edition_excerpts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    start_slot         INTEGER NOT NULL CHECK (start_slot >= 0),
    end_slot           INTEGER NOT NULL CHECK (end_slot >= start_slot),
    created_version    INTEGER NOT NULL,
    removed_version    INTEGER,
    last_touch_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_edition_excerpts_session
    ON edition_excerpts(session_id, removed_version);

-- 草稿的"缝隙"（gap）：相邻两段摘录之间的位置，首尾边界用 NULL 表示。
-- 缝隙同样有稳定 id。填充一个缝隙会永久关闭它；即使后来把填入的摘录删空，
-- 旧缝隙也不会重新打开，旧 id 不能沿用。
CREATE TABLE IF NOT EXISTS edition_gaps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    left_excerpt_id   INTEGER,
    right_excerpt_id  INTEGER,
    open              INTEGER NOT NULL DEFAULT 1 CHECK (open IN (0, 1)),
    created_version   INTEGER NOT NULL,
    closed_version    INTEGER,
    last_touch_version INTEGER NOT NULL
);

-- 每个相邻关系在任一时刻至多一个打开的缝隙；首尾边界各至多一个。
CREATE UNIQUE INDEX IF NOT EXISTS ux_edition_gap_open_internal
    ON edition_gaps(session_id, left_excerpt_id, right_excerpt_id)
    WHERE open = 1 AND left_excerpt_id IS NOT NULL
      AND right_excerpt_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_edition_gap_open_head
    ON edition_gaps(session_id)
    WHERE open = 1 AND left_excerpt_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_edition_gap_open_tail
    ON edition_gaps(session_id)
    WHERE open = 1 AND right_excerpt_id IS NULL;

-- 发行版：把某个草稿版本的摘录顺序与逐格内容冻结下来。每场至多一个 live 版本。
-- 条目按 position 展平（一段多格摘录对应多个 position），逐格记录原格号。
CREATE TABLE IF NOT EXISTS edition_releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    draft_version  INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'live'
                   CHECK (status IN ('live', 'superseded', 'taken_down')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at       TEXT,
    ended_reason   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_edition_release_live
    ON edition_releases(session_id) WHERE status = 'live';

CREATE TABLE IF NOT EXISTS edition_release_entries (
    release_id   INTEGER NOT NULL REFERENCES edition_releases(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL CHECK (position >= 0),
    excerpt_id   INTEGER NOT NULL,
    source_slot  INTEGER NOT NULL CHECK (source_slot >= 0),
    kind         TEXT NOT NULL CHECK (kind IN ('audio', 'silence')),
    PRIMARY KEY (release_id, position)
);

-- ------------------------------------------------------------------------- --
-- 核账、付款单与回执（billing / payment orders / receipts）
-- ------------------------------------------------------------------------- --

-- 每次核账（包括首次和后续更正）产生一个新的、不可变账版。
CREATE TABLE IF NOT EXISTS billing_versions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version            INTEGER NOT NULL CHECK (version > 0),
    base_version       INTEGER NOT NULL DEFAULT 0 CHECK (base_version >= 0),
    cutoff_slot        INTEGER NOT NULL CHECK (cutoff_slot >= 0),
    total_amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_amount_cents >= 0),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, version)
);

-- 账版中提交的计费条目。有声格数和金额在核账时冻结，旧账版不随后续更正改变。
CREATE TABLE IF NOT EXISTS billing_entries (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id             INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version_id             INTEGER NOT NULL REFERENCES billing_versions(id) ON DELETE CASCADE,
    version                INTEGER NOT NULL,
    ordinal                INTEGER NOT NULL DEFAULT 0,
    duty_id                INTEGER NOT NULL REFERENCES duties(id) ON DELETE CASCADE,
    start_slot             INTEGER NOT NULL CHECK (start_slot >= 0),
    end_slot               INTEGER NOT NULL CHECK (end_slot >= start_slot),
    payee_id               TEXT NOT NULL,
    unit_amount_cents      INTEGER NOT NULL CHECK (unit_amount_cents >= 0),
    voiced_slot_count      INTEGER NOT NULL CHECK (voiced_slot_count >= 0),
    amount_cents           INTEGER NOT NULL CHECK (amount_cents >= 0)
);

CREATE INDEX IF NOT EXISTS ix_billing_entries_version
    ON billing_entries(session_id, version);
CREATE INDEX IF NOT EXISTS ix_billing_entries_payee
    ON billing_entries(session_id, version, payee_id);

-- 每个账版条目实际计费的有声格；同时用于不可变明细和审计。
CREATE TABLE IF NOT EXISTS billing_entry_slots (
    entry_id          INTEGER NOT NULL REFERENCES billing_entries(id) ON DELETE CASCADE,
    session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version_id        INTEGER NOT NULL REFERENCES billing_versions(id) ON DELETE CASCADE,
    slot              INTEGER NOT NULL CHECK (slot >= 0),
    duty_id           INTEGER NOT NULL REFERENCES duties(id) ON DELETE CASCADE,
    payee_id          TEXT NOT NULL,
    unit_amount_cents INTEGER NOT NULL CHECK (unit_amount_cents >= 0),
    PRIMARY KEY (entry_id, slot),
    UNIQUE (version_id, slot)
);

-- 一次幂等开单请求对应一张批量付款单；没有正余额时 order_id 可为 NULL，
-- 但请求指纹仍保存，保证同一 request_id 的重试不会在以后意外开单。
CREATE TABLE IF NOT EXISTS payment_idempotency (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    request_key   TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    order_id      INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, request_key)
);

CREATE TABLE IF NOT EXISTS payment_orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    order_no           TEXT NOT NULL UNIQUE,
    request_key        TEXT NOT NULL,
    account_version    INTEGER NOT NULL CHECK (account_version > 0),
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'partially_receipted', 'completed')),
    total_amount_cents INTEGER NOT NULL CHECK (total_amount_cents > 0),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, request_key)
);

CREATE INDEX IF NOT EXISTS ix_payment_orders_session
    ON payment_orders(session_id, id);

-- 批量付款单中的逐条付款指令，每个收款方至多一条。
CREATE TABLE IF NOT EXISTS payment_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    order_id              INTEGER NOT NULL REFERENCES payment_orders(id) ON DELETE CASCADE,
    payee_id              TEXT NOT NULL,
    account_version       INTEGER NOT NULL CHECK (account_version > 0),
    amount_cents          INTEGER NOT NULL CHECK (amount_cents > 0),
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'paid', 'unpaid')),
    external_receipt_no   TEXT,
    receipt_amount_cents  INTEGER CHECK (receipt_amount_cents IS NULL OR receipt_amount_cents >= 0),
    receipted_at          TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (order_id, payee_id)
);

CREATE INDEX IF NOT EXISTS ix_payment_items_payee_status
    ON payment_items(session_id, payee_id, status);
CREATE INDEX IF NOT EXISTS ix_payment_items_order
    ON payment_items(order_id, id);

-- 同一场内，外部已付回执号只能用于一个付款项。
CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_items_paid_receipt_no
    ON payment_items(session_id, external_receipt_no)
    WHERE status = 'paid' AND external_receipt_no IS NOT NULL;

-- ------------------------------------------------------------------------- --
-- 跨会场补班（dispatch groups：多个分会场共用一批口译员）
-- ------------------------------------------------------------------------- --
--
-- 一个调度组登记若干"尚未排班、没有音频/静音记录"的场次；每场只能加入一个组。
-- 组内按人员 ID 管理口译员（语言能力与可用时间窗）。每次成功补班产生一条不可变
-- 的班表修订（dispatch_revisions + dispatch_revision_segments），落实到 duties；
-- 重启后以最后一次成功修订为准。

CREATE TABLE IF NOT EXISTS dispatch_groups (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    dispatcher_token TEXT NOT NULL UNIQUE,
    schedule_version INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 已接入组的场次。主键就是 session_id，保证每场至多加入一个组。
CREATE TABLE IF NOT EXISTS dispatch_group_sessions (
    session_id    INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    group_id      INTEGER NOT NULL REFERENCES dispatch_groups(id) ON DELETE CASCADE,
    -- 场次的绝对开播时间（UTC ISO-8601 文本，含时区）。
    starts_at     TEXT NOT NULL,
    slot_count    INTEGER NOT NULL CHECK (slot_count > 0),
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_dispatch_group_sessions_group
    ON dispatch_group_sessions(group_id);

-- 允许换人的格号（换人点）。0 与 slot_count 是隐含边界；每个换人点唯一。
CREATE TABLE IF NOT EXISTS dispatch_handover_slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    slot       INTEGER NOT NULL CHECK (slot >= 0),
    UNIQUE (session_id, slot)
);

-- 每场登记时提供的所需语言（可多个）；班段人员必须擅长其中至少一种。
CREATE TABLE IF NOT EXISTS dispatch_session_languages (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    language   TEXT NOT NULL,
    PRIMARY KEY (session_id, language)
);

CREATE TABLE IF NOT EXISTS dispatch_interpreters (
    group_id  INTEGER NOT NULL REFERENCES dispatch_groups(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL,
    PRIMARY KEY (group_id, person_id)
);

CREATE TABLE IF NOT EXISTS dispatch_interpreter_languages (
    group_id  INTEGER NOT NULL REFERENCES dispatch_groups(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL,
    language  TEXT NOT NULL,
    PRIMARY KEY (group_id, person_id, language)
);

-- 每人若干可用时间窗；覆盖判定按并集处理，首尾相接即可。
CREATE TABLE IF NOT EXISTS dispatch_availability (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES dispatch_groups(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at   TEXT NOT NULL,
    CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS ix_dispatch_availability_person
    ON dispatch_availability(group_id, person_id);

-- 每次成功落实的班表都是一条不可变修订；同组修订号严格递增。
CREATE TABLE IF NOT EXISTS dispatch_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES dispatch_groups(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL CHECK (version > 0),
    reason     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (group_id, version)
);

-- 修订中每个班段冻结的归属人员。班段身份由 (session_id, start_slot) 标识。
CREATE TABLE IF NOT EXISTS dispatch_revision_segments (
    revision_id   INTEGER NOT NULL REFERENCES dispatch_revisions(id) ON DELETE CASCADE,
    group_id      INTEGER NOT NULL REFERENCES dispatch_groups(id) ON DELETE CASCADE,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_slot    INTEGER NOT NULL CHECK (start_slot >= 0),
    end_slot      INTEGER NOT NULL CHECK (end_slot > start_slot),
    person_id     TEXT NOT NULL,
    duty_id       INTEGER REFERENCES duties(id) ON DELETE SET NULL,
    PRIMARY KEY (revision_id, segment_index)
);

CREATE INDEX IF NOT EXISTS ix_dispatch_revision_segments_group
    ON dispatch_revision_segments(group_id, session_id);
"""


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """旧库升级：把 duties 的表级 UNIQUE 换成支持"当班被取代后保留"的部分唯一索引。

    新库的 SCHEMA 直接带 ``superseded_at`` 列；只有旧库需要这一步表重建。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(duties)").fetchall()}
    if not cols or "superseded_at" in cols:
        return
    # SQLite 不能直接 DROP 表级约束，按官方配方重建表；临时关闭外键并在同一
    # 连接内完成，submissions/billing 等引用的 duty id 保持不变。
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE duties_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                interpreter   TEXT NOT NULL,
                start_slot    INTEGER NOT NULL CHECK (start_slot >= 0),
                duty_token    TEXT NOT NULL UNIQUE,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                superseded_at TEXT
            );
            INSERT INTO duties_new
                (id, session_id, interpreter, start_slot, duty_token, created_at)
                SELECT id, session_id, interpreter, start_slot, duty_token, created_at
                FROM duties;
            DROP TABLE duties;
            ALTER TABLE duties_new RENAME TO duties;
            CREATE UNIQUE INDEX ux_duties_active_start
                ON duties(session_id, start_slot) WHERE superseded_at IS NULL;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建并建表）数据库连接。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _migrate_legacy_schema(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读事务：取一份一致快照，避免多表读取时看到半步更新。

    用 ``BEGIN``（deferred）即可，不抢写锁，因此与正在进行的写下架互不阻塞，
    但读取期间不会有新的提交落进来。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：先抢写锁，避免两个并发请求互相覆盖。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
