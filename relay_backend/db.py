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
    UNIQUE (session_id, start_slot)
);

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
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建并建表）数据库连接。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


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
