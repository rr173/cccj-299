"""接力后端业务规则测试。

重点场景：
  * 两人接力、上传顺序打乱，从接力前一格回听，依次听到正确来源；
  * 掉线重连、再次当班，上传凭 duty token 识别到具体当班；
  * 已发布格不可回填/替换；同一班次重试不能换声音；
  * 换班后失去资格的旧声音不静默启用，需重新明确提交；
  * 静音裁决封闭该格；缺口前的声音先收但不发布；
  * 服务重启后节目续接、已发布声音仍可回听。
"""

from __future__ import annotations

import io
import math
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from relay_backend.main import create_app
from relay_backend.wav import make_mono_wav

from conftest import WAV_A, WAV_B, assign, make_session, upload


# --------------------------------------------------------------------------- #
# 工具：从 WAV 字节估算主频，用于确认"听到的是谁"
# --------------------------------------------------------------------------- #


def dominant_frequency(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        n = wf.getnframes()
        raw = wf.readframes(n)
    samples = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
    # 过零点法估算频率（演示用 8kHz 正弦音，足够准确）
    crossings = 0
    prev = samples[0]
    for s in samples[1:]:
        if prev <= 0 < s or prev >= 0 > s:
            crossings += 1
        prev = s
    rate = 8000
    return crossings * rate / (2 * len(samples))


def fetch_audio(client: TestClient, sid: int, slot: int) -> bytes:
    r = client.get(f"/sessions/{sid}/slots/{slot}/audio")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    return r.content


# --------------------------------------------------------------------------- #
# 核心场景：两人接力 + 乱序上传 + 从接力前一格回听
# --------------------------------------------------------------------------- #


def test_two_interpreter_relay_with_shuffled_uploads(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)   # 负责 0..3
    d2 = assign(client, sid, staff, "bob", 4)     # 负责 4..

    # 故意打乱上传顺序：新班先传、后班补传旧格、交错提交
    plan = [
        (d2["duty_id"], d2["duty_token"], 4, WAV_B),
        (d1["duty_id"], d1["duty_token"], 1, WAV_A),
        (d2["duty_id"], d2["duty_token"], 5, WAV_B),
        (d1["duty_id"], d1["duty_token"], 0, WAV_A),
        (d1["duty_id"], d1["duty_token"], 3, WAV_A),
        (d2["duty_id"], d2["duty_token"], 6, WAV_B),
        (d1["duty_id"], d1["duty_token"], 2, WAV_A),
    ]
    for duty_id, token, slot, wav in plan:
        r = client.post(
            f"/duties/{duty_id}/slots/{slot}/audio",
            content=wav,
            headers={"X-Duty-Token": token, "Content-Type": "audio/wav"},
        )
        assert r.status_code == 201, r.text

    # 全部到齐，frontier 推进到 7
    prog = client.get(f"/sessions/{sid}/program", params={"start": 0}).json()
    assert prog["frontier"] == 7
    assert [e["slot"] for e in prog["entries"]] == list(range(7))
    assert [e["interpreter"] for e in prog["entries"]] == [
        "alice", "alice", "alice", "alice", "bob", "bob", "bob"
    ]

    # 从接力前一格（3）开始回听：来源应依次为 alice, bob, bob, bob
    replay = client.get(f"/sessions/{sid}/program", params={"start": 3}).json()
    assert [e["slot"] for e in replay["entries"]] == [3, 4, 5, 6]
    assert [e["interpreter"] for e in replay["entries"]] == [
        "alice", "bob", "bob", "bob"
    ]

    # 真正取回声音，主频对应各自的演示音（300Hz / 500Hz）
    f3 = dominant_frequency(fetch_audio(client, sid, 3))
    f4 = dominant_frequency(fetch_audio(client, sid, 4))
    assert math.isclose(f3, 300, abs_tol=5)
    assert math.isclose(f4, 500, abs_tol=5)
    # 字节级一致：存进去什么就播什么
    assert fetch_audio(client, sid, 0) == WAV_A
    assert fetch_audio(client, sid, 6) == WAV_B


# --------------------------------------------------------------------------- #
# 掉线重连 / 再次当班：token 识别具体当班
# --------------------------------------------------------------------------- #


def test_reconnect_and_reassignment_identifies_duty_not_name(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)

    # 掉线期间录好的格，重连后用同一个 duty token 补传，仍在负责范围内
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 201

    # alice 再次当班（换人后又换回 alice），产生新 duty、新 token
    d2 = assign(client, sid, staff, "bob", 2)
    d3 = assign(client, sid, staff, "alice", 5)
    assert d3["duty_token"] != d1["duty_token"]

    # 旧 token 不能越权提交新当班负责的格
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/5/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "outside_duty_range"

    # 新 token 才能提交格 5
    r = client.post(
        f"/duties/{d3['duty_id']}/slots/5/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d3["duty_token"]},
    )
    assert r.status_code == 201

    # 伪造/他人 token 一律拒绝
    r = client.post(
        f"/duties/{d3['duty_id']}/slots/6/audio",
        content=WAV_A,
        headers={"X-Duty-Token": "duty_not_a_real_token"},
    )
    assert r.status_code == 403
    r = client.post(
        f"/duties/{d3['duty_id']}/slots/6/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d2["duty_token"]},
    )
    assert r.status_code == 403

    # 不存在的当班
    r = client.post(
        "/duties/9999/slots/0/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 404


def test_disconnected_duty_can_backfill_within_its_range(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    assign(client, sid, staff, "bob", 3)

    # alice 掉线，bob 先把 3、4 传了；alice 的 0..2 还缺 -> 节目卡在 0
    for slot in (3, 4):
        r = client.post(
            f"/duties/2/slots/{slot}/audio",
            content=WAV_B,
            headers={"X-Duty-Token": _token_of(client, sid, 2)},
        )
        assert r.status_code == 201
        assert r.json()["published"] is False  # 前面有缺口，先收但不发布

    host = client.get(
        f"/sessions/{sid}/host", headers={"X-Staff-Token": staff}
    ).json()
    assert host["frontier"] == 0
    assert host["blocked"]["slot"] == 0
    assert host["blocked"]["reason"] == "waiting_audio"
    # 格 3 状态为 buffered_ahead
    states = {s["slot"]: s["state"] for s in host["slots"]}
    assert states[3] == "buffered_ahead"

    # alice 重连，在自己负责范围（0..2）内补传
    for slot in (0, 1, 2):
        r = client.post(
            f"/duties/1/slots/{slot}/audio",
            content=WAV_A,
            headers={"X-Duty-Token": _token_of(client, sid, 1)},
        )
        assert r.status_code == 201

    prog = client.get(f"/sessions/{sid}/program").json()
    assert prog["frontier"] == 5
    assert [e["interpreter"] for e in prog["entries"]] == [
        "alice", "alice", "alice", "bob", "bob"
    ]


def _token_of(client: TestClient, sid: int, duty_id: int) -> str:
    # 每请求连接模型下，测试用独立连接查库取 duty token
    del sid
    from relay_backend.db import connect
    with connect(client.app.state.db_path) as conn:
        row = conn.execute(
            "SELECT duty_token FROM duties WHERE id=?", (duty_id,)
        ).fetchone()
    return row["duty_token"]


# --------------------------------------------------------------------------- #
# 不可回填 / 不可重试替换
# --------------------------------------------------------------------------- #


def test_finalized_slot_cannot_be_backfilled_or_replaced(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 201 and r.json()["published"] is True

    # 同一班次重试（即使声音不同）也被拒绝；该格已发布，先命中 finalized 规则
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=WAV_B,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "slot_finalized"
    # 节目里仍是原来的声音
    assert fetch_audio(client, sid, 0) == WAV_A

    # 在已发布格 0 设置新当班被拒绝（发布前缀不可改）
    r = client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "bob", "start_slot": 0},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "start_slot_taken"


def test_retry_rejected_even_for_unpublished_slot(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    # 先传格 2（格 0/1 还缺，未发布）
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/2/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 201 and r.json()["published"] is False
    # 对这个尚未发布的格重试换声音，同样拒绝
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/2/audio",
        content=WAV_B,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "already_submitted"


def test_finalized_slot_cannot_change_owner(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    # 格 0 已发布：不允许在 0 再设一个当班，也不允许把既有当班移到 0
    r = client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "bob", "start_slot": 0},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409
    d2 = assign(client, sid, staff, "bob", 2)
    r = client.patch(
        f"/duties/{d2['duty_id']}",
        json={"start_slot": 0},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "cannot_move_into_finalized_range"


# --------------------------------------------------------------------------- #
# 换班取消资格 / 恢复负责后必须重新明确提交
# --------------------------------------------------------------------------- #


def test_disqualified_audio_never_silently_reactivates(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    # alice 先把格 4 传了（当时她负责 0..）
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/4/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 201

    # 换班：bob 从格 4 接起，alice 的格 4 声音失去资格
    d2 = assign(client, sid, staff, "bob", 4)
    assert d2["disqualified_submissions"] >= 1

    host = client.get(
        f"/sessions/{sid}/host", headers={"X-Staff-Token": staff}
    ).json()
    s4 = next(s for s in host["slots"] if s["slot"] == 4)
    assert s4["state"] == "disqualified_only"
    assert s4["disqualified_submissions"] == 1

    # 改班：bob 的起点推迟到 6，格 4 重新归 alice 负责
    r = client.patch(
        f"/duties/{d2['duty_id']}",
        json={"start_slot": 6},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 200
    # 旧声音仍是 disqualified，没有静默启用：格 4 虽重新归 alice，
    # 但只剩失去资格的旧记录，必须重新明确提交
    host = client.get(
        f"/sessions/{sid}/host", headers={"X-Staff-Token": staff}
    ).json()
    s4 = next(s for s in host["slots"] if s["slot"] == 4)
    assert s4["state"] == "disqualified_only"
    assert s4["owner"]["duty_id"] == d1["duty_id"]
    assert s4["disqualified_submissions"] == 1
    assert s4["active_submissions"] == []
    # 听众侧仍然拿不到格 4
    r = client.get(f"/sessions/{sid}/slots/4/audio")
    assert r.status_code == 404

    # alice 重新明确提交格 4 后才进入节目
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/4/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 201

    # 把前面补齐后，格 4 发布的是新提交（submission id 不同）
    for slot in range(0, 4):
        client.post(
            f"/duties/{d1['duty_id']}/slots/{slot}/audio",
            content=WAV_A,
            headers={"X-Duty-Token": d1["duty_token"]},
        )
    prog = client.get(f"/sessions/{sid}/program", params={"start": 4}).json()
    assert prog["entries"][0]["slot"] == 4
    assert prog["entries"][0]["duty_id"] == d1["duty_id"]


def test_reschedule_rejected_inside_finalized_range(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    d2 = assign(client, sid, staff, "bob", 3)
    for slot in range(3):
        client.post(
            f"/duties/{d1['duty_id']}/slots/{slot}/audio",
            content=WAV_A,
            headers={"X-Duty-Token": d1["duty_token"]},
        )
    # frontier=3，d1 的起点 0 已在发布前缀内，不能改
    r = client.patch(
        f"/duties/{d1['duty_id']}",
        json={"start_slot": 1},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "duty_covers_finalized_slots"
    # 未发布范围内可以改
    r = client.patch(
        f"/duties/{d2['duty_id']}",
        json={"start_slot": 4},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 200
    assert r.json()["start_slot"] == 4


# --------------------------------------------------------------------------- #
# 静音裁决
# --------------------------------------------------------------------------- #


def test_silence_marks_block_future_uploads_and_extend_program(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    # 格 0 有声音；格 1 等不到人，记静音；格 2 已有缓冲
    client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    client.post(
        f"/duties/{d1['duty_id']}/slots/2/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    r = client.put(
        f"/sessions/{sid}/slots/1/silence",
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 201 and r.json()["frontier"] == 3

    prog = client.get(f"/sessions/{sid}/program").json()
    kinds = [(e["slot"], e["kind"]) for e in prog["entries"]]
    assert kinds == [(0, "audio"), (1, "silence"), (2, "audio")]

    # 静音格的音频读取给出明确 404
    r = client.get(f"/sessions/{sid}/slots/1/audio")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "slot_is_silence"

    # 此后该格不再接受补传
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/1/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "slot_marked_silence"

    # 重复记静音被拒绝
    r = client.put(
        f"/sessions/{sid}/slots/1/silence",
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409


def test_silence_rejected_when_qualifying_audio_exists(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    r = client.put(
        f"/sessions/{sid}/slots/0/silence",
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "slot_finalized"


def test_missing_due_to_disconnect_is_not_silence(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    # 只有格 1 的声音、没有格 0：不能发布，也不会被当成静音
    client.post(
        f"/duties/{d1['duty_id']}/slots/1/audio",
        content=WAV_A,
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    prog = client.get(f"/sessions/{sid}/program").json()
    assert prog["frontier"] == 0
    assert prog["entries"] == []
    assert prog["blocked"]["reason"] == "waiting_audio"
    host = client.get(
        f"/sessions/{sid}/host", headers={"X-Staff-Token": staff}
    ).json()
    s0 = next(s for s in host["slots"] if s["slot"] == 0)
    assert s0["state"] == "waiting" and s0["silence"] is False


# --------------------------------------------------------------------------- #
# 重启续接
# --------------------------------------------------------------------------- #


def test_server_restart_resumes_program_and_audio(tmp_path: Path) -> None:
    db_file = tmp_path / "restart_relay.db"
    app = create_app(db_file)
    with TestClient(app) as c:
        sid, staff = make_session(c)
        d1 = assign(c, sid, staff, "alice", 0)
        d2 = assign(c, sid, staff, "bob", 4)
        # 乱序上传 0..5
        order = [(d2, 4, WAV_B), (d1, 0, WAV_A), (d2, 5, WAV_B),
                 (d1, 1, WAV_A), (d1, 3, WAV_A), (d1, 2, WAV_A)]
        for duty, slot, wav in order:
            r = c.post(
                f"/duties/{duty['duty_id']}/slots/{slot}/audio",
                content=wav,
                headers={"X-Duty-Token": duty["duty_token"]},
            )
            assert r.status_code == 201
        c.put(f"/sessions/{sid}/slots/6/silence", headers={"X-Staff-Token": staff})

    # 模拟服务重启：用同一个数据库文件重新建 app
    app2 = create_app(db_file)
    with TestClient(app2) as c2:
        info = c2.get(f"/sessions/{sid}").json()
        assert info["frontier"] == 7  # 0..5 音频 + 6 静音
        prog = c2.get(f"/sessions/{sid}/program", params={"start": 3}).json()
        assert [e["interpreter"] for e in prog["entries"][:3]] == [
            "alice", "bob", "bob"
        ]
        # 已发布声音仍可回听，字节不变
        assert fetch_audio(c2, sid, 0) == WAV_A
        assert fetch_audio(c2, sid, 5) == WAV_B
        # 静音标记仍生效，补传仍被拒
        from relay_backend.db import connect
        with connect(db_file) as conn:
            row = conn.execute(
                "SELECT duty_token FROM duties WHERE id=?", (d1["duty_id"],)
            ).fetchone()
        r = c2.post(
            f"/duties/{d1['duty_id']}/slots/6/audio",
            content=WAV_A,
            headers={"X-Duty-Token": row["duty_token"]},
        )
        assert r.status_code == 409
        # 旧 duty token 仍可在其负责范围内补传未发布格（格 7 归 bob，试格 ... alice 范围到 3，已发完）
        r = c2.post(
            f"/duties/{d2['duty_id']}/slots/7/audio",
            content=WAV_B,
            headers={"X-Duty-Token": d2["duty_token"]},
        )
        assert r.status_code == 201


# --------------------------------------------------------------------------- #
# 并发：每请求独立连接 + BEGIN IMMEDIATE，写操作串行化
# --------------------------------------------------------------------------- #


def test_concurrent_uploads_distinct_slots_all_succeed(client: TestClient) -> None:
    import threading

    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(slot: int) -> None:
        try:
            # 每个线程独立 TestClient -> 独立 DB 连接
            local = TestClient(client.app)
            barrier.wait()
            r = local.post(
                f"/duties/{d1['duty_id']}/slots/{slot}/audio",
                content=WAV_A,
                headers={"X-Duty-Token": d1["duty_token"]},
            )
            assert r.status_code == 201, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    prog = client.get(f"/sessions/{sid}/program").json()
    assert prog["frontier"] == 8


def test_concurrent_duplicate_upload_one_wins_no_500(client: TestClient) -> None:
    import threading

    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        local = TestClient(client.app)
        barrier.wait()
        # 格 5 前面有缺口，永远不会因 finalized 规则提前结束；
        # 两个并发请求中恰有一个成功，另一个被 already_submitted 拒绝。
        r = local.post(
            f"/duties/{d1['duty_id']}/slots/5/audio",
            content=WAV_A,
            headers={"X-Duty-Token": d1["duty_token"]},
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]
    body = client.get(f"/sessions/{sid}/host",
                      headers={"X-Staff-Token": staff}).json()
    s5 = next(s for s in body["slots"] if s["slot"] == 5)
    assert len(s5["active_submissions"]) == 1


# --------------------------------------------------------------------------- #
# 输入校验与鉴权
# --------------------------------------------------------------------------- #


def test_invalid_wav_rejected(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/0/audio",
        content=b"this is definitely not a wav file",
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_wav"

    # 立体声 WAV 也拒绝
    stereo = make_mono_wav(300)  # 先造一个合法的，再手工拼双声道
    import io as _io
    import struct as _struct
    import wave as _wave
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(_struct.pack("<" + "h" * 16, *([100, -100] * 8)))
    r = client.post(
        f"/duties/{d1['duty_id']}/slots/1/audio",
        content=buf.getvalue(),
        headers={"X-Duty-Token": d1["duty_token"]},
    )
    assert r.status_code == 400


def test_staff_token_required(client: TestClient) -> None:
    sid, _ = make_session(client)
    r = client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "alice", "start_slot": 0},
    )
    assert r.status_code == 403
    r = client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "alice", "start_slot": 0},
        headers={"X-Staff-Token": "wrong"},
    )
    assert r.status_code == 403


def test_first_duty_must_start_at_zero(client: TestClient) -> None:
    sid, staff = make_session(client)
    r = client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "alice", "start_slot": 3},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "first_duty_must_start_at_zero"


def test_program_read_pagination(client: TestClient) -> None:
    sid, staff = make_session(client)
    d1 = assign(client, sid, staff, "alice", 0)
    for slot in range(6):
        client.post(
            f"/duties/{d1['duty_id']}/slots/{slot}/audio",
            content=WAV_A,
            headers={"X-Duty-Token": d1["duty_token"]},
        )
    page1 = client.get(
        f"/sessions/{sid}/program", params={"start": 0, "limit": 3}
    ).json()
    assert [e["slot"] for e in page1["entries"]] == [0, 1, 2]
    assert page1["next_start"] == 3
    page2 = client.get(
        f"/sessions/{sid}/program", params={"start": 3, "limit": 3}
    ).json()
    assert [e["slot"] for e in page2["entries"]] == [3, 4, 5]
    # 未发布范围不可直接取声音
    r = client.get(f"/sessions/{sid}/slots/9/audio")
    assert r.status_code == 404
