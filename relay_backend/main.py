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

    return app
