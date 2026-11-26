"""FastAPI 应用：口译声道接力后端。

角色与接口
==========
工作人员（``X-Staff-Token``）
  * ``POST   /sessions``                          建场次
  * ``PUT    /sessions/{sid}/duties``             指定当班口译员（含交接格）
  * ``PATCH  /duties/{duty_id}``                  修改未发布时段的换班格
  * ``PUT    /sessions/{sid}/slots/{n}/silence``  把空格明确记为静音
  * ``GET    /sessions/{sid}/host``               主持端：卡在哪一格、原因

口译端（``X-Duty-Token``，识别到具体当班而非人名）
  * ``POST   /duties/{duty_id}/slots/{n}/audio``  上传单格 WAV（原始字节）

听众端（无需鉴权）
  * ``GET    /sessions/{sid}/program``            从指定格读取连续已发布节目
  * ``GET    /sessions/{sid}/slots/{n}/audio``    取某已发布格的声音
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response

from . import db as dbmod
from .wav import WavError, validate_mono_wav

# 防止异常数据导致 frontier 扫描无界增长的安全阀。
_MAX_SCAN_SLOTS = 1_000_000


# --------------------------------------------------------------------------- #
# 应用工厂与基础工具
# --------------------------------------------------------------------------- #


def create_app(db_path: str | Path = "relay.db") -> FastAPI:
    if str(db_path) == ":memory:":
        # 每请求独立连接，内存库无法在连接间共享状态
        raise ValueError("in-memory database is not supported; use a file path")
    app = FastAPI(title="interpretation-relay", version="1.0.0")
    app.state.db_path = str(db_path)
    # 启动时确保库文件与表结构存在
    dbmod.connect(db_path).close()

    def get_conn() -> Iterator[sqlite3.Connection]:
        # 每请求一个连接：写事务用 BEGIN IMMEDIATE 串行化，
        # busy_timeout 让后来的写者等待而不是互相踩同一连接。
        conn = dbmod.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def err(status: int, code: str, message: str) -> HTTPException:
        return HTTPException(status_code=status, detail={"error": code, "message": message})

    def get_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise err(404, "session_not_found", f"session {session_id} does not exist")
        return row

    def require_staff(
        conn: sqlite3.Connection, session_id: int, token: str | None
    ) -> sqlite3.Row:
        session = get_session(conn, session_id)
        if not token or not hmac.compare_digest(token, session["staff_token"]):
            raise err(403, "bad_staff_token", "valid X-Staff-Token required")
        return session

    def get_duty(conn: sqlite3.Connection, duty_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM duties WHERE id=?", (duty_id,)).fetchone()
        if row is None:
            raise err(404, "duty_not_found", f"duty {duty_id} does not exist")
        return row

    def require_reviewer(
        conn: sqlite3.Connection, session_id: int, token: str | None
    ) -> sqlite3.Row:
        get_session(conn, session_id)
        row = conn.execute(
            "SELECT * FROM edition_reviewers WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None or not token or not hmac.compare_digest(
            token, row["reviewer_token"]
        ):
            raise err(403, "bad_reviewer_token", "valid X-Reviewer-Token required")
        return row

    def require_duty_token(row: sqlite3.Row, token: str | None) -> None:
        if not token or not hmac.compare_digest(token, row["duty_token"]):
            raise err(403, "bad_duty_token", "valid X-Duty-Token for this duty required")

    # ------------------------------------------------------------------ #
    # 时间线规则
    # ------------------------------------------------------------------ #

    def list_duties(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM duties WHERE session_id=? ORDER BY start_slot, id",
                (session_id,),
            ).fetchall()
        )

    def owner_of(conn: sqlite3.Connection, session_id: int, slot: int) -> sqlite3.Row | None:
        """某格当前归属的当班（start_slot 最大且 <= slot 的 duty）。"""
        return conn.execute(
            """SELECT * FROM duties
               WHERE session_id=? AND start_slot <= ?
               ORDER BY start_slot DESC, id DESC LIMIT 1""",
            (session_id, slot),
        ).fetchone()

    def qualifying_audio(
        conn: sqlite3.Connection, session_id: int, slot: int
    ) -> sqlite3.Row | None:
        """该格当前归属当班的 active 提交（即能进入节目的声音）。"""
        owner = owner_of(conn, session_id, slot)
        if owner is None:
            return None
        row = conn.execute(
            """SELECT * FROM submissions
               WHERE session_id=? AND slot=? AND duty_id=? AND status='active'
               LIMIT 1""",
            (session_id, slot, owner["id"]),
        ).fetchone()
        return row

    def is_silence(conn: sqlite3.Connection, session_id: int, slot: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM slot_marks WHERE session_id=? AND slot=?",
            (session_id, slot),
        ).fetchone() is not None

    def compute_frontier(conn: sqlite3.Connection, session_id: int) -> int:
        """连续已发布前缀之后的第一格（前缀长度）。

        从格 0 起逐格检查：静音标记或归属当班的 active 音频均可发布；
        遇到第一个两者都没有的格即停止——后面的声音只能先收、不能发布。
        """
        frontier = 0
        while frontier < _MAX_SCAN_SLOTS:
            if is_silence(conn, session_id, frontier):
                frontier += 1
                continue
            if qualifying_audio(conn, session_id, frontier) is not None:
                frontier += 1
                continue
            break
        return frontier

    def blocked_reason(conn: sqlite3.Connection, session_id: int, slot: int) -> dict[str, Any]:
        owner = owner_of(conn, session_id, slot)
        if owner is None:
            return {"reason": "no_duty_covers_slot",
                    "message": f"slot {slot} has no interpreter duty assigned yet"}
        if qualifying_audio(conn, session_id, slot) is not None:
            # 理论上不可达：有合格声音时 frontier 会推进
            return {"reason": "internal", "message": "unexpected state"}
        other = conn.execute(
            """SELECT COUNT(*) AS c FROM submissions
               WHERE session_id=? AND slot=? AND status='active'""",
            (session_id, slot),
        ).fetchone()
        if other["c"]:
            return {"reason": "audio_from_non_owner_duty",
                    "message": f"held audio belongs to a duty no longer responsible for slot {slot}"}
        return {"reason": "waiting_audio",
                "message": f"waiting for duty {owner['id']} ({owner['interpreter']}) "
                           f"to upload slot {slot}"}

    def disqualify_outside(conn: sqlite3.Connection, session_id: int) -> int:
        """换班/改班后，把不再负责相应格的 active 提交全部取消资格。

        被取消资格的记录永久保留；即使原当班之后重新拿回该格，也必须重新
        明确提交，旧声音不会静默恢复。
        """
        n = 0
        for sub in conn.execute(
            "SELECT * FROM submissions WHERE session_id=? AND status='active'",
            (session_id,),
        ).fetchall():
            owner = owner_of(conn, session_id, sub["slot"])
            if owner is None or owner["id"] != sub["duty_id"]:
                conn.execute(
                    "UPDATE submissions SET status='disqualified' WHERE id=?",
                    (sub["id"],),
                )
                n += 1
        return n

    def duty_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "duty_id": row["id"],
            "session_id": row["session_id"],
            "interpreter": row["interpreter"],
            "start_slot": row["start_slot"],
            "duty_token": row["duty_token"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------ #
    # 健康检查 & 场次
    # ------------------------------------------------------------------ #

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def create_session(
        payload: dict[str, Any] | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        payload = payload or {}
        title = str(payload.get("title") or "untitled session")
        slot_seconds = int(payload.get("slot_seconds") or 2)
        if slot_seconds <= 0:
            raise err(400, "bad_slot_seconds", "slot_seconds must be positive")
        token = "staff_" + uuid.uuid4().hex
        reviewer_token = "reviewer_" + uuid.uuid4().hex
        with dbmod.immediate_transaction(conn):
            cur = conn.execute(
                "INSERT INTO sessions (staff_token, title, slot_seconds) VALUES (?, ?, ?)",
                (token, title, slot_seconds),
            )
            sid = cur.lastrowid
            conn.execute(
                "INSERT INTO edition_reviewers (session_id, reviewer_token) VALUES (?, ?)",
                (sid, reviewer_token),
            )
            conn.execute("INSERT INTO edition_drafts (session_id, version) VALUES (?, 0)",
                         (sid,))
        return {"session_id": sid, "title": title, "slot_seconds": slot_seconds,
                "staff_token": token, "reviewer_token": reviewer_token}

    @app.get("/sessions/{session_id}")
    def session_info(
        session_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        row = get_session(conn, session_id)
        frontier = compute_frontier(conn, session_id)
        return {"session_id": row["id"], "title": row["title"],
                "slot_seconds": row["slot_seconds"], "created_at": row["created_at"],
                "frontier": frontier,
                "published_through": frontier - 1 if frontier else None,
                "duties": [{"duty_id": d["id"], "interpreter": d["interpreter"],
                            "start_slot": d["start_slot"]}
                           for d in list_duties(conn, session_id)]}

    # ------------------------------------------------------------------ #
    # 工作人员：当班调度
    # ------------------------------------------------------------------ #

    @app.put("/sessions/{session_id}/duties", status_code=201)
    def assign_duty(
        session_id: int,
        payload: dict[str, Any],
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        interpreter = payload.get("interpreter")
        if not interpreter or not str(interpreter).strip():
            raise err(400, "interpreter_required", "interpreter name is required")
        try:
            start_slot = int(payload["start_slot"])
        except (KeyError, TypeError, ValueError):
            raise err(400, "start_slot_required", "integer start_slot is required")
        if start_slot < 0:
            raise err(400, "bad_start_slot", "start_slot must be >= 0")

        with dbmod.immediate_transaction(conn):
            duties = list_duties(conn, session_id)
            if not duties and start_slot != 0:
                raise err(409, "first_duty_must_start_at_zero",
                          "the first duty of a session must start at slot 0")
            if any(d["start_slot"] == start_slot for d in duties):
                raise err(409, "start_slot_taken",
                          f"a duty already starts at slot {start_slot}")
            token = "duty_" + uuid.uuid4().hex
            cur = conn.execute(
                "INSERT INTO duties (session_id, interpreter, start_slot, duty_token)"
                " VALUES (?, ?, ?, ?)",
                (session_id, str(interpreter).strip(), start_slot, token),
            )
            duty_id = cur.lastrowid
            disqualified = disqualify_outside(conn, session_id)
            frontier = compute_frontier(conn, session_id)
            duty = conn.execute("SELECT * FROM duties WHERE id=?", (duty_id,)).fetchone()
        return {**duty_dict(duty), "frontier": frontier,
                "disqualified_submissions": disqualified}

    @app.patch("/duties/{duty_id}")
    def reschedule_duty(
        duty_id: int,
        payload: dict[str, Any],
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        duty = get_duty(conn, duty_id)
        require_staff(conn, duty["session_id"], x_staff_token)
        try:
            new_start = int(payload["start_slot"])
        except (KeyError, TypeError, ValueError):
            raise err(400, "start_slot_required", "integer start_slot is required")
        if new_start < 0:
            raise err(400, "bad_start_slot", "start_slot must be >= 0")

        with dbmod.immediate_transaction(conn):
            sid = duty["session_id"]
            old_start = duty["start_slot"]
            frontier = compute_frontier(conn, sid)
            duties = list_duties(conn, sid)
            first_start = min(d["start_slot"] for d in duties)

            # 只允许改未发布时段：已经进入连续前缀的部分一律不能动。
            if old_start < frontier:
                raise err(409, "duty_covers_finalized_slots",
                          f"duty {duty_id} starts at {old_start} which is inside the "
                          f"published prefix (frontier={frontier}); reschedule rejected")
            if new_start < frontier:
                raise err(409, "cannot_move_into_finalized_range",
                          f"new start {new_start} is inside the published prefix "
                          f"(frontier={frontier})")
            if old_start == first_start and new_start != first_start:
                raise err(409, "first_duty_must_start_at_zero",
                          "the first duty must keep covering slot 0")
            clash = conn.execute(
                "SELECT id FROM duties WHERE session_id=? AND start_slot=? AND id<>?",
                (sid, new_start, duty_id),
            ).fetchone()
            if clash is not None:
                raise err(409, "start_slot_taken",
                          f"a duty already starts at slot {new_start}")

            conn.execute("UPDATE duties SET start_slot=? WHERE id=?",
                         (new_start, duty_id))
            disqualified = disqualify_outside(conn, sid)
            frontier = compute_frontier(conn, sid)
            duty = conn.execute("SELECT * FROM duties WHERE id=?", (duty_id,)).fetchone()
        return {**duty_dict(duty), "frontier": frontier,
                "disqualified_submissions": disqualified}

    # ------------------------------------------------------------------ #
    # 工作人员：静音裁决 & 主持端视图
    # ------------------------------------------------------------------ #

    @app.put("/sessions/{session_id}/slots/{slot}/silence", status_code=201)
    def mark_silence(
        session_id: int,
        slot: int,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        if slot < 0:
            raise err(400, "bad_slot", "slot must be >= 0")
        with dbmod.immediate_transaction(conn):
            if is_silence(conn, session_id, slot):
                raise err(409, "already_marked_silence",
                          f"slot {slot} is already marked silence and stays closed")
            frontier = compute_frontier(conn, session_id)
            if slot < frontier:
                raise err(409, "slot_finalized",
                          f"slot {slot} is already published and cannot be changed")
            audio = qualifying_audio(conn, session_id, slot)
            if audio is not None:
                raise err(409, "slot_has_audio",
                          f"slot {slot} already holds qualifying audio; silence rejected")
            conn.execute(
                "INSERT INTO slot_marks (session_id, slot, kind) VALUES (?, ?, 'silence')",
                (session_id, slot),
            )
            frontier = compute_frontier(conn, session_id)
        return {"session_id": session_id, "slot": slot, "kind": "silence",
                "frontier": frontier}

    def slot_view(conn: sqlite3.Connection, session_id: int, slot: int,
                  frontier: int) -> dict[str, Any]:
        owner = owner_of(conn, session_id, slot)
        silence = is_silence(conn, session_id, slot)
        active = conn.execute(
            """SELECT s.*, d.interpreter FROM submissions s
               JOIN duties d ON d.id = s.duty_id
               WHERE s.session_id=? AND s.slot=? AND s.status='active'""",
            (session_id, slot),
        ).fetchall()
        disq = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE session_id=? AND slot=? "
            "AND status='disqualified'",
            (session_id, slot),
        ).fetchone()["c"]
        qualifying = qualifying_audio(conn, session_id, slot)

        if slot < frontier:
            state = "silence" if silence else "published"
        elif silence:
            state = "silence"
        elif owner is None:
            state = "not_assigned"
        elif qualifying is not None:
            state = "buffered_ahead"  # 声音已到，但前面有缺口，暂不能发布
        elif disq:
            state = "disqualified_only"  # 只有失去资格的旧声音，必须重新明确提交
        else:
            state = "waiting"

        return {
            "slot": slot,
            "state": state,
            "silence": silence,
            "owner": None if owner is None else {
                "duty_id": owner["id"], "interpreter": owner["interpreter"],
                "start_slot": owner["start_slot"],
            },
            "active_submissions": [
                {"duty_id": r["duty_id"], "interpreter": r["interpreter"],
                 "qualifies_for_program": qualifying is not None
                 and r["id"] == qualifying["id"]}
                for r in active
            ],
            "disqualified_submissions": disq,
        }

    @app.get("/sessions/{session_id}/host")
    def host_view(
        session_id: int,
        start: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=1000),
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        frontier = compute_frontier(conn, session_id)
        blocked = None
        if frontier < _MAX_SCAN_SLOTS:
            blocked = {"slot": frontier, **blocked_reason(conn, session_id, frontier)}
        slots = [slot_view(conn, session_id, s, frontier)
                 for s in range(start, start + limit)]
        return {
            "session_id": session_id,
            "frontier": frontier,
            "published_through": frontier - 1 if frontier else None,
            "blocked": blocked,
            "duties": [duty_dict(d) for d in list_duties(conn, session_id)],
            "slots": slots,
        }

    # ------------------------------------------------------------------ #
    # 口译端：单格音频上传
    # ------------------------------------------------------------------ #

    @app.post("/duties/{duty_id}/slots/{slot}/audio", status_code=201)
    async def upload_audio(
        duty_id: int,
        slot: int,
        request: Request,
        x_duty_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        duty = get_duty(conn, duty_id)
        require_duty_token(duty, x_duty_token)
        if slot < 0:
            raise err(400, "bad_slot", "slot must be >= 0")
        body = await request.body()
        try:
            wav_info = validate_mono_wav(body)
        except WavError as exc:
            raise err(400, "invalid_wav", str(exc))

        with dbmod.immediate_transaction(conn):
            sid = duty["session_id"]
            # 重新读取 duty，避免事务外状态过期
            duty = get_duty(conn, duty_id)
            if is_silence(conn, sid, slot):
                raise err(409, "slot_marked_silence",
                          f"slot {slot} was marked silence; no further upload accepted")
            frontier = compute_frontier(conn, sid)
            if slot < frontier:
                raise err(409, "slot_finalized",
                          f"slot {slot} is already published; backfill/replace rejected")
            owner = owner_of(conn, sid, slot)
            if owner is None or owner["id"] != duty["id"]:
                # 掉线前录好的音频只允许在本当班负责范围内补传；
                # 已不属于本当班的格，声音不能进入节目。
                raise err(409, "outside_duty_range",
                          f"duty {duty_id} is not responsible for slot {slot}")
            existing = conn.execute(
                "SELECT id FROM submissions WHERE duty_id=? AND slot=? AND status='active'",
                (duty_id, slot),
            ).fetchone()
            if existing is not None:
                # 同一班次对同一格（即使尚未发布）也不能用重试更换声音。
                raise err(409, "already_submitted",
                          f"duty {duty_id} already submitted slot {slot}; "
                          "retry with different audio is not allowed")
            cur = conn.execute(
                "INSERT INTO submissions (session_id, duty_id, slot, audio, status)"
                " VALUES (?, ?, ?, ?, 'active')",
                (sid, duty_id, slot, body),
            )
            submission_id = cur.lastrowid
            new_frontier = compute_frontier(conn, sid)
        return {"submission_id": submission_id, "session_id": sid,
                "duty_id": duty_id, "interpreter": duty["interpreter"],
                "slot": slot, "wav": wav_info,
                "published": slot < new_frontier, "frontier": new_frontier}

    # ------------------------------------------------------------------ #
    # 听众端：连续节目读取
    # ------------------------------------------------------------------ #

    @app.get("/sessions/{session_id}/program")
    def read_program(
        session_id: int,
        start: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        get_session(conn, session_id)  # 404 if missing
        frontier = compute_frontier(conn, session_id)
        end = min(frontier, start + limit)
        entries: list[dict[str, Any]] = []
        for slot in range(start, end):
            if is_silence(conn, session_id, slot):
                entries.append({"slot": slot, "kind": "silence", "silence": True})
                continue
            row = qualifying_audio(conn, session_id, slot)
            if row is None:
                # 不可达：frontier 内每格必有静音或合格音频
                continue
            duty = conn.execute("SELECT * FROM duties WHERE id=?",
                                (row["duty_id"],)).fetchone()
            entries.append({
                "slot": slot,
                "kind": "audio",
                "silence": False,
                "duty_id": row["duty_id"],
                "interpreter": duty["interpreter"],
                "audio_url": f"/sessions/{session_id}/slots/{slot}/audio",
            })
        blocked = None
        if frontier < _MAX_SCAN_SLOTS:
            blocked = {"slot": frontier, **blocked_reason(conn, session_id, frontier)}
        return {
            "session_id": session_id,
            "start": start,
            "limit": limit,
            "frontier": frontier,
            "published_through": frontier - 1 if frontier else None,
            "blocked": blocked,
            "entries": entries,
            "next_start": end,
        }

    @app.get("/sessions/{session_id}/slots/{slot}/audio")
    def read_slot_audio(
        session_id: int,
        slot: int,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        get_session(conn, session_id)
        if slot < 0:
            raise err(400, "bad_slot", "slot must be >= 0")
        frontier = compute_frontier(conn, session_id)
        if slot >= frontier:
            raise err(404, "not_published",
                      f"slot {slot} is not part of the continuous published program")
        if is_silence(conn, session_id, slot):
            raise err(404, "slot_is_silence", f"slot {slot} was marked silence")
        row = qualifying_audio(conn, session_id, slot)
        if row is None:  # 不可达
            raise err(404, "not_published", "no audio for this slot")
        return Response(content=row["audio"], media_type="audio/wav",
                        headers={"X-Interpreter": str(
                            conn.execute("SELECT interpreter FROM duties WHERE id=?",
                                         (row["duty_id"],)).fetchone()["interpreter"])})

    # ------------------------------------------------------------------ #
    # 官方精选：草稿、发行、回听
    # ------------------------------------------------------------------ #
    #
    # 草稿是若干"摘录"（excerpt）的有序串。每段摘录引用原节目里一段连续格
    # [start_slot, end_slot]，可以重排、可以重复引用同一段原声；每次出现是一
    # 行独立记录，有稳定的 excerpt id，彼此不能替代。
    #
    # 相邻摘录之间（以及首尾边界）是"缝隙"（gap），同样有稳定 id。缝隙一旦被
    # 填入就永久关闭：即使后来把填入的摘录删空，旧缝隙也不会重新打开，旧 id
    # 不能再沿用。
    #
    # 编辑冲突一律按稳定 id 与"上次被改动版本"判定，而不是按位置或内容：
    #   * 别处插入/删除造成的位置移动不算冲突；
    #   * 引用同一段原声的两次出现互相独立；
    #   * 删换要检查被选中的摘录及其内部相邻关系是否在 base 之后被改过；
    #   * 插入要检查缝隙本身（含首尾边界）是否在 base 之后被改过。
    #
    # 一批编辑整批生效或整批拒绝，冲突时逐项报告哪一项失效。

    def _draft_version(conn: sqlite3.Connection, session_id: int) -> int:
        """当前草稿版本号；建场次时已插入，查不到时按空草稿 0 处理。"""
        row = conn.execute(
            "SELECT version FROM edition_drafts WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["version"] if row is not None else 0

    def _ensure_draft(conn: sqlite3.Connection, session_id: int) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO edition_drafts (session_id, version) VALUES (?, 0)",
            (session_id,),
        )
        return _draft_version(conn, session_id)

    def _open_gaps(conn: sqlite3.Connection,
                   session_id: int) -> dict[int, sqlite3.Row]:
        rows = conn.execute(
            "SELECT * FROM edition_gaps WHERE session_id=? AND open=1 ORDER BY id",
            (session_id,),
        ).fetchall()
        return {r["id"]: r for r in rows}

    def _alive_excerpts(conn: sqlite3.Connection,
                        session_id: int) -> dict[int, sqlite3.Row]:
        rows = conn.execute(
            "SELECT * FROM edition_excerpts WHERE session_id=? "
            "AND removed_version IS NULL ORDER BY id",
            (session_id,),
        ).fetchall()
        return {r["id"]: r for r in rows}

    def _order_from_gaps(excerpts: dict[int, sqlite3.Row],
                         gaps: dict[int, sqlite3.Row]) -> list[int]:
        """沿开放缝隙走出草稿顺序。首缝 left IS NULL，尾缝 right IS NULL。"""
        by_left = {g["left_excerpt_id"]: g for g in gaps.values()}
        head = by_left.get(None)
        if head is None:
            return []
        order: list[int] = []
        current = head["right_excerpt_id"]
        while current is not None and current in excerpts and current not in order:
            order.append(current)
            gap = by_left.get(current)
            if gap is None:
                break
            current = gap["right_excerpt_id"]
        return order

    def _find_gap(gaps: dict[int, sqlite3.Row], left: int | None,
                  right: int | None) -> sqlite3.Row | None:
        for gap in gaps.values():
            if gap["left_excerpt_id"] == left and gap["right_excerpt_id"] == right:
                return gap
        return None

    def _ensure_gap(conn: sqlite3.Connection, session_id: int, left: int | None,
                    right: int | None, version: int) -> None:
        row = conn.execute(
            "SELECT 1 FROM edition_gaps WHERE session_id=? AND open=1 "
            "AND left_excerpt_id IS ? AND right_excerpt_id IS ?",
            (session_id, left, right),
        ).fetchone()
        if row is not None:
            return
        conn.execute(
            "INSERT INTO edition_gaps (session_id, left_excerpt_id, "
            "right_excerpt_id, open, created_version, last_touch_version) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (session_id, left, right, version, version),
        )

    def _close_gap(conn: sqlite3.Connection, gap_id: int, version: int) -> None:
        conn.execute(
            "UPDATE edition_gaps SET open=0, closed_version=?, last_touch_version=? "
            "WHERE id=? AND open=1",
            (version, version, gap_id),
        )

    def _ensure_all_gaps(conn: sqlite3.Connection, session_id: int,
                         version: int) -> None:
        """保证草稿每个相邻关系（含首尾边界）都有一个开放缝隙。

        空草稿也有一个 ``(None, None)`` 的首尾缝隙，供第一次插入使用。
        """
        excerpts = _alive_excerpts(conn, session_id)
        order = _order_from_gaps(excerpts, _open_gaps(conn, session_id))
        left: int | None = None
        for position in range(len(order) + 1):
            right = order[position] if position < len(order) else None
            _ensure_gap(conn, session_id, left, right, version)
            left = right

    def _is_approved(conn: sqlite3.Connection, session_id: int, slot: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM edition_approvals WHERE session_id=? "
            "AND revoked_at IS NULL AND start_slot<=? AND end_slot>=? LIMIT 1",
            (session_id, slot, slot),
        ).fetchone() is not None

    def _approval_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "approval_id": row["id"],
            "start_slot": row["start_slot"],
            "end_slot": row["end_slot"],
            "active": row["revoked_at"] is None,
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    def _live_release(conn: sqlite3.Connection,
                      session_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM edition_releases WHERE session_id=? AND status='live'",
            (session_id,),
        ).fetchone()

    def _release_entries(conn: sqlite3.Connection,
                         release_id: int) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM edition_release_entries WHERE release_id=? "
            "ORDER BY position", (release_id,),
        ).fetchall())

    def _take_down_unapproved(conn: sqlite3.Connection,
                              session_id: int) -> int | None:
        """live 版任一引用格失去批准覆盖时整版下架；返回被下架的 release id。"""
        release = _live_release(conn, session_id)
        if release is None:
            return None
        entries = _release_entries(conn, release["id"])
        if all(_is_approved(conn, session_id, e["source_slot"]) for e in entries):
            return None
        conn.execute(
            "UPDATE edition_releases SET status='taken_down', "
            "ended_at=datetime('now'), ended_reason='approval_withdrawn' WHERE id=?",
            (release["id"],),
        )
        return release["id"]

    def _release_dict(conn: sqlite3.Connection, release: sqlite3.Row,
                      include_entries: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "release_id": release["id"],
            "session_id": release["session_id"],
            "draft_version": release["draft_version"],
            "status": release["status"],
            "created_at": release["created_at"],
            "ended_at": release["ended_at"],
            "ended_reason": release["ended_reason"],
        }
        if include_entries:
            data["entries"] = [
                {
                    "position": e["position"],
                    "source_slot": e["source_slot"],
                    "kind": e["kind"],
                    "silence": e["kind"] == "silence",
                    "audio_url": (None if e["kind"] == "silence" else
                                  f"/sessions/{release['session_id']}/curated/"
                                  f"slots/{e['position']}/audio"),
                }
                for e in _release_entries(conn, release["id"])
            ]
        return data

    def _excerpt_dict(conn: sqlite3.Connection, session_id: int, row: sqlite3.Row,
                      position: int) -> dict[str, Any]:
        slots = list(range(row["start_slot"], row["end_slot"] + 1))
        slot_info = []
        for slot in slots:
            silence = is_silence(conn, session_id, slot)
            slot_info.append({
                "slot": slot,
                "kind": "silence" if silence else "audio",
                "silence": silence,
                "approved": _is_approved(conn, session_id, slot),
                "audio_url": (None if silence else
                              f"/sessions/{session_id}/slots/{slot}/audio"),
            })
        return {
            "excerpt_id": row["id"],
            "position": position,
            "start_slot": row["start_slot"],
            "end_slot": row["end_slot"],
            "approved": all(s["approved"] for s in slot_info),
            "slots": slot_info,
        }

    def _draft_dict(conn: sqlite3.Connection, session_id: int,
                    ensure_gaps: bool = True) -> dict[str, Any]:
        version = _draft_version(conn, session_id)
        if ensure_gaps:
            _ensure_all_gaps(conn, session_id, version)
        excerpts = _alive_excerpts(conn, session_id)
        gaps = _open_gaps(conn, session_id)
        order = _order_from_gaps(excerpts, gaps)
        gap_rows: list[dict[str, Any]] = []
        for position in range(len(order) + 1):
            left = order[position - 1] if position > 0 else None
            right = order[position] if position < len(order) else None
            gap = _find_gap(gaps, left, right)
            if gap is not None:
                gap_rows.append({
                    "gap_id": gap["id"],
                    "position": position,
                    "left_excerpt_id": left,
                    "right_excerpt_id": right,
                })
        release = _live_release(conn, session_id)
        return {
            "session_id": session_id,
            "version": version,
            "excerpts": [
                _excerpt_dict(conn, session_id, excerpts[excerpt_id], position)
                for position, excerpt_id in enumerate(order)
            ],
            "gaps": gap_rows,
            "live_release_id": release["id"] if release else None,
        }

    def _apply_batch(conn: sqlite3.Connection, session_id: int,
                     payload: dict[str, Any]) -> dict[str, Any]:
        try:
            base = int(payload["version"])
        except (KeyError, TypeError, ValueError):
            raise err(400, "version_required",
                      "integer draft version the edit is based on is required")
        ops = payload.get("operations")
        if not isinstance(ops, list) or not ops:
            raise err(400, "operations_required",
                      "a non-empty operations list is required")

        with dbmod.immediate_transaction(conn):
            get_session(conn, session_id)
            current = _ensure_draft(conn, session_id)
            if base < 0 or base > current:
                raise err(400, "unknown_base_version",
                          f"base version {base} is not a known draft version "
                          f"(current={current})")
            _ensure_all_gaps(conn, session_id, current)
            frontier = compute_frontier(conn, session_id)
            excerpts = _alive_excerpts(conn, session_id)
            gaps = _open_gaps(conn, session_id)
            order = _order_from_gaps(excerpts, gaps)
            pos = {excerpt_id: i for i, excerpt_id in enumerate(order)}

            failures: dict[int, dict[str, Any]] = {}

            def fail(index: int, code: str, message: str) -> None:
                failures.setdefault(index, {"operation": index, "error": code,
                                            "message": message})

            def as_int(value: Any) -> int | None:
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
                return value

            def span_of(op: dict[str, Any], index: int,
                        optional: bool) -> tuple[int, int] | None | bool:
                """解析一段连续原格；返回 (start, end)、None（合法地空）或 False。"""
                start = op.get("start_slot")
                end = op.get("end_slot")
                if start is None and end is None and optional:
                    return None
                start_i = as_int(start)
                end_i = as_int(end) if end is not None else start_i
                if start_i is None or end_i is None:
                    fail(index, "slot_range_required",
                         "start_slot and end_slot (integers) are required")
                    return False
                if start_i < 0 or end_i < start_i:
                    fail(index, "bad_slot_range",
                         "require 0 <= start_slot <= end_slot")
                    return False
                if end_i >= frontier:
                    fail(index, "slot_range_not_published",
                         f"slots {start_i}..{end_i} must be inside the published "
                         f"program (frontier={frontier})")
                    return False
                return start_i, end_i

            inserts: list[tuple[int, sqlite3.Row, tuple[int, int] | None]] = []
            replaces: list[tuple[int, list[int], tuple[int, int] | None]] = []
            selected_excerpts: dict[int, int] = {}
            selected_gaps: set[int] = set()
            run_excerpts: set[int] = set()

            for index, op in enumerate(ops):
                if not isinstance(op, dict):
                    fail(index, "bad_operation", "each operation must be an object")
                    continue
                kind = op.get("kind")
                if kind == "insert":
                    gap_id = as_int(op.get("gap_id"))
                    if gap_id is None:
                        fail(index, "gap_id_required", "integer gap_id is required")
                        continue
                    span = span_of(op, index, optional=False)
                    if span is False:
                        continue
                    gap = gaps.get(gap_id)
                    if gap is None:
                        fail(index, "gap_unavailable",
                             f"gap {gap_id} is not open in the current draft "
                             "(a filled gap stays closed for good)")
                        continue
                    if gap_id in selected_gaps:
                        fail(index, "duplicate_gap_in_batch",
                             f"gap {gap_id} is filled by another operation in "
                             "this batch")
                        continue
                    selected_gaps.add(gap_id)
                    inserts.append((index, gap, span))
                elif kind == "replace":
                    span = span_of(op, index, optional=True)
                    if span is False:
                        continue
                    run = op.get("excerpt_ids")
                    if not isinstance(run, list) or not run:
                        fail(index, "excerpt_ids_required",
                             "replace needs a non-empty excerpt_ids list")
                        continue
                    run_ids: list[int] = []
                    bad = False
                    for value in run:
                        excerpt_id = as_int(value)
                        if excerpt_id is None:
                            fail(index, "bad_excerpt_id",
                                 "excerpt ids must be integers")
                            bad = True
                            break
                        if excerpt_id in selected_excerpts:
                            fail(index, "duplicate_excerpt_in_batch",
                                 f"excerpt {excerpt_id} is selected by another "
                                 "operation in this batch")
                            bad = True
                            break
                        if excerpt_id not in excerpts:
                            fail(index, "excerpt_unavailable",
                                 f"excerpt {excerpt_id} is not in the current draft")
                            bad = True
                            break
                        selected_excerpts[excerpt_id] = index
                        run_ids.append(excerpt_id)
                    if bad:
                        continue
                    run_positions = [pos[i] for i in run_ids]
                    if run_positions != list(range(run_positions[0],
                                                   run_positions[0] + len(run_ids))):
                        fail(index, "run_not_contiguous",
                             "excerpt_ids must be one contiguous run in draft order")
                        continue
                    run_excerpts.update(run_ids)
                    replaces.append((index, run_ids, span))
                else:
                    fail(index, "unknown_operation_kind",
                         "kind must be 'insert' or 'replace'")

            # 同批：不能在要删换的摘录旁插入（缝隙任一端属于本批删换的摘录）。
            for index, gap, _span in inserts:
                if (gap["left_excerpt_id"] in run_excerpts
                        or gap["right_excerpt_id"] in run_excerpts):
                    fail(index, "insert_beside_replaced_in_batch",
                         f"gap {gap['id']} touches an excerpt replaced by "
                         "another operation in this batch")

            # 与其它已接受提交的冲突：只看被选中的摘录和相邻关系本身。
            # 插入要校验缝隙本身（含首尾边界）及其两端摘录是否在 base 之后
            # 被改过；任何一端被改动，这个相邻关系就算冲突。
            for index, gap, _span in inserts:
                if gap["last_touch_version"] > base:
                    fail(index, "gap_changed",
                         f"gap {gap['id']} was changed after draft version {base}")
                    continue
                touched_ends = [
                    end_id for end_id in (gap["left_excerpt_id"],
                                          gap["right_excerpt_id"])
                    if end_id is not None
                    and excerpts.get(end_id) is not None
                    and excerpts[end_id]["last_touch_version"] > base
                ]
                if touched_ends:
                    fail(index, "gap_neighbour_changed",
                         f"excerpt(s) {touched_ends} at the ends of gap "
                         f"{gap['id']} were changed after draft version {base}")
            for index, run_ids, _span in replaces:
                stale = [i for i in run_ids
                         if excerpts[i]["last_touch_version"] > base]
                if stale:
                    fail(index, "excerpt_changed",
                         f"excerpt(s) {stale} were changed after "
                         f"draft version {base}")
                    continue
                for left, right in zip(run_ids, run_ids[1:]):
                    gap = _find_gap(gaps, left, right)
                    if gap is None or gap["last_touch_version"] > base:
                        fail(index, "internal_adjacency_changed",
                             f"adjacency {left}->{right} inside the replaced run "
                             f"was changed after draft version {base}")
                        break

            if failures:
                # 整批一起拒绝；按操作下标稳定排序报告哪一项失效。
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "batch_rejected",
                        "message": "no operation was applied; see 'operations' "
                                   "for the operations that failed",
                        "operations": [failures[i] for i in sorted(failures)],
                    },
                )

            # ---- 整批应用：逐项按稳定 id 定位 ----
            new_version = current + 1

            def new_excerpt(start: int, end: int) -> int:
                cur = conn.execute(
                    "INSERT INTO edition_excerpts (session_id, start_slot, end_slot, "
                    "created_version, last_touch_version) VALUES (?, ?, ?, ?, ?)",
                    (session_id, start, end, new_version, new_version),
                )
                return cur.lastrowid

            for _index, gap, span in inserts:
                assert span is not None
                _close_gap(conn, gap["id"], new_version)
                left = gap["left_excerpt_id"]
                right = gap["right_excerpt_id"]
                excerpt_id = new_excerpt(*span)
                _ensure_gap(conn, session_id, left, excerpt_id, new_version)
                _ensure_gap(conn, session_id, excerpt_id, right, new_version)

            for _index, run_ids, span in replaces:
                # 重新求当前顺序：本批其它操作的目标互不相邻，两端邻居稳定。
                live_excerpts = _alive_excerpts(conn, session_id)
                live_order = _order_from_gaps(live_excerpts,
                                              _open_gaps(conn, session_id))
                first_pos = live_order.index(run_ids[0])
                last_pos = live_order.index(run_ids[-1])
                left = live_order[first_pos - 1] if first_pos > 0 else None
                right = (live_order[last_pos + 1]
                         if last_pos + 1 < len(live_order) else None)
                for excerpt_id in run_ids:
                    conn.execute(
                        "UPDATE edition_excerpts SET removed_version=?, "
                        "last_touch_version=? WHERE id=?",
                        (new_version, new_version, excerpt_id),
                    )
                # 关闭这段摘录相关的所有开放缝隙（内部与两端）。
                for gap in _open_gaps(conn, session_id).values():
                    if (gap["left_excerpt_id"] in run_ids
                            or gap["right_excerpt_id"] in run_ids):
                        _close_gap(conn, gap["id"], new_version)
                if span is not None:
                    excerpt_id = new_excerpt(*span)
                    _ensure_gap(conn, session_id, left, excerpt_id, new_version)
                    _ensure_gap(conn, session_id, excerpt_id, right, new_version)
                else:
                    _ensure_gap(conn, session_id, left, right, new_version)

            conn.execute("UPDATE edition_drafts SET version=? WHERE session_id=?",
                         (new_version, session_id))
            _ensure_all_gaps(conn, session_id, new_version)
            return {"session_id": session_id, "version": new_version,
                    "applied_operations": len(ops)}

    # ---- 工作人员：草稿读写 ------------------------------------------- #

    def _gaps_complete(conn: sqlite3.Connection, session_id: int) -> bool:
        """草稿里每个相邻关系（含首尾边界）是否都已有开放缝隙。"""
        excerpts = _alive_excerpts(conn, session_id)
        order = _order_from_gaps(excerpts, _open_gaps(conn, session_id))
        if len(order) != len(excerpts):
            return False
        gaps = _open_gaps(conn, session_id)
        pairs = {(g["left_excerpt_id"], g["right_excerpt_id"]) for g in gaps.values()}
        return all(
            (order[i - 1] if i > 0 else None,
             order[i] if i < len(order) else None) in pairs
            for i in range(len(order) + 1)
        )

    @app.get("/sessions/{session_id}/curated/draft")
    def read_curated_draft(
        session_id: int,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        # 正常情况下缝隙齐全（每次接受编辑后都会补齐），此时纯读、不抢写锁；
        # 只有旧库/空草稿初次读取才补一次缝隙（不推进草稿版本）。
        if _gaps_complete(conn, session_id):
            # 缝隙齐全时是纯读：只读事务不会因为写锁而阻塞，也不会写下任何东西。
            with dbmod.read_transaction(conn):
                return _draft_dict(conn, session_id, ensure_gaps=False)
        with dbmod.immediate_transaction(conn):
            return _draft_dict(conn, session_id)

    @app.post("/sessions/{session_id}/curated/draft/edits")
    def edit_curated_draft(
        session_id: int,
        payload: dict[str, Any],
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        return _apply_batch(conn, session_id, payload)

    # ---- 素材审核员：凭据 / 批准 / 撤回 ------------------------------- #

    @app.get("/sessions/{session_id}/curated/reviewer")
    def get_reviewer_token(
        session_id: int,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        """工作人员取回本场审核员凭据（旧库中尚未签发时补发一次）。"""
        require_staff(conn, session_id, x_staff_token)
        row = conn.execute(
            "SELECT reviewer_token FROM edition_reviewers WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is not None:
            return {"session_id": session_id, "reviewer_token": row["reviewer_token"]}
        with dbmod.immediate_transaction(conn):
            row = conn.execute(
                "SELECT reviewer_token FROM edition_reviewers WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                token = "reviewer_" + uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO edition_reviewers (session_id, reviewer_token) "
                    "VALUES (?, ?)", (session_id, token),
                )
            else:
                token = row["reviewer_token"]
            return {"session_id": session_id, "reviewer_token": token}

    @app.get("/sessions/{session_id}/curated/approvals")
    def list_approvals(
        session_id: int,
        x_staff_token: str | None = Header(default=None),
        x_reviewer_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        get_session(conn, session_id)
        if x_staff_token:
            require_staff(conn, session_id, x_staff_token)
        else:
            require_reviewer(conn, session_id, x_reviewer_token)
        rows = conn.execute(
            "SELECT * FROM edition_approvals WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return {"session_id": session_id,
                "approvals": [_approval_dict(r) for r in rows]}

    @app.post("/sessions/{session_id}/curated/approvals", status_code=201)
    def create_approval(
        session_id: int,
        payload: dict[str, Any],
        x_reviewer_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_reviewer(conn, session_id, x_reviewer_token)
        try:
            start = int(payload["start_slot"])
            end = int(payload.get("end_slot", start))
        except (KeyError, TypeError, ValueError):
            raise err(400, "slot_range_required",
                      "integer start_slot (and optional end_slot) required")
        if start < 0 or end < start:
            raise err(400, "bad_slot_range", "require 0 <= start_slot <= end_slot")
        with dbmod.immediate_transaction(conn):
            frontier = compute_frontier(conn, session_id)
            if end >= frontier:
                raise err(409, "slots_not_published",
                          f"approval range {start}..{end} must be fully inside "
                          f"the published program (frontier={frontier})")
            cur = conn.execute(
                "INSERT INTO edition_approvals (session_id, start_slot, end_slot) "
                "VALUES (?, ?, ?)", (session_id, start, end),
            )
            row = conn.execute("SELECT * FROM edition_approvals WHERE id=?",
                               (cur.lastrowid,)).fetchone()
            return _approval_dict(row)

    @app.delete("/sessions/{session_id}/curated/approvals/{approval_id}")
    def revoke_approval(
        session_id: int,
        approval_id: int,
        x_reviewer_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_reviewer(conn, session_id, x_reviewer_token)
        with dbmod.immediate_transaction(conn):
            row = conn.execute(
                "SELECT * FROM edition_approvals WHERE id=? AND session_id=?",
                (approval_id, session_id),
            ).fetchone()
            if row is None:
                raise err(404, "approval_not_found",
                          f"approval {approval_id} does not exist")
            if row["revoked_at"] is not None:
                raise err(409, "already_revoked",
                          f"approval {approval_id} was already revoked")
            conn.execute(
                "UPDATE edition_approvals SET revoked_at=datetime('now') WHERE id=?",
                (approval_id,),
            )
            taken_down = _take_down_unapproved(conn, session_id)
            row = conn.execute("SELECT * FROM edition_approvals WHERE id=?",
                               (approval_id,)).fetchone()
            return {"approval": _approval_dict(row),
                    "taken_down_release_id": taken_down}

    # ---- 工作人员：发行 ----------------------------------------------- #

    @app.post("/sessions/{session_id}/curated/releases", status_code=201)
    def create_release(
        session_id: int,
        payload: dict[str, Any],
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        try:
            version = int(payload["version"])
        except (KeyError, TypeError, ValueError):
            raise err(400, "version_required",
                      "integer draft version to release is required")
        with dbmod.immediate_transaction(conn):
            current = _draft_version(conn, session_id)
            if version != current:
                raise err(409, "stale_draft_version",
                          f"draft is at version {current}; only the current "
                          "version can be released")
            excerpts = _alive_excerpts(conn, session_id)
            order = _order_from_gaps(excerpts, _open_gaps(conn, session_id))
            if not order:
                raise err(409, "draft_empty",
                          "the current draft has no excerpts to release")

            # 展平成逐格条目：一段多格摘录对应多个 position。
            entries: list[tuple[int, int]] = []  # (excerpt_id, source_slot)
            for excerpt_id in order:
                row = excerpts[excerpt_id]
                for slot in range(row["start_slot"], row["end_slot"] + 1):
                    entries.append((excerpt_id, slot))
            missing = sorted({slot for _eid, slot in entries
                              if not _is_approved(conn, session_id, slot)})
            if missing:
                raise err(409, "missing_approval",
                          [{"source_slot": s} for s in missing])

            conn.execute(
                "UPDATE edition_releases SET status='superseded', "
                "ended_at=datetime('now'), ended_reason='superseded' "
                "WHERE session_id=? AND status='live'", (session_id,),
            )
            cur = conn.execute(
                "INSERT INTO edition_releases (session_id, draft_version) "
                "VALUES (?, ?)", (session_id, version),
            )
            release_id = cur.lastrowid
            for position, (excerpt_id, slot) in enumerate(entries):
                kind = "silence" if is_silence(conn, session_id, slot) else "audio"
                conn.execute(
                    "INSERT INTO edition_release_entries (release_id, position, "
                    "excerpt_id, source_slot, kind) VALUES (?, ?, ?, ?, ?)",
                    (release_id, position, excerpt_id, slot, kind),
                )
            release = conn.execute(
                "SELECT * FROM edition_releases WHERE id=?", (release_id,)
            ).fetchone()
            return _release_dict(conn, release, include_entries=True)

    def _public_live(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
        get_session(conn, session_id)
        release = _live_release(conn, session_id)
        if release is None:
            raise err(404, "no_live_release",
                      "this session has no released curated edition")
        return release

    @app.get("/sessions/{session_id}/curated")
    def read_curated(
        session_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        with dbmod.read_transaction(conn):
            release = _public_live(conn, session_id)
            return _release_dict(conn, release, include_entries=True)

    @app.get("/sessions/{session_id}/curated/slots/{position}/audio")
    def read_curated_audio(
        session_id: int,
        position: int,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        with dbmod.read_transaction(conn):
            release = _public_live(conn, session_id)
            entry = conn.execute(
                "SELECT * FROM edition_release_entries WHERE release_id=? "
                "AND position=?", (release["id"], position),
            ).fetchone()
            if entry is None:
                raise err(404, "position_not_in_release",
                          f"position {position} is not part of the live release")
            if entry["kind"] == "silence":
                raise err(404, "entry_is_silence",
                          f"position {position} refers to silence at source slot "
                          f"{entry['source_slot']}")
            row = qualifying_audio(conn, session_id, entry["source_slot"])
            if row is None:
                raise err(404, "source_not_available",
                          f"source slot {entry['source_slot']} has no audio")
            return Response(content=row["audio"], media_type="audio/wav",
                            headers={"X-Source-Slot": str(entry["source_slot"])})

    # ------------------------------------------------------------------ #
    # 核账、付款单与回执
    # ------------------------------------------------------------------ #

    def _pick(data: Any, *names: str) -> Any:
        if not isinstance(data, dict):
            return None
        for name in names:
            if name in data and data[name] is not None:
                return data[name]
        return None

    def _required_int(value: Any, field: str, *, minimum: int = 0) -> int:
        if value is None:
            raise err(400, "field_required", f"{field} is required")
        if isinstance(value, bool):
            raise err(400, "invalid_integer", f"{field} must be an integer")
        if isinstance(value, int):
            number = value
        elif isinstance(value, float) and value.is_integer():
            number = int(value)
        elif isinstance(value, str):
            try:
                number = int(value.strip())
            except ValueError:
                raise err(400, "invalid_integer", f"{field} must be an integer")
        else:
            raise err(400, "invalid_integer", f"{field} must be an integer")
        if number < minimum:
            raise err(400, "integer_out_of_range",
                      f"{field} must be >= {minimum}")
        return number

    def _payee_id(value: Any) -> str:
        if value is None:
            raise err(400, "payee_id_required", "payee identifier is required")
        payee = str(value).strip()
        if not payee:
            raise err(400, "payee_id_required", "payee identifier is required")
        return payee

    def _receipt_no(value: Any) -> str:
        if value is None:
            raise err(400, "receipt_no_required",
                      "paid receipt requires an external receipt number")
        number = str(value).strip()
        if not number:
            raise err(400, "receipt_no_required",
                      "paid receipt requires an external receipt number")
        return number

    def _content_hash(payload: Any) -> str:
        id_keys = {
            "request_id", "idempotency_key", "request_key",
            "idempotency-request-id", "x-request-id",
        }

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    k: clean(v) for k, v in value.items()
                    if str(k).lower() not in id_keys
                }
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        encoded = json.dumps(
            clean(payload), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def _json_body(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            return {}
        return {} if body is None else body

    def _current_billing_row(
        conn: sqlite3.Connection, session_id: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM billing_versions WHERE session_id=? "
            "ORDER BY version DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    def _billing_version_dict(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        current_version: int | None = None,
        include_slots: bool = True,
    ) -> dict[str, Any]:
        session_id = row["session_id"]
        if current_version is None:
            current_row = _current_billing_row(conn, session_id)
            current_version = current_row["version"] if current_row else 0

        entry_rows = conn.execute(
            "SELECT * FROM billing_entries WHERE version_id=? "
            "ORDER BY ordinal, id",
            (row["id"],),
        ).fetchall()
        entries: list[dict[str, Any]] = []
        duty_totals: dict[int, dict[str, Any]] = {}
        payee_totals: dict[str, dict[str, Any]] = {}

        for entry_row in entry_rows:
            billed_slots: list[int] = []
            if include_slots:
                billed_slots = [
                    r["slot"] for r in conn.execute(
                        "SELECT slot FROM billing_entry_slots "
                        "WHERE entry_id=? ORDER BY slot",
                        (entry_row["id"],),
                    ).fetchall()
                ]
            entry = dict(entry_row)
            entry.update({
                "billing_entry_id": entry_row["id"],
                "entry_id": entry_row["id"],
                "id": entry_row["id"],
                "payee": entry_row["payee_id"],
                "unit_amount": entry_row["unit_amount_cents"],
                "amount_per_slot_cents": entry_row["unit_amount_cents"],
                "rate_cents": entry_row["unit_amount_cents"],
                "voiced_slots": billed_slots,
                "billed_slots": billed_slots,
                "slots": billed_slots,
                "slot_count": entry_row["voiced_slot_count"],
                "billable_slot_count": entry_row["voiced_slot_count"],
                "line_amount_cents": entry_row["amount_cents"],
                "amount": entry_row["amount_cents"],
            })
            entries.append(entry)

            duty_id = entry_row["duty_id"]
            duty_agg = duty_totals.setdefault(duty_id, {
                "duty_id": duty_id,
                "voiced_slot_count": 0,
                "slot_count": 0,
                "amount_cents": 0,
                "entries": [],
            })
            duty_agg["voiced_slot_count"] += entry_row["voiced_slot_count"]
            duty_agg["slot_count"] += entry_row["voiced_slot_count"]
            duty_agg["amount_cents"] += entry_row["amount_cents"]
            duty_agg["entries"].append(entry)

            payee = entry_row["payee_id"]
            payee_agg = payee_totals.setdefault(payee, {
                "payee_id": payee,
                "payee": payee,
                "voiced_slot_count": 0,
                "slot_count": 0,
                "amount_cents": 0,
                "entries": [],
            })
            payee_agg["voiced_slot_count"] += entry_row["voiced_slot_count"]
            payee_agg["slot_count"] += entry_row["voiced_slot_count"]
            payee_agg["amount_cents"] += entry_row["amount_cents"]
            payee_agg["entries"].append(entry)

        duties: list[dict[str, Any]] = []
        for duty_id in sorted(duty_totals):
            agg = duty_totals[duty_id]
            duty = conn.execute(
                "SELECT interpreter FROM duties WHERE id=?", (duty_id,)
            ).fetchone()
            agg["interpreter"] = duty["interpreter"] if duty else None
            duties.append(agg)

        payees = [payee_totals[key] for key in sorted(payee_totals)]
        total_count = sum(e["voiced_slot_count"] for e in entries)
        total_amount = sum(e["amount_cents"] for e in entries)

        return {
            "billing_version_id": row["id"],
            "id": row["id"],
            "session_id": session_id,
            "version": row["version"],
            "account_version": row["version"],
            "base_version": row["base_version"],
            "cutoff_slot": row["cutoff_slot"],
            "cutoff": row["cutoff_slot"],
            "through_slot": row["cutoff_slot"],
            "end_slot": row["cutoff_slot"],
            "exclusive_end_slot": row["cutoff_slot"],
            "created_at": row["created_at"],
            "is_current": row["version"] == current_version,
            "current": row["version"] == current_version,
            "entries": entries,
            "billing_entries": entries,
            "line_items": entries,
            "duties": duties,
            "duty_summaries": duties,
            "payees": payees,
            "payee_summaries": payees,
            "voiced_slot_count": total_count,
            "total_voiced_slot_count": total_count,
            "total_count": total_count,
            "amount_cents": total_amount,
            "total_amount_cents": total_amount,
            "total_amount": total_amount,
        }

    def _create_billing_version(
        conn: sqlite3.Connection,
        session_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current_row = _current_billing_row(conn, session_id)
        current_version = current_row["version"] if current_row else 0

        cutoff = _required_int(
            _pick(payload, "cutoff_slot", "settlement_cutoff_slot",
                  "up_to_slot", "upto_slot", "as_of_slot", "to_slot",
                  "cutoff", "through_slot", "until_slot",
                  "exclusive_end_slot", "end_slot"),
            "cutoff_slot",
        )
        base_raw = _pick(
            payload, "base_version", "based_on_version", "based_on", "base",
            "parent_version", "previous_version", "expected_version",
            "account_version", "billing_version", "version",
        )
        base_version = current_version if base_raw is None else _required_int(
            base_raw, "base_version"
        )
        if base_version != current_version:
            raise err(
                409, "stale_billing_version",
                f"this session is at billing version {current_version}; "
                f"submission was based on {base_version}",
            )

        frontier = compute_frontier(conn, session_id)
        if cutoff > frontier:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "cutoff_beyond_published_range",
                    "message": f"billing cutoff {cutoff} is beyond the "
                               f"continuous published prefix (frontier={frontier})",
                    "cutoff_slot": cutoff,
                    "frontier": frontier,
                },
            )
        if current_row is not None and cutoff < current_row["cutoff_slot"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "billing_cutoff_cannot_shrink",
                    "message": "a corrected audit must not omit the previously "
                               "audited tail",
                    "cutoff_slot": cutoff,
                    "previous_cutoff_slot": current_row["cutoff_slot"],
                },
            )

        raw_entries = _pick(
            payload, "entries", "billing_entries", "billing_items",
            "items", "rates", "charges", "lines",
        )
        if raw_entries is None:
            raw_entries = []
        if not isinstance(raw_entries, list):
            raise err(400, "entries_must_be_list",
                      "billing entries must be a list")

        parsed: list[dict[str, Any]] = []
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, dict):
                raise err(400, "invalid_billing_entry",
                          f"billing entry {index} must be an object")
            duty_id = _required_int(
                _pick(raw_entry, "duty_id", "duty"),
                f"entries[{index}].duty_id", minimum=1,
            )
            duty = conn.execute(
                "SELECT * FROM duties WHERE id=?", (duty_id,)
            ).fetchone()
            if duty is None or duty["session_id"] != session_id:
                raise err(400, "duty_not_in_session",
                          f"duty {duty_id} does not belong to session "
                          f"{session_id}")

            range_value = _pick(raw_entry, "slot_range", "range", "interval")
            range_start = range_end = None
            if isinstance(range_value, (list, tuple)) and len(range_value) == 2:
                range_start, range_end = range_value
            start_slot = _required_int(
                _pick(raw_entry, "start_slot", "slot_start", "first_slot",
                      "from_slot", "since_slot", "start")
                if _pick(raw_entry, "start_slot", "slot_start", "first_slot",
                         "from_slot", "since_slot", "start") is not None
                else range_start,
                f"entries[{index}].start_slot",
            )
            end_slot = _required_int(
                _pick(raw_entry, "end_slot", "slot_end", "stop_slot",
                      "until_slot", "exclusive_end_slot", "end")
                if _pick(raw_entry, "end_slot", "slot_end", "stop_slot",
                         "until_slot", "exclusive_end_slot", "end") is not None
                else range_end,
                f"entries[{index}].end_slot",
            )
            if end_slot < start_slot:
                raise err(400, "bad_slot_range",
                          f"entries[{index}] requires start_slot <= end_slot")

            payee_id = _payee_id(_pick(
                raw_entry, "payee_id", "payee", "recipient_id",
                "recipient", "payee_identifier",
            ))
            unit_amount = _required_int(
                _pick(raw_entry, "unit_amount_cents", "amount_per_slot_cents",
                      "cents_per_slot", "rate_cents", "unit_price_cents",
                      "unit_cents", "price_cents", "cents",
                      "unit_amount", "amount_per_slot", "rate", "amount_cents"),
                f"entries[{index}].unit_amount_cents",
            )
            parsed.append({
                "ordinal": index,
                "duty_id": duty_id,
                "start_slot": start_slot,
                "end_slot": end_slot,
                "payee_id": payee_id,
                "unit_amount_cents": unit_amount,
                "slots": [],
            })

        mismatches: list[dict[str, Any]] = []
        for slot in range(cutoff):
            if is_silence(conn, session_id, slot):
                continue
            audio = qualifying_audio(conn, session_id, slot)
            if audio is None:
                mismatches.append({
                    "slot": slot,
                    "reason": "published_voice_not_found",
                })
                continue
            actual_duty_id = audio["duty_id"]
            matches = [
                index for index, entry in enumerate(parsed)
                if entry["duty_id"] == actual_duty_id
                and entry["start_slot"] <= slot < entry["end_slot"]
            ]
            if len(matches) != 1:
                mismatches.append({
                    "slot": slot,
                    "reason": "billing_entry_not_unique_match",
                    "actual_duty_id": actual_duty_id,
                    "matched_entry_indexes": matches,
                })
            else:
                parsed[matches[0]]["slots"].append(slot)

        if mismatches:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "billing_entries_do_not_match_program",
                    "message": "every published voiced slot must match exactly "
                               "one entry for the duty that recorded it",
                    "issues": mismatches,
                },
            )

        total_amount = sum(
            len(entry["slots"]) * entry["unit_amount_cents"]
            for entry in parsed
        )
        next_version = current_version + 1
        cursor = conn.execute(
            "INSERT INTO billing_versions "
            "(session_id, version, base_version, cutoff_slot, total_amount_cents) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, next_version, base_version, cutoff, total_amount),
        )
        version_id = cursor.lastrowid
        for entry in parsed:
            count = len(entry["slots"])
            amount = count * entry["unit_amount_cents"]
            entry_cursor = conn.execute(
                "INSERT INTO billing_entries "
                "(session_id, version_id, version, ordinal, duty_id, "
                "start_slot, end_slot, payee_id, unit_amount_cents, "
                "voiced_slot_count, amount_cents) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, version_id, next_version, entry["ordinal"],
                 entry["duty_id"], entry["start_slot"], entry["end_slot"],
                 entry["payee_id"], entry["unit_amount_cents"], count, amount),
            )
            entry_id = entry_cursor.lastrowid
            conn.executemany(
                "INSERT INTO billing_entry_slots "
                "(entry_id, session_id, version_id, slot, duty_id, payee_id, "
                "unit_amount_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (entry_id, session_id, version_id, slot,
                     entry["duty_id"], entry["payee_id"],
                     entry["unit_amount_cents"])
                    for slot in entry["slots"]
                ],
            )

        version_row = conn.execute(
            "SELECT * FROM billing_versions WHERE id=?", (version_id,)
        ).fetchone()
        return _billing_version_dict(
            conn, version_row, current_version=next_version
        )

    def _payee_balances(
        conn: sqlite3.Connection,
        session_id: int,
        current_version: int | None = None,
    ) -> list[dict[str, Any]]:
        if current_version is None:
            current_row = _current_billing_row(conn, session_id)
            current_version = current_row["version"] if current_row else 0

        payable_rows = conn.execute(
            "SELECT payee_id, COALESCE(SUM(amount_cents), 0) AS amount "
            "FROM billing_entries WHERE session_id=? AND version=? "
            "GROUP BY payee_id",
            (session_id, current_version),
        ).fetchall()
        paid_rows = conn.execute(
            "SELECT payee_id, COALESCE(SUM(amount_cents), 0) AS amount "
            "FROM payment_items WHERE session_id=? AND status='paid' "
            "GROUP BY payee_id",
            (session_id,),
        ).fetchall()
        occupied_rows = conn.execute(
            "SELECT payee_id, COALESCE(SUM(amount_cents), 0) AS amount "
            "FROM payment_items WHERE session_id=? AND status='pending' "
            "GROUP BY payee_id",
            (session_id,),
        ).fetchall()

        payable = {r["payee_id"]: int(r["amount"]) for r in payable_rows}
        paid = {r["payee_id"]: int(r["amount"]) for r in paid_rows}
        occupied = {r["payee_id"]: int(r["amount"]) for r in occupied_rows}
        payees = sorted(set(payable) | set(paid) | set(occupied))

        result: list[dict[str, Any]] = []
        for payee in payees:
            payable_amount = payable.get(payee, 0)
            paid_amount = paid.get(payee, 0)
            occupied_amount = occupied.get(payee, 0)
            balance = payable_amount - paid_amount - occupied_amount
            openable = max(0, balance)
            result.append({
                "payee_id": payee,
                "payee": payee,
                "current_payable_cents": payable_amount,
                "payable_cents": payable_amount,
                "payable": payable_amount,
                "paid_cents": paid_amount,
                "paid": paid_amount,
                "occupied_cents": occupied_amount,
                "occupied": occupied_amount,
                "reserved_cents": occupied_amount,
                "held_cents": occupied_amount,
                "pending_receipt_cents": occupied_amount,
                "pending_cents": occupied_amount,
                "pending_amount_cents": occupied_amount,
                "balance_cents": balance,
                "balance": balance,
                "remaining_balance_cents": balance,
                "remaining_payable_cents": balance,
                "net_balance_cents": balance,
                "available_cents": balance,
                "available_balance_cents": balance,
                "openable_amount_cents": openable,
                "can_open_amount_cents": openable,
            })
        return result

    def _payment_item_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.update({
            "payment_item_id": row["id"],
            "item_id": row["id"],
            "instruction_id": row["id"],
            "payment_id": row["id"],
            "instruction_no": f"PI-{row['id']:08d}",
            "payment_instruction_no": f"PI-{row['id']:08d}",
            "payee": row["payee_id"],
            "instructed_amount_cents": row["amount_cents"],
            "instruction_amount_cents": row["amount_cents"],
            "amount": row["amount_cents"],
            "receipt_no": row["external_receipt_no"],
            "external_receipt_id": row["external_receipt_no"],
            "paid_amount_cents": row["receipt_amount_cents"],
            "is_pending": row["status"] == "pending",
            "is_paid": row["status"] == "paid",
            "is_unpaid": row["status"] == "unpaid",
            "pending_receipt": row["status"] == "pending",
        })
        return item

    def _payment_order_dict(
        conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        item_rows = conn.execute(
            "SELECT * FROM payment_items WHERE order_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
        items = [_payment_item_dict(item_row) for item_row in item_rows]
        pending_amount = sum(
            i["amount_cents"] for i in items if i["status"] == "pending"
        )
        paid_amount = sum(
            i["amount_cents"] for i in items if i["status"] == "paid"
        )
        unpaid_amount = sum(
            i["amount_cents"] for i in items if i["status"] == "unpaid"
        )
        if pending_amount == 0:
            status = "completed"
        elif pending_amount == row["total_amount_cents"] and not paid_amount \
                and not unpaid_amount:
            status = "pending"
        else:
            status = "partially_receipted"

        order = dict(row)
        order.update({
            "payment_order_id": row["id"],
            "order_id": row["id"],
            "id": row["id"],
            "payment_order_no": row["order_no"],
            "order_number": row["order_no"],
            "order_no": row["order_no"],
            "request_id": row["request_key"],
            "idempotency_key": row["request_key"],
            "billing_version": row["account_version"],
            "version": row["account_version"],
            "status": status,
            "stored_status": row["status"],
            "items": items,
            "payment_items": items,
            "instructions": items,
            "payment_instructions": items,
            "transfers": items,
            "payments": items,
            "item_count": len(items),
            "pending_count": sum(i["status"] == "pending" for i in items),
            "paid_count": sum(i["status"] == "paid" for i in items),
            "unpaid_count": sum(i["status"] == "unpaid" for i in items),
            "pending_amount_cents": pending_amount,
            "occupied_cents": pending_amount,
            "paid_amount_cents": paid_amount,
            "unpaid_amount_cents": unpaid_amount,
            "amount_cents": row["total_amount_cents"],
            "total_amount": row["total_amount_cents"],
        })
        return order

    def _wrapped_order(order: dict[str, Any]) -> dict[str, Any]:
        return {
            **order,
            "payment_order": order,
            "order": order,
            "payment_orders": [order],
            "orders": [order],
        }

    def _no_payment_order_body(
        session_id: int,
        request_key: str,
        *,
        replayed: bool,
        reason: str = "no_positive_balance",
    ) -> dict[str, Any]:
        data = {
            "session_id": session_id,
            "payment_order_id": None,
            "order_id": None,
            "id": None,
            "order_no": None,
            "request_id": request_key,
            "idempotency_key": request_key,
            "created": False,
            "replayed": replayed,
            "reason": reason,
            "items": [],
            "payment_items": [],
            "instructions": [],
            "payment_instructions": [],
            "transfers": [],
            "payments": [],
            "payment_orders": [],
            "orders": [],
            "total_amount_cents": 0,
        }
        return {**data, "payment_order": None, "order": None}

    def _normalize_payee_selection(raw_payees: Any) -> list[str]:
        if raw_payees is None:
            return []
        if not isinstance(raw_payees, list):
            raise err(400, "payee_ids_must_be_list",
                      "payee_ids must be a list")
        payees: list[str] = []
        seen: set[str] = set()
        for raw in raw_payees:
            if isinstance(raw, dict):
                payee = _payee_id(_pick(
                    raw, "payee_id", "payee", "recipient_id", "recipient"
                ))
            else:
                payee = _payee_id(raw)
            if payee not in seen:
                seen.add(payee)
                payees.append(payee)
        return payees

    def _create_payment_order(
        conn: sqlite3.Connection,
        session_id: int,
        payload: Any,
        header_keys: list[str | None],
    ) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            body: dict[str, Any] = {}
            if isinstance(payload, list):
                body["payee_ids"] = payload
        else:
            body = payload

        request_key = _pick(
            body, "request_id", "idempotency_key", "request_key", "key"
        )
        if request_key is None:
            request_key = next((key for key in header_keys if key), None)
        if not request_key:
            raise err(400, "request_id_required",
                      "a request_id or Idempotency-Key is required")
        request_key = str(request_key)
        request_hash = _content_hash(payload)

        existing = conn.execute(
            "SELECT * FROM payment_idempotency "
            "WHERE session_id=? AND request_key=?",
            (session_id, request_key),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(existing["request_hash"], request_hash):
                raise err(
                    409, "idempotency_key_content_conflict",
                    f"request_id {request_key} was already used with different "
                    "content",
                )
            if existing["order_id"] is None:
                return _no_payment_order_body(
                    session_id, request_key, replayed=True
                ), 200
            order_row = conn.execute(
                "SELECT * FROM payment_orders WHERE id=? AND session_id=?",
                (existing["order_id"], session_id),
            ).fetchone()
            if order_row is None:
                raise err(500, "idempotent_order_missing",
                          "stored idempotency record points to a missing order")
            order = _payment_order_dict(conn, order_row)
            order["created"] = False
            order["replayed"] = True
            return _wrapped_order(order), 200

        current_row = _current_billing_row(conn, session_id)
        current_version = current_row["version"] if current_row else 0
        if current_version == 0:
            raise err(409, "no_current_billing_version",
                      "submit a billing version before creating payment orders")

        requested_version_raw = _pick(
            body, "account_version", "billing_version", "ledger_version",
            "account_revision", "version", "base_version",
        )
        requested_version = (
            current_version if requested_version_raw is None
            else _required_int(requested_version_raw, "account_version", minimum=0)
        )
        if requested_version != current_version:
            raise err(
                409, "stale_account_version",
                f"account is at version {current_version}; payment request "
                f"used version {requested_version}",
            )

        raw_payees = _pick(
            body, "payee_ids", "selected_payee_ids", "payees",
            "selected_payees", "recipients",
        )
        if raw_payees is None:
            singular_payee = _pick(
                body, "payee_id", "payee", "recipient_id", "recipient"
            )
            raw_payees = [singular_payee] if singular_payee is not None else None
        balances = _payee_balances(conn, session_id, current_version)
        balance_by_payee = {b["payee_id"]: b for b in balances}
        if raw_payees is None:
            selected = [
                b["payee_id"] for b in balances if b["balance_cents"] > 0
            ]
        else:
            selected = _normalize_payee_selection(raw_payees)

        positive_items = [
            (payee, int(balance_by_payee.get(payee, {}).get(
                "balance_cents", 0)))
            for payee in selected
        ]
        positive_items = [(p, amount) for p, amount in positive_items
                           if amount > 0]

        if not positive_items:
            conn.execute(
                "INSERT INTO payment_idempotency "
                "(session_id, request_key, request_hash, order_id) "
                "VALUES (?, ?, ?, NULL)",
                (session_id, request_key, request_hash),
            )
            return _no_payment_order_body(
                session_id, request_key, replayed=False
            ), 200

        total_amount = sum(amount for _p, amount in positive_items)
        order_cursor = conn.execute(
            "INSERT INTO payment_orders "
            "(session_id, order_no, request_key, account_version, status, "
            "total_amount_cents) VALUES (?, ?, ?, ?, 'pending', ?)",
            (session_id, "PENDING_ORDER_NO", request_key,
             current_version, total_amount),
        )
        order_id = order_cursor.lastrowid
        order_no = f"PO-{order_id:08d}"
        conn.execute(
            "UPDATE payment_orders SET order_no=? WHERE id=?",
            (order_no, order_id),
        )
        for payee, amount in positive_items:
            conn.execute(
                "INSERT INTO payment_items "
                "(session_id, order_id, payee_id, account_version, "
                "amount_cents, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (session_id, order_id, payee, current_version, amount),
            )
        conn.execute(
            "INSERT INTO payment_idempotency "
            "(session_id, request_key, request_hash, order_id) "
            "VALUES (?, ?, ?, ?)",
            (session_id, request_key, request_hash, order_id),
        )
        order_row = conn.execute(
            "SELECT * FROM payment_orders WHERE id=?", (order_id,)
        ).fetchone()
        order = _payment_order_dict(conn, order_row)
        order["created"] = True
        order["replayed"] = False
        return _wrapped_order(order), 201

    def _receipt_outcome(raw: dict[str, Any]) -> str:
        value = _pick(raw, "status", "result", "outcome", "state",
                      "receipt_status")
        if value is None and "confirmed" in raw:
            if raw["confirmed"] is True:
                return "paid"
            if raw["confirmed"] is False:
                return "unpaid"
        if value is None:
            raise err(400, "receipt_status_required",
                      "each receipt must be paid or unpaid")
        status = str(value).strip().lower().replace("-", "_")
        paid_values = {
            "paid", "confirmed_paid", "success", "succeeded", "successful",
            "received", "complete", "completed", "已付", "确认已付",
        }
        unpaid_values = {
            "unpaid", "confirmed_unpaid", "not_paid", "failed", "failure",
            "returned", "bounced", "rejected", "未付", "确认未付",
        }
        pending_values = {"pending", "unknown", "undecided", "待回执", "尚无定论"}
        if status in paid_values:
            return "paid"
        if status in unpaid_values:
            return "unpaid"
        if status in pending_values:
            return "pending"
        raise err(400, "invalid_receipt_status",
                  f"unsupported receipt status {value!r}")

    def _update_order_status(
        conn: sqlite3.Connection, order_id: int
    ) -> None:
        counts = conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,"
            "SUM(CASE WHEN status='paid' THEN amount_cents ELSE 0 END) AS paid_amount,"
            "SUM(CASE WHEN status='unpaid' THEN amount_cents ELSE 0 END) AS unpaid_amount,"
            "COUNT(*) AS item_count "
            "FROM payment_items WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if counts["pending_count"]:
            if counts["paid_amount"] or counts["unpaid_amount"]:
                status = "partially_receipted"
            else:
                status = "pending"
        else:
            status = "completed"
        conn.execute(
            "UPDATE payment_orders SET status=? WHERE id=?",
            (status, order_id),
        )

    def _resolve_order_id(
        conn: sqlite3.Connection, session_id: int, order_ref: str | int
    ) -> int:
        ref = str(order_ref)
        row = None
        if ref.isdigit():
            row = conn.execute(
                "SELECT id FROM payment_orders WHERE id=? AND session_id=?",
                (int(ref), session_id),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM payment_orders "
                "WHERE order_no=? AND session_id=?",
                (ref, session_id),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM payment_orders "
                "WHERE request_key=? AND session_id=?",
                (ref, session_id),
            ).fetchone()
        if row is None:
            raise err(404, "payment_order_not_found",
                      f"payment order {order_ref} does not exist")
        return int(row["id"])

    def _register_receipts(
        conn: sqlite3.Connection,
        session_id: int,
        raw_receipts: Any,
        order_id: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw_receipts, list) or not raw_receipts:
            raise err(400, "receipts_required",
                      "a non-empty list of receipt results is required")

        target_order: sqlite3.Row | None = None
        if order_id is not None:
            target_order = conn.execute(
                "SELECT * FROM payment_orders WHERE id=? AND session_id=?",
                (order_id, session_id),
            ).fetchone()
            if target_order is None:
                raise err(404, "payment_order_not_found",
                          f"payment order {order_id} does not exist")

        item_rows = conn.execute(
            "SELECT * FROM payment_items WHERE session_id=?",
            (session_id,),
        ).fetchall()
        items_by_id = {row["id"]: row for row in item_rows}
        decisions: dict[int, tuple[str, str | None, int | None]] = {}
        referenced_order_ids: set[int] = set()

        for raw in raw_receipts:
            if not isinstance(raw, dict):
                raise err(400, "invalid_receipt",
                          "each receipt must be an object")
            item_id = _required_int(
                _pick(raw, "payment_item_id", "item_id", "instruction_id",
                      "payment_instruction_id", "payment_id", "id"),
                "payment_item_id", minimum=1,
            )
            item = items_by_id.get(item_id)
            if item is None:
                raise err(404, "payment_item_not_found",
                          f"payment item {item_id} does not exist")
            if order_id is not None and item["order_id"] != order_id:
                raise err(404, "payment_item_not_in_order",
                          f"payment item {item_id} is not in order {order_id}")
            referenced_order_ids.add(item["order_id"])

            outcome = _receipt_outcome(raw)
            external_no: str | None = None
            paid_amount: int | None = None
            if outcome == "paid":
                external_no = _receipt_no(_pick(
                    raw, "external_receipt_no", "receipt_no", "receipt_id",
                    "external_receipt_id", "receipt_number",
                    "external_receipt_number", "external_reference",
                    "bank_receipt_no",
                ))
                paid_amount = _required_int(
                    _pick(raw, "amount_cents", "receipt_amount_cents",
                          "paid_amount_cents", "amount", "paid_amount"),
                    "amount_cents",
                )
                if paid_amount != item["amount_cents"]:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "receipt_amount_mismatch",
                            "message": "paid receipt amount must equal the "
                                       "original payment instruction amount",
                            "payment_item_id": item_id,
                            "instruction_amount_cents": item["amount_cents"],
                            "receipt_amount_cents": paid_amount,
                        },
                    )

            if item_id in decisions:
                old_outcome, old_no, old_amount = decisions[item_id]
                same = (
                    outcome == old_outcome
                    and external_no == old_no
                    and paid_amount == old_amount
                )
                if not same:
                    raise err(409, "conflicting_receipt_batch",
                              f"payment item {item_id} has conflicting receipt "
                              "results in the same batch")
                continue

            if item["status"] == "paid":
                same_paid = (
                    outcome == "paid"
                    and external_no == item["external_receipt_no"]
                    and paid_amount == item["receipt_amount_cents"]
                )
                if same_paid:
                    continue
                raise err(409, "receipt_already_recorded",
                          f"payment item {item_id} already has a conflicting "
                          "paid receipt")
            if item["status"] == "unpaid":
                if outcome == "unpaid":
                    continue
                raise err(409, "receipt_already_recorded",
                          f"payment item {item_id} was already confirmed unpaid")

            # Item is still pending.
            if outcome == "pending":
                decisions[item_id] = ("pending", None, None)
                continue

            if outcome == "paid":
                duplicate = conn.execute(
                    "SELECT id FROM payment_items "
                    "WHERE session_id=? AND status='paid' "
                    "AND external_receipt_no=? AND id<>?",
                    (session_id, external_no, item_id),
                ).fetchone()
                if duplicate is not None:
                    raise err(
                        409, "external_receipt_no_already_used",
                        f"external receipt number {external_no} is already "
                        f"used by payment item {duplicate['id']}",
                    )
                for other_id, decision in decisions.items():
                    if (other_id != item_id and decision[0] == "paid"
                            and decision[1] == external_no):
                        raise err(
                            409, "external_receipt_no_already_used",
                            f"external receipt number {external_no} occurs for "
                            "two payment items in this batch",
                        )

            decisions[item_id] = (outcome, external_no, paid_amount)

        touched_order_ids: set[int] = set()
        for item_id, (outcome, external_no, paid_amount) in decisions.items():
            item = items_by_id[item_id]
            touched_order_ids.add(item["order_id"])
            if item["status"] != "pending" or outcome == "pending":
                continue
            if outcome == "paid":
                conn.execute(
                    "UPDATE payment_items SET status='paid', "
                    "external_receipt_no=?, receipt_amount_cents=?, "
                    "receipted_at=datetime('now') "
                    "WHERE id=? AND status='pending'",
                    (external_no, paid_amount, item_id),
                )
            else:
                conn.execute(
                    "UPDATE payment_items SET status='unpaid', "
                    "receipted_at=datetime('now') "
                    "WHERE id=? AND status='pending'",
                    (item_id,),
                )

        for touched_order_id in touched_order_ids:
            _update_order_status(conn, touched_order_id)

        if order_id is None:
            if not referenced_order_ids:
                raise err(400, "receipts_required",
                          "no payment items were referenced")
            # 单条目接口只处理一个付款项；若以后开放跨单回执，调用方应分别查询。
            order_id = sorted(referenced_order_ids)[0]
        order_row = conn.execute(
            "SELECT * FROM payment_orders WHERE id=? AND session_id=?",
            (order_id, session_id),
        ).fetchone()
        if order_row is None:
            raise err(404, "payment_order_not_found",
                      "receipts were recorded but payment order is missing")
        order = _payment_order_dict(conn, order_row)
        order["receipt_batch_applied"] = bool(touched_order_ids)
        return _wrapped_order(order)

    # ---- 核账路由 ------------------------------------------------------- #

    @app.post("/sessions/{session_id}/billing/versions", status_code=201)
    def submit_billing_version(
        session_id: int,
        payload: dict[str, Any] | None = None,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.immediate_transaction(conn):
            return _create_billing_version(conn, session_id, payload or {})

    @app.get("/sessions/{session_id}/billing/versions")
    def list_billing_versions(
        session_id: int,
        version: int | None = Query(default=None, ge=1),
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            if version is not None:
                row = conn.execute(
                    "SELECT * FROM billing_versions "
                    "WHERE session_id=? AND version=?",
                    (session_id, version),
                ).fetchone()
                if row is None:
                    raise err(404, "billing_version_not_found",
                              f"billing version {version} does not exist")
                return _billing_version_dict(conn, row)
            rows = conn.execute(
                "SELECT * FROM billing_versions WHERE session_id=? "
                "ORDER BY version",
                (session_id,),
            ).fetchall()
            current_version = rows[-1]["version"] if rows else 0
            versions = [
                _billing_version_dict(
                    conn, row, current_version=current_version,
                    include_slots=False,
                )
                for row in rows
            ]
            return {
                "session_id": session_id,
                "current_version": current_version,
                "versions": versions,
                "billing_versions": versions,
            }

    @app.get("/sessions/{session_id}/billing/versions/{version}")
    def get_billing_version(
        session_id: int,
        version: str,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            if version in {"current", "latest"}:
                row = _current_billing_row(conn, session_id)
                if row is None:
                    balances = _payee_balances(conn, session_id, 0)
                    return {
                        "session_id": session_id,
                        "exists": False,
                        "version": 0,
                        "account_version": 0,
                        "current_version": 0,
                        "cutoff_slot": 0,
                        "end_slot": 0,
                        "entries": [],
                        "billing_entries": [],
                        "balances": balances,
                        "payee_balances": balances,
                    }
            elif version.isdigit():
                row = conn.execute(
                    "SELECT * FROM billing_versions "
                    "WHERE session_id=? AND version=?",
                    (session_id, int(version)),
                ).fetchone()
            else:
                row = None
            if row is None:
                raise err(404, "billing_version_not_found",
                          f"billing version {version} does not exist")
            return _billing_version_dict(conn, row)

    @app.get("/sessions/{session_id}/billing/current")
    @app.get("/sessions/{session_id}/billing/latest")
    @app.get("/sessions/{session_id}/billing/versions/current")
    @app.get("/sessions/{session_id}/billing/versions/latest")
    @app.get("/sessions/{session_id}/accounting/current")
    @app.get("/sessions/{session_id}/ledger/current")
    def get_current_billing(
        session_id: int,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            row = _current_billing_row(conn, session_id)
            balances = _payee_balances(conn, session_id)
            if row is None:
                return {
                    "session_id": session_id,
                    "exists": False,
                    "version": 0,
                    "account_version": 0,
                    "current_version": 0,
                    "cutoff_slot": 0,
                    "end_slot": 0,
                    "entries": [],
                    "billing_entries": [],
                    "balances": balances,
                    "payee_balances": balances,
                }
            result = _billing_version_dict(conn, row)
            result["exists"] = True
            result["balances"] = balances
            result["payee_balances"] = balances
            return result

    @app.get("/sessions/{session_id}/balances")
    @app.get("/sessions/{session_id}/billing/balances")
    @app.get("/sessions/{session_id}/billing/payees")
    @app.get("/sessions/{session_id}/payment-balances")
    @app.get("/sessions/{session_id}/payees")
    def get_payee_balances(
        session_id: int,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            current_row = _current_billing_row(conn, session_id)
            current_version = current_row["version"] if current_row else 0
            balances = _payee_balances(conn, session_id, current_version)
            return {
                "session_id": session_id,
                "current_version": current_version,
                "account_version": current_version,
                "balances": balances,
                "payee_balances": balances,
                "payees": balances,
            }

    @app.get("/sessions/{session_id}/payees/{payee_id}/balance")
    @app.get("/sessions/{session_id}/billing/payees/{payee_id}/balance")
    @app.get("/sessions/{session_id}/balances/{payee_id}")
    def get_payee_balance(
        session_id: int,
        payee_id: str,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        normalized = _payee_id(payee_id)
        with dbmod.read_transaction(conn):
            balances = _payee_balances(conn, session_id)
            balance = next(
                (b for b in balances if b["payee_id"] == normalized),
                None,
            )
            if balance is None:
                balance = {
                    "payee_id": normalized,
                    "payee": normalized,
                    "current_payable_cents": 0,
                    "payable_cents": 0,
                    "payable": 0,
                    "paid_cents": 0,
                    "paid": 0,
                    "occupied_cents": 0,
                    "occupied": 0,
                    "reserved_cents": 0,
                    "held_cents": 0,
                    "pending_receipt_cents": 0,
                    "pending_cents": 0,
                    "pending_amount_cents": 0,
                    "balance_cents": 0,
                    "balance": 0,
                    "remaining_balance_cents": 0,
                    "remaining_payable_cents": 0,
                    "available_cents": 0,
                    "available_balance_cents": 0,
                    "openable_amount_cents": 0,
                    "can_open_amount_cents": 0,
                }
            return balance

    # ---- 付款单路由 ----------------------------------------------------- #

    @app.post("/sessions/{session_id}/payment-orders", status_code=201)
    async def create_payment_order(
        session_id: int,
        request: Request,
        response: Response,
        x_staff_token: str | None = Header(default=None),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key"
        ),
        x_idempotency_key: str | None = Header(
            default=None, alias="X-Idempotency-Key"
        ),
        x_request_id: str | None = Header(
            default=None, alias="X-Request-Id"
        ),
        request_id: str | None = None,
        idempotency_key_query: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        payload = await _json_body(request)
        with dbmod.immediate_transaction(conn):
            body, status_code = _create_payment_order(
                conn, session_id, payload,
                [request_id, idempotency_key_query, idempotency_key,
                 x_idempotency_key, x_request_id],
            )
        response.status_code = status_code
        return body

    @app.get("/sessions/{session_id}/payment-orders")
    def list_payment_orders(
        session_id: int,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            rows = conn.execute(
                "SELECT * FROM payment_orders WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            orders = [_payment_order_dict(conn, row) for row in rows]
            return {
                "session_id": session_id,
                "payment_orders": orders,
                "orders": orders,
            }

    @app.get("/sessions/{session_id}/payment-orders/{order_id}")
    def get_payment_order(
        session_id: int,
        order_id: str,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            row = None
            if order_id.isdigit():
                row = conn.execute(
                    "SELECT * FROM payment_orders WHERE id=? AND session_id=?",
                    (int(order_id), session_id),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM payment_orders "
                    "WHERE request_key=? AND session_id=?",
                    (order_id, session_id),
                ).fetchone()
            if row is None:
                raise err(404, "payment_order_not_found",
                          f"payment order {order_id} does not exist")
            return _wrapped_order(_payment_order_dict(conn, row))

    @app.get("/sessions/{session_id}/payment-orders/by-request/{request_key}")
    @app.get("/sessions/{session_id}/payment-orders/request/{request_key}")
    def get_payment_order_by_request(
        session_id: int,
        request_key: str,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        with dbmod.read_transaction(conn):
            row = conn.execute(
                "SELECT * FROM payment_orders "
                "WHERE session_id=? AND request_key=?",
                (session_id, request_key),
            ).fetchone()
            if row is None:
                idem = conn.execute(
                    "SELECT * FROM payment_idempotency "
                    "WHERE session_id=? AND request_key=?",
                    (session_id, request_key),
                ).fetchone()
                if idem is not None and idem["order_id"] is None:
                    return _no_payment_order_body(
                        session_id, request_key, replayed=True
                    )
                raise err(404, "payment_order_not_found",
                          f"no payment order for request {request_key}")
            return _wrapped_order(_payment_order_dict(conn, row))

    @app.post("/sessions/{session_id}/payment-orders/{order_id}/receipts")
    async def register_payment_order_receipts(
        session_id: int,
        order_id: str,
        request: Request,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        body = await _json_body(request)
        if isinstance(body, list):
            raw_receipts: Any = body
        elif isinstance(body, dict):
            raw_receipts = _pick(
                body, "receipts", "items", "results", "payment_items"
            )
            if raw_receipts is None and _pick(
                body, "payment_item_id", "item_id", "instruction_id",
                "payment_instruction_id", "payment_id", "id",
            ) is not None:
                raw_receipts = [body]
            if raw_receipts is None:
                raise err(400, "receipts_required",
                          "receipts list or a single receipt object is required")
        else:
            raw_receipts = []
        with dbmod.immediate_transaction(conn):
            resolved_order_id = _resolve_order_id(conn, session_id, order_id)
            return _register_receipts(
                conn, session_id, raw_receipts,
                order_id=resolved_order_id,
            )

    @app.post("/sessions/{session_id}/receipts")
    @app.post("/sessions/{session_id}/payment-receipts")
    async def register_session_receipts(
        session_id: int,
        request: Request,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        body = await _json_body(request)
        order_ref: Any = None
        if isinstance(body, list):
            raw_receipts: Any = body
        elif isinstance(body, dict):
            order_ref = _pick(
                body, "payment_order_id", "order_id", "order_no",
                "payment_order_no",
            )
            raw_receipts = _pick(
                body, "receipts", "items", "results", "payment_items"
            )
            if raw_receipts is None and _pick(
                body, "payment_item_id", "item_id", "instruction_id",
                "payment_instruction_id", "payment_id", "id",
            ) is not None:
                raw_receipts = [body]
            if raw_receipts is None:
                raise err(400, "receipts_required",
                          "receipts list or a single receipt object is required")
        else:
            raw_receipts = []
        with dbmod.immediate_transaction(conn):
            order_id = (
                _resolve_order_id(conn, session_id, order_ref)
                if order_ref is not None else None
            )
            return _register_receipts(
                conn, session_id, raw_receipts, order_id=order_id
            )

    @app.post("/sessions/{session_id}/payment-items/{item_id}/receipt")
    @app.post("/sessions/{session_id}/payment-items/{item_id}/receipts")
    @app.post("/sessions/{session_id}/payments/{item_id}/receipt")
    @app.post("/sessions/{session_id}/payment-instructions/{item_id}/receipt")
    async def register_payment_item_receipt(
        session_id: int,
        item_id: int,
        request: Request,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        require_staff(conn, session_id, x_staff_token)
        body = await _json_body(request)
        raw: dict[str, Any] = body if isinstance(body, dict) else {}
        raw.setdefault("payment_item_id", item_id)
        with dbmod.immediate_transaction(conn):
            return _register_receipts(conn, session_id, [raw])

    # 非嵌套别名：与既有 /duties/{id} 路由风格保持一致。
    @app.post("/payment-orders/{order_id}/receipts")
    @app.post("/payment_orders/{order_id}/receipts")
    async def register_receipts_without_session(
        order_id: str,
        request: Request,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        ref = str(order_id)
        order = None
        if ref.isdigit():
            order = conn.execute(
                "SELECT * FROM payment_orders WHERE id=?", (int(ref),)
            ).fetchone()
        if order is None:
            order = conn.execute(
                "SELECT * FROM payment_orders WHERE order_no=?", (ref,)
            ).fetchone()
        if order is None:
            key_rows = conn.execute(
                "SELECT * FROM payment_orders WHERE request_key=?", (ref,)
            ).fetchall()
            if len(key_rows) == 1:
                order = key_rows[0]
        if order is None:
            raise err(404, "payment_order_not_found",
                      f"payment order {order_id} does not exist")
        require_staff(conn, order["session_id"], x_staff_token)
        body = await _json_body(request)
        if isinstance(body, list):
            raw_receipts = body
        elif isinstance(body, dict):
            raw_receipts = _pick(body, "receipts", "items", "results") or [body]
        else:
            raw_receipts = []
        with dbmod.immediate_transaction(conn):
            return _register_receipts(
                conn, order["session_id"], raw_receipts,
                order_id=int(order["id"]),
            )

    @app.post("/payment-items/{item_id}/receipt")
    @app.post("/payment-items/{item_id}/receipts")
    @app.post("/payments/{item_id}/receipt")
    async def register_item_receipt_without_session(
        item_id: int,
        request: Request,
        x_staff_token: str | None = Header(default=None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        item = conn.execute(
            "SELECT * FROM payment_items WHERE id=?", (item_id,)
        ).fetchone()
        if item is None:
            raise err(404, "payment_item_not_found",
                      f"payment item {item_id} does not exist")
        require_staff(conn, item["session_id"], x_staff_token)
        body = await _json_body(request)
        if isinstance(body, list):
            raw_receipts: Any = body
        else:
            raw = dict(body) if isinstance(body, dict) else {}
            raw.setdefault("payment_item_id", item_id)
            raw_receipts = [raw]
        with dbmod.immediate_transaction(conn):
            return _register_receipts(
                conn, item["session_id"], raw_receipts
            )

    # 常见命名别名，降低接口命名差异带来的集成成本。
    billing_collection_aliases = [
        "/sessions/{session_id}/billing",
        "/sessions/{session_id}/billing/audit",
        "/sessions/{session_id}/billing/reconcile",
        "/sessions/{session_id}/billing/audits",
        "/sessions/{session_id}/billings",
        "/sessions/{session_id}/settlements",
        "/sessions/{session_id}/reconciliations",
    ]
    for alias_path in billing_collection_aliases:
        app.add_api_route(
            alias_path, submit_billing_version, methods=["POST"],
            status_code=201,
        )
        app.add_api_route(
            alias_path, list_billing_versions, methods=["GET"],
        )

    billing_detail_aliases = [
        "/sessions/{session_id}/billing/audits/{version}",
        "/sessions/{session_id}/billings/{version}",
        "/sessions/{session_id}/settlements/{version}",
        "/sessions/{session_id}/reconciliations/{version}",
    ]
    for alias_path in billing_detail_aliases:
        app.add_api_route(
            alias_path, get_billing_version, methods=["GET"],
        )

    payment_collection_aliases = [
        "/sessions/{session_id}/payment_orders",
        "/sessions/{session_id}/payment-batches",
        "/sessions/{session_id}/payout-batches",
        "/sessions/{session_id}/payouts",
        "/sessions/{session_id}/payout-orders",
        "/sessions/{session_id}/payments/orders",
    ]
    for alias_path in payment_collection_aliases:
        app.add_api_route(
            alias_path, create_payment_order, methods=["POST"],
            status_code=201,
        )
        app.add_api_route(
            alias_path, list_payment_orders, methods=["GET"],
        )

    payment_detail_aliases = [
        "/sessions/{session_id}/payment_orders/{order_id}",
        "/sessions/{session_id}/payouts/{order_id}",
        "/sessions/{session_id}/payout-orders/{order_id}",
    ]
    for alias_path in payment_detail_aliases:
        app.add_api_route(alias_path, get_payment_order, methods=["GET"])

    receipt_aliases = [
        "/sessions/{session_id}/payment-orders/{order_id}/receipts",
        "/sessions/{session_id}/payment-orders/{order_id}/receipt",
        "/sessions/{session_id}/payment_orders/{order_id}/receipts",
        "/sessions/{session_id}/payment_orders/{order_id}/receipt",
        "/sessions/{session_id}/payouts/{order_id}/receipts",
    ]
    for alias_path in receipt_aliases:
        app.add_api_route(
            alias_path, register_payment_order_receipts,
            methods=["POST", "PUT", "PATCH"],
        )

    return app
