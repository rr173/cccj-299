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

import hmac
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
        with dbmod.immediate_transaction(conn):
            cur = conn.execute(
                "INSERT INTO sessions (staff_token, title, slot_seconds) VALUES (?, ?, ?)",
                (token, title, slot_seconds),
            )
            sid = cur.lastrowid
        return {"session_id": sid, "title": title, "slot_seconds": slot_seconds,
                "staff_token": token}

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

    return app
