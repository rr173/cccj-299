"""官方精选：草稿编辑、发行、批准与回听。

重点场景（对应用户要求的两场小演示）：
  * 同一来源格在草稿中出现两次：基于旧版本提交、只改未受影响那一次，能合入；
    填过又清空的缝隙不能沿用（旧 gap id 永久关闭）。
  * 重叠批准逐条撤回：撤一条时仍在播可听，最后一条失效后整版下架，
    连旧版取音也拿不到声音；原节目不受影响。
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_backend.main import create_app
from relay_backend.wav import make_mono_wav

from conftest import assign, make_session


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

WAV = {slot: make_mono_wav(200 + 40 * slot) for slot in range(10)}


@pytest.fixture()
def studio(client: TestClient):
    """一场已发布 0..5 格口译节目，外加工作人员与审核员凭据。"""
    sid, staff = make_session(client)
    duty = assign(client, sid, staff, "alice", 0)
    for slot in range(6):
        r = client.post(
            f"/duties/{duty['duty_id']}/slots/{slot}/audio",
            content=WAV[slot],
            headers={"X-Duty-Token": duty["duty_token"]},
        )
        assert r.status_code == 201, r.text
    info = client.get(f"/sessions/{sid}").json()
    assert info["frontier"] == 6
    # 建场次时签发的审核员 token 从库里取（测试用独立连接）
    from relay_backend.db import connect

    with connect(client.app.state.db_path) as conn:
        token = conn.execute(
            "SELECT reviewer_token FROM edition_reviewers WHERE session_id=?", (sid,)
        ).fetchone()["reviewer_token"]
    return {
        "sid": sid,
        "staff": {"X-Staff-Token": staff},
        "reviewer": {"X-Reviewer-Token": token},
        "duty": duty,
    }


def draft(client: TestClient, studio: dict) -> dict:
    r = client.get(f"/sessions/{studio['sid']}/curated/draft",
                   headers=studio["staff"])
    assert r.status_code == 200, r.text
    return r.json()


def edit(client: TestClient, studio: dict, version: int, operations: list[dict]):
    return client.post(
        f"/sessions/{studio['sid']}/curated/draft/edits",
        headers=studio["staff"],
        json={"version": version, "operations": operations},
    )


def spans(view: dict) -> list[tuple[int, int]]:
    return [(e["start_slot"], e["end_slot"]) for e in view["excerpts"]]


def gap_at(view: dict, position: int) -> int:
    matches = [g for g in view["gaps"] if g["position"] == position]
    assert len(matches) == 1, view
    return matches[0]["gap_id"]


def excerpt_at(view: dict, position: int) -> int:
    return view["excerpts"][position]["excerpt_id"]


def fetch_curated_audio(client: TestClient, sid: int, position: int):
    return client.get(f"/sessions/{sid}/curated/slots/{position}/audio")


# --------------------------------------------------------------------------- #
# 演示一：同一原声的两次出现互相独立；填过又清空的缝隙不能沿用
# --------------------------------------------------------------------------- #


def test_stale_edit_survives_when_its_own_excerpt_is_untouched(
    client: TestClient, studio: dict
) -> None:
    sid = studio["sid"]

    # v1: 摘录 [0..1]；v2: 在尾部再插入一段 [0..0]（同一原声的第二次出现）
    v0 = draft(client, studio)
    r = edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0, "end_slot": 1},
    ])
    assert r.status_code == 200 and r.json()["version"] == 1, r.text
    v1 = draft(client, studio)
    r = edit(client, studio, 1, [
        {"kind": "insert", "gap_id": gap_at(v1, 1), "start_slot": 0},
    ])
    assert r.status_code == 200, r.text
    v2 = draft(client, studio)
    assert spans(v2) == [(0, 1), (0, 0)]

    first = excerpt_at(v2, 0)   # 第一次出现
    second = excerpt_at(v2, 1)  # 第二次出现，原声同样是格 0

    # X 基于 v2 把"第一次出现"换成 [2..2]
    r = edit(client, studio, 2, [
        {"kind": "replace", "excerpt_ids": [first], "start_slot": 2},
    ])
    assert r.status_code == 200, r.text

    # Y 也基于旧的 v2，但改的是"第二次出现"：原声相同、出现 id 不同，
    # 互不替代，因此仍应合入，不被 X 的提交挡住。
    r = edit(client, studio, 2, [
        {"kind": "replace", "excerpt_ids": [second], "start_slot": 3},
    ])
    assert r.status_code == 200, r.text
    v4 = draft(client, studio)
    assert spans(v4) == [(2, 2), (3, 3)]

    # 反例：基于 v2 去改已被 X 动过的第一次出现 -> 冲突，整批不生效。
    r = edit(client, studio, 2, [
        {"kind": "replace", "excerpt_ids": [first], "start_slot": 4},
    ])
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["error"] == "batch_rejected"
    assert body["operations"][0]["operation"] == 0
    assert body["operations"][0]["error"] == "excerpt_unavailable"
    assert draft(client, studio)["version"] == 4

    # ---- 填过又清空的缝隙不能沿用 ----
    # P 在 v4 的缝隙（位置 1）填入 [5..5] -> [(2,2), (5,5), (3,3)]
    gap_between = gap_at(v4, 1)
    r = edit(client, studio, 4, [
        {"kind": "insert", "gap_id": gap_between, "start_slot": 5},
    ])
    assert r.status_code == 200, r.text
    v5 = draft(client, studio)
    assert spans(v5) == [(2, 2), (5, 5), (3, 3)]
    filled = excerpt_at(v5, 1)

    # Q 把刚填进去的那段删空 -> 看起来又回到 [(2,2), (3,3)]
    r = edit(client, studio, 5, [
        {"kind": "replace", "excerpt_ids": [filled]},
    ])
    assert r.status_code == 200, r.text
    v6 = draft(client, studio)
    assert spans(v6) == [(2, 2), (3, 3)]

    # 同一位置又出现一个"新"缝隙，但旧缝隙已永久关闭：基于旧版本 v4 再向
    # 旧缝隙插入必须被拒，即使那里看起来又是空的。
    r = edit(client, studio, 4, [
        {"kind": "insert", "gap_id": gap_between, "start_slot": 4},
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["operations"][0]["error"] == "gap_unavailable"
    assert gap_at(v6, 1) != gap_between
    assert draft(client, studio)["version"] == 6


def test_batch_is_atomic_and_reports_the_failing_operation(
    client: TestClient, studio: dict
) -> None:
    v0 = draft(client, studio)
    r = edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0, "end_slot": 1},
    ])
    assert r.status_code == 200
    v1 = draft(client, studio)

    # 同一批里：合法插入 + 重复填同一缝隙。整批拒绝，版本不变。
    g = gap_at(v1, 1)
    r = edit(client, studio, 1, [
        {"kind": "insert", "gap_id": gap_at(v1, 0), "start_slot": 2},
        {"kind": "insert", "gap_id": g, "start_slot": 3},
        {"kind": "insert", "gap_id": g, "start_slot": 4},
    ])
    assert r.status_code == 409
    failed = {op["operation"] for op in r.json()["detail"]["operations"]}
    assert failed == {2}
    assert spans(draft(client, studio)) == [(0, 1)]
    assert draft(client, studio)["version"] == 1


# --------------------------------------------------------------------------- #
# 演示二：重叠批准逐条撤回 -> 整版下架；原节目仍可回听
# --------------------------------------------------------------------------- #


def release_live(client: TestClient, studio: dict) -> dict:
    version = draft(client, studio)["version"]
    r = client.post(f"/sessions/{studio['sid']}/curated/releases",
                    headers=studio["staff"], json={"version": version})
    assert r.status_code == 201, r.text
    return r.json()


def approve(client: TestClient, studio: dict, start: int, end: int) -> int:
    r = client.post(f"/sessions/{studio['sid']}/curated/approvals",
                    headers=studio["reviewer"],
                    json={"start_slot": start, "end_slot": end})
    assert r.status_code == 201, r.text
    return r.json()["approval_id"]


def revoke(client: TestClient, studio: dict, approval_id: int):
    return client.delete(
        f"/sessions/{studio['sid']}/curated/approvals/{approval_id}",
        headers=studio["reviewer"],
    )


def test_overlapping_approval_revocation_takes_whole_release_down(
    client: TestClient, studio: dict
) -> None:
    sid = studio["sid"]
    # 一段摘录 [0..2]，发行后逐格为 0, 1, 2
    v0 = draft(client, studio)
    assert edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0, "end_slot": 2},
    ]).status_code == 200

    # 格 0 被两条批准重叠覆盖；格 1、2 各一条
    a1 = approve(client, studio, 0, 0)
    a2 = approve(client, studio, 0, 0)
    approve(client, studio, 1, 1)
    approve(client, studio, 2, 2)

    release = release_live(client, studio)
    assert [e["source_slot"] for e in release["entries"]] == [0, 1, 2]
    for position in range(3):
        assert fetch_curated_audio(client, sid, position).status_code == 200

    # 先撤一条：格 0 仍被 a2 覆盖，整版继续在播，全部可听。
    r = revoke(client, studio, a1)
    assert r.status_code == 200 and r.json()["taken_down_release_id"] is None
    public = client.get(f"/sessions/{sid}/curated")
    assert public.status_code == 200
    assert fetch_curated_audio(client, sid, 0).status_code == 200
    assert fetch_curated_audio(client, sid, 0).content == WAV[0]

    # 最后一条覆盖格 0 的批准也撤掉 -> 整版同时下架。
    r = revoke(client, studio, a2)
    assert r.status_code == 200
    assert r.json()["taken_down_release_id"] == release["release_id"]

    # 已下架版本：不能再经精选接口列格或取音（任何位置都拿不到）。
    assert client.get(f"/sessions/{sid}/curated").status_code == 404
    for position in range(3):
        r = fetch_curated_audio(client, sid, position)
        assert r.status_code == 404
        assert "audio" not in r.headers.get("content-type", "")

    # 原节目完全不受影响：仍能列格、取音，字节一致。
    prog = client.get(f"/sessions/{sid}/program").json()
    assert prog["frontier"] == 6
    for slot in range(6):
        r = client.get(f"/sessions/{sid}/slots/{slot}/audio")
        assert r.status_code == 200 and r.content == WAV[slot]

    # 后来补批也不会恢复旧版：必须重新发行。
    approve(client, studio, 0, 0)
    assert client.get(f"/sessions/{sid}/curated").status_code == 404
    new_release = release_live(client, studio)
    assert new_release["release_id"] != release["release_id"]
    assert fetch_curated_audio(client, sid, 0).status_code == 200


def test_release_requires_current_nonempty_draft_and_full_approval(
    client: TestClient, studio: dict
) -> None:
    sid = studio["sid"]

    # 空草稿不能发行
    r = client.post(f"/sessions/{sid}/curated/releases",
                    headers=studio["staff"], json={"version": 0})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "draft_empty"

    v0 = draft(client, studio)
    edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0, "end_slot": 1},
    ])
    approve(client, studio, 0, 0)
    # 1 未获批准 -> 发行被拒，并指出缺批准的来源格
    r = client.post(f"/sessions/{sid}/curated/releases",
                    headers=studio["staff"], json={"version": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "missing_approval"
    assert r.json()["detail"]["message"] == [{"source_slot": 1}]

    # 指定旧版本发行被拒（只能发行当前版本）
    approve(client, studio, 1, 1)
    r = client.post(f"/sessions/{sid}/curated/releases",
                    headers=studio["staff"], json={"version": 0})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "stale_draft_version"

    release = release_live(client, studio)
    # 后续改稿不改变在播内容：草稿换成只留 [0..0]，在播仍是逐格 [0, 1]。
    edit(client, studio, 1, [
        {"kind": "replace", "excerpt_ids": [excerpt_at(draft(client, studio), 0)],
         "start_slot": 0, "end_slot": 0},
    ])
    assert spans(draft(client, studio)) == [(0, 0)]
    live = client.get(f"/sessions/{sid}/curated").json()
    assert [e["source_slot"] for e in live["entries"]] == [0, 1]
    assert live["release_id"] == release["release_id"]
    assert fetch_curated_audio(client, sid, 1).status_code == 200


def test_curated_silence_entry_and_restart_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "curated_restart.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        sid, staff = make_session(client)
        duty = assign(client, sid, staff, "alice", 0)
        for slot in (0, 1, 3, 4):
            r = client.post(
                f"/duties/{duty['duty_id']}/slots/{slot}/audio",
                content=WAV[slot],
                headers={"X-Duty-Token": duty["duty_token"]},
            )
            assert r.status_code == 201
        # 格 2 等待无果记静音，前台节目连续到 5
        r = client.put(f"/sessions/{sid}/slots/2/silence",
                       headers={"X-Staff-Token": staff})
        assert r.status_code == 201
        assert client.get(f"/sessions/{sid}").json()["frontier"] == 5

        from relay_backend.db import connect

        with connect(db_path) as conn:
            reviewer = conn.execute(
                "SELECT reviewer_token FROM edition_reviewers WHERE session_id=?",
                (sid,),
            ).fetchone()["reviewer_token"]
        staff_h = {"X-Staff-Token": staff}
        reviewer_h = {"X-Reviewer-Token": reviewer}

        v0 = draft(client, {"sid": sid, "staff": staff_h})
        assert edit(client, {"sid": sid, "staff": staff_h}, 0, [
            {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0, "end_slot": 2},
        ]).status_code == 200

        def approval(start: int, end: int) -> int:
            r = client.post(f"/sessions/{sid}/curated/approvals",
                            headers=reviewer_h,
                            json={"start_slot": start, "end_slot": end})
            assert r.status_code == 201, r.text
            return r.json()["approval_id"]

        keep = approval(0, 1)
        drop = approval(2, 2)
        release = client.post(f"/sessions/{sid}/curated/releases",
                              headers=staff_h, json={"version": 1})
        assert release.status_code == 201, release.text
        release = release.json()
        assert [e["source_slot"] for e in release["entries"]] == [0, 1, 2]
        assert release["entries"][2]["kind"] == "silence"
        r = client.get(f"/sessions/{sid}/curated/slots/2/audio")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "entry_is_silence"
        assert client.get(f"/sessions/{sid}/curated/slots/0/audio").content == WAV[0]

        # 静音格失去批准也导致整版下架
        r = client.delete(f"/sessions/{sid}/curated/approvals/{drop}",
                          headers=reviewer_h)
        assert r.json()["taken_down_release_id"] == release["release_id"]
        # 重新批准并重发
        approval(2, 2)
        r = client.post(f"/sessions/{sid}/curated/releases",
                        headers=staff_h, json={"version": 1})
        assert r.status_code == 201, r.text
        release2 = r.json()

    # 重启（同一库文件重建应用）后草稿、批准、在播版本仍在。
    app2 = create_app(db_path)
    with TestClient(app2) as c2:
        live = c2.get(f"/sessions/{sid}/curated")
        assert live.status_code == 200
        assert live.json()["release_id"] == release2["release_id"]
        assert c2.get(f"/sessions/{sid}/curated/slots/0/audio").content == WAV[0]
        d = c2.get(f"/sessions/{sid}/curated/draft", headers=staff_h).json()
        assert spans(d) == [(0, 2)]
        assert d["live_release_id"] == release2["release_id"]
        # 重启后审核员仍能撤回，且撤回会下架在播版
        r = c2.delete(f"/sessions/{sid}/curated/approvals/{keep}",
                      headers=reviewer_h)
        assert r.status_code == 200
        assert r.json()["taken_down_release_id"] == release2["release_id"]
        assert c2.get(f"/sessions/{sid}/curated").status_code == 404
        # 原节目不受影响
        assert c2.get(f"/sessions/{sid}/slots/0/audio").content == WAV[0]


def test_curated_role_checks(client: TestClient, studio: dict) -> None:
    sid = studio["sid"]
    # 草稿读写要工作人员
    assert client.get(f"/sessions/{sid}/curated/draft").status_code == 403
    # 批准要审核员，工作人员不能代替
    r = client.post(f"/sessions/{sid}/curated/approvals",
                    headers=studio["staff"],
                    json={"start_slot": 0, "end_slot": 0})
    assert r.status_code == 403
    # 发行要工作人员，审核员不能代替
    r = client.post(f"/sessions/{sid}/curated/releases",
                    headers=studio["reviewer"], json={"version": 0})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# 补充：批内约束、编辑不受批准限制、并发下架无部分状态
# --------------------------------------------------------------------------- #


def test_batch_rejects_insert_beside_replaced_excerpt(
    client: TestClient, studio: dict
) -> None:
    # 空草稿只有一个首尾缝隙，逐版在两旁长出三段摘录。
    v0 = draft(client, studio)
    assert edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 1},
    ]).status_code == 200
    v1 = draft(client, studio)
    assert edit(client, studio, 1, [
        {"kind": "insert", "gap_id": gap_at(v1, 0), "start_slot": 0},
    ]).status_code == 200
    v2 = draft(client, studio)
    assert edit(client, studio, 2, [
        {"kind": "insert", "gap_id": gap_at(v2, 2), "start_slot": 2},
    ]).status_code == 200
    v3 = draft(client, studio)
    assert spans(v3) == [(0, 0), (1, 1), (2, 2)]
    middle = excerpt_at(v3, 1)

    # 替换中间摘录的同时在它左侧的缝隙插入 -> 同批冲突
    r = edit(client, studio, 3, [
        {"kind": "replace", "excerpt_ids": [middle], "start_slot": 4},
        {"kind": "insert", "gap_id": gap_at(v3, 1), "start_slot": 5},
    ])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["operations"][0]["error"] == (
        "insert_beside_replaced_in_batch")

    # 同一个摘录被两项操作选中 -> 冲突
    r = edit(client, studio, 3, [
        {"kind": "replace", "excerpt_ids": [middle], "start_slot": 4},
        {"kind": "replace", "excerpt_ids": [middle], "start_slot": 5},
    ])
    assert r.status_code == 409
    errors = {op["operation"]: op["error"] for op in r.json()["detail"]["operations"]}
    assert errors == {1: "duplicate_excerpt_in_batch"}
    assert spans(draft(client, studio)) == [(0, 0), (1, 1), (2, 2)]
    assert draft(client, studio)["version"] == 3


def test_draft_editing_ignores_approvals(client: TestClient, studio: dict) -> None:
    # 完全没有批准也能自由编辑草稿。
    v0 = draft(client, studio)
    r = edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 5},
    ])
    assert r.status_code == 200, r.text
    assert draft(client, studio)["excerpts"][0]["approved"] is False


def test_concurrent_revoke_and_read_never_expose_partial_state(
    client: TestClient, studio: dict
) -> None:
    import threading

    sid = studio["sid"]
    v0 = draft(client, studio)
    edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0, "end_slot": 2},
    ])
    approve(client, studio, 0, 0)
    approve(client, studio, 0, 2)  # 格 0 被两条重叠批准覆盖
    release = release_live(client, studio)

    read_results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(3)
    saw_first = threading.Barrier(3)

    def reader() -> None:
        try:
            local = TestClient(client.app)
            barrier.wait()
            for i in range(40):
                if i == 1:
                    # 两个读者都至少看过一次完整在播状态后，才允许撤回。
                    saw_first.wait()
                # 单次请求看到的是完整快照：要么整版在播（三格齐全），
                # 要么整版下架（404），绝不会只下架一部分。
                r = local.get(f"/sessions/{sid}/curated")
                if r.status_code == 200:
                    assert [e["position"] for e in r.json()["entries"]] == [0, 1, 2]
                    read_results.append("live")
                else:
                    assert r.status_code == 404
                    read_results.append("down")
                audio = local.get(f"/sessions/{sid}/curated/slots/2/audio")
                if audio.status_code == 200:
                    assert audio.content == WAV[2]
                else:
                    assert audio.status_code == 404
                    assert "audio" not in audio.headers.get("content-type", "")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def revoker() -> None:
        try:
            local = TestClient(client.app)
            barrier.wait()
            saw_first.wait()
            # 撤第一条（格 0 仍被第二条覆盖）-> 不下架；
            # 再撤第二条 -> 整版下架。
            for approval_id in (1, 2):
                r = local.delete(
                    f"/sessions/{sid}/curated/approvals/{approval_id}",
                    headers=studio["reviewer"],
                )
                assert r.status_code == 200, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    threads.append(threading.Thread(target=revoker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert "live" in read_results and "down" in read_results
    assert client.get(f"/sessions/{sid}/curated").status_code == 404
    # 原节目仍完整可听
    for slot in range(6):
        assert client.get(f"/sessions/{sid}/slots/{slot}/audio").content == WAV[slot]
    del release


def test_stale_insert_survives_position_shift_elsewhere(
    client: TestClient, studio: dict
) -> None:
    """别处插入把缝隙的序号推移了，但缝隙本身没被改过，仍应合入。"""
    v0 = draft(client, studio)
    assert edit(client, studio, 0, [
        {"kind": "insert", "gap_id": gap_at(v0, 0), "start_slot": 0},
    ]).status_code == 200
    v1 = draft(client, studio)
    tail_gap = gap_at(v1, 1)  # 尾部缝隙（位置 1）

    # X 在头部缝隙插入，把尾部缝隙的位置从 1 推到 2。
    assert edit(client, studio, 1, [
        {"kind": "insert", "gap_id": gap_at(v1, 0), "start_slot": 1},
    ]).status_code == 200
    assert spans(draft(client, studio)) == [(1, 1), (0, 0)]

    # Y 基于旧版本 v1、仍按旧位置使用同一个尾部缝隙 -> 位置移动不算冲突。
    r = edit(client, studio, 1, [
        {"kind": "insert", "gap_id": tail_gap, "start_slot": 2},
    ])
    assert r.status_code == 200, r.text
    assert spans(draft(client, studio)) == [(1, 1), (0, 0), (2, 2)]
