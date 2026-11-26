"""跨会场补班（dispatch groups）规则测试。

覆盖：
  * 登记约束（未排班、无音频/静音、每场只能入一个组、换人点范围）；
  * 首次排班与补班：语言、可用时间窗、跨会场实际时间不重叠、首尾相接允许；
  * 最少换人：只挪动未开播班段，已发布（含静音）格所在班段锁定；
  * 无解时整批拒绝、原安排与人员信息保留，并报告无法同时覆盖的班段；
  * 换人后旧当班失权、暂存声音取消资格且不自动复活、凭据按班段保留；
  * 已接入场次不能走原人工排班/静音接口绕过组内安排，不能续出登记范围；
  * 乐观并发、重启续接。
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from relay_backend.main import create_app
from relay_backend.wav import make_mono_wav

from conftest import WAV_A, make_session

WAV_C = make_mono_wav(700)


def make_session_with_token(client: TestClient, title: str = "test") -> tuple[int, str]:
    sid, token = make_session(client, title)
    return sid, token


# --------------------------------------------------------------------------- #
# 构造助手
# --------------------------------------------------------------------------- #


def iso(hour: int, minute: int = 0, second: int = 0) -> str:
    return f"2026-11-26T{hour:02d}:{minute:02d}:{second:02d}+00:00"


START = iso(10)
END = iso(11)
YESTERDAY = ("2026-11-25T00:00:00+00:00", "2026-11-25T01:00:00+00:00")


def make_group(client: TestClient, name: str = "g") -> dict:
    r = client.post("/dispatch-groups", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def register(client: TestClient, group: dict, sid: int, *, starts_at=START,
             slot_count=8, handover_slots=(4,), languages=("en",)) -> dict:
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/sessions",
        headers={"X-Dispatcher-Token": group["dispatcher_token"]},
        json={"session_id": sid, "starts_at": starts_at, "slot_count": slot_count,
              "handover_slots": list(handover_slots), "languages": list(languages)},
    )
    assert r.status_code == 201, r.text
    return r.json()


def person(pid: str, windows, *, languages=("en",)) -> dict:
    return {"person_id": pid, "languages": list(languages),
            "availability_windows": [
                {"starts_at": a, "ends_at": b} for a, b in windows]}


def full(pid: str, *, languages=("en",)) -> dict:
    return person(pid, [(START, END)], languages=languages)


def unavailable(pid: str) -> dict:
    return person(pid, [YESTERDAY])


def schedule(client: TestClient, group: dict, people, *, base_version=None,
             expected=201):
    payload: dict = {"interpreters": people}
    if base_version is not None:
        payload["base_version"] = base_version
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/schedule",
        headers={"X-Dispatcher-Token": group["dispatcher_token"]},
        json=payload,
    )
    assert r.status_code == expected, r.text
    body = r.json()
    # 失败时直接返回 detail，调用方按错误字典断言；成功时返回完整班表。
    return body["detail"] if expected != 201 else body


def two_sessions(client: TestClient,
                 group: dict) -> tuple[int, str, int, str]:
    s1, t1 = make_session_with_token(client, "hall-A")
    s2, t2 = make_session_with_token(client, "hall-B")
    register(client, group, s1)
    register(client, group, s2)
    return s1, t1, s2, t2


def segment_map(plan: dict) -> dict[tuple[int, int], str]:
    out = {}
    for sess in plan["sessions"]:
        for seg in sess["segments"]:
            out[(sess["session_id"], seg["start_slot"])] = seg["person_id"]
    return out


def find_segment(plan: dict, sid: int, start_slot: int) -> dict:
    sess = next(s for s in plan["sessions"] if s["session_id"] == sid)
    return next(seg for seg in sess["segments"] if seg["start_slot"] == start_slot)


# --------------------------------------------------------------------------- #
# 登记约束
# --------------------------------------------------------------------------- #


def test_onboarding_requires_clean_session(client: TestClient) -> None:
    group = make_group(client)
    sid, staff = make_session(client)
    # 已有当班 -> 拒绝
    r = client.put(f"/sessions/{sid}/duties",
                   json={"interpreter": "alice", "start_slot": 0},
                   headers={"X-Staff-Token": staff})
    assert r.status_code == 201
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/sessions",
        headers={"X-Dispatcher-Token": group["dispatcher_token"]},
        json={"session_id": sid, "starts_at": START, "slot_count": 4,
              "handover_slots": [], "languages": ["en"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "session_already_scheduled"

    # 有静音记录 -> 拒绝
    sid2, staff2 = make_session(client)
    r = client.put(f"/sessions/{sid2}/slots/0/silence",
                   headers={"X-Staff-Token": staff2})
    assert r.status_code == 201
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/sessions",
        headers={"X-Dispatcher-Token": group["dispatcher_token"]},
        json={"session_id": sid2, "starts_at": START, "slot_count": 4,
              "handover_slots": [], "languages": ["en"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "session_has_silence"


def test_session_can_join_only_one_group_and_handover_range_checked(
    client: TestClient,
) -> None:
    g1 = make_group(client, "g1")
    g2 = make_group(client, "g2")
    sid, _ = make_session(client)
    register(client, g1, sid, slot_count=6, handover_slots=(2, 4))
    r = client.post(
        f"/dispatch-groups/{g2['group_id']}/sessions",
        headers={"X-Dispatcher-Token": g2["dispatcher_token"]},
        json={"session_id": sid, "starts_at": START, "slot_count": 6,
              "handover_slots": [], "languages": ["en"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "session_already_onboarded"

    # 换人点不能在边界上、不能重复
    sid2, _ = make_session(client)
    r = client.post(
        f"/dispatch-groups/{g1['group_id']}/sessions",
        headers={"X-Dispatcher-Token": g1["dispatcher_token"]},
        json={"session_id": sid2, "starts_at": START, "slot_count": 6,
              "handover_slots": [0, 6], "languages": ["en"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "handover_slot_out_of_range"
    sid3, _ = make_session(client)
    r = client.post(
        f"/dispatch-groups/{g1['group_id']}/sessions",
        headers={"X-Dispatcher-Token": g1["dispatcher_token"]},
        json={"session_id": sid3, "starts_at": START, "slot_count": 6,
              "handover_slots": [3, 3], "languages": ["en"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "duplicate_handover_slot"


def test_dispatcher_token_required(client: TestClient) -> None:
    group = make_group(client)
    sid, _ = make_session(client)
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/sessions",
        json={"session_id": sid, "starts_at": START, "slot_count": 4,
              "handover_slots": [], "languages": ["en"]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "bad_dispatcher_token"


# --------------------------------------------------------------------------- #
# 首次排班：语言、可用性、跨会场时间不重叠
# --------------------------------------------------------------------------- #


def test_first_schedule_covers_every_segment_without_overlap(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1, s2, _staff2 = two_sessions(client, group)
    plan = schedule(client, group, [full("alice"), full("bob")])
    assert plan["schedule_version"] == 1
    mapping = segment_map(plan)
    assert len(mapping) == 4 and set(mapping.values()) <= {"alice", "bob"}
    # 同时开播的两个会场，同一时刻不能是同一人
    assert mapping[(s1, 0)] != mapping[(s2, 0)]
    assert mapping[(s1, 4)] != mapping[(s2, 4)]
    # 每个班段都带当班凭据
    for sess in plan["sessions"]:
        for seg in sess["segments"]:
            assert seg["duty_id"] and seg["duty_token"]
            assert seg["required_languages"] == ["en"]


def test_language_requirement_enforced(client: TestClient) -> None:
    group = make_group(client)
    s1, _ = make_session(client, "fr-hall")
    register(client, group, s1, languages=("fr",))
    # 只有英语译员 -> 无解
    r = schedule(client, group, [full("alice"), full("bob")], expected=409)
    detail = r
    assert detail["error"] == "schedule_infeasible"
    assert detail["uncoverable_segments"][0]["reason"] == \
        "no_qualified_available_interpreter"
    # 名单未落库
    cov = client.get(f"/dispatch-groups/{group['group_id']}/coverage",
                     headers={"X-Dispatcher-Token": group["dispatcher_token"]}).json()
    assert cov["schedule_version"] == 0
    # 补上法语译员后排得出来
    plan = schedule(client, group,
                    [full("alice"), person("colette", [(START, END)],
                                           languages=("fr",))])
    assert all(seg["person_id"] == "colette"
               for sess in plan["sessions"] for seg in sess["segments"])


def test_availability_must_cover_whole_segment_but_touching_windows_are_fine(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, _ = make_session(client)
    # 4 个格、每格 2 秒：节目 10:00:00~10:00:08，换人点 4 -> 两段各 4 秒
    register(client, group, s1, slot_count=4, handover_slots=(2,))
    # 窗口只覆盖到第 2 格之前 -> 第二段排不出来
    detail = schedule(client, group, [
        person("alice", [(START, iso(10, 0, 4))]),
    ], expected=409)
    assert detail["uncoverable_segments"][0]["start_slot"] == 2
    # 两个首尾相接的窗口完整覆盖 -> 可行
    plan = schedule(client, group, [
        person("alice", [(START, iso(10, 0, 4)), (iso(10, 0, 4), iso(10, 0, 8))]),
    ])
    assert find_segment(plan, s1, 0)["person_id"] == "alice"
    assert find_segment(plan, s1, 2)["person_id"] == "alice"


def test_back_to_back_segments_across_venues_allowed(client: TestClient) -> None:
    group = make_group(client)
    # 会场 A 10:00 开播 4 格（8 秒）；会场 B 10:00:08 开播，首尾相接不重叠
    s1, _ = make_session(client, "A")
    s2, _ = make_session(client, "B")
    register(client, group, s1, starts_at=iso(10), slot_count=4,
             handover_slots=())
    register(client, group, s2, starts_at=iso(10, 0, 8), slot_count=4,
             handover_slots=())
    # 只有一名译员也能排：两段实际时间首尾相接
    plan = schedule(client, group, [full("alice")])
    mapping = segment_map(plan)
    assert mapping[(s1, 0)] == "alice" and mapping[(s2, 0)] == "alice"


# --------------------------------------------------------------------------- #
# 补班：最少换人、锁定、无解保留原安排
# --------------------------------------------------------------------------- #


def test_refill_minimizes_reassignments_and_preserves_credentials(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1, s2, _staff2 = two_sessions(client, group)
    plan1 = schedule(client, group, [full("alice"), full("bob")])
    mapping1 = segment_map(plan1)
    # 记录会场 A 第一段的当班 ID/凭据（未换人时必须保留）
    kept = find_segment(plan1, s1, 0)

    # alice 请假（窗口挪到昨天），carol 加入：只应换掉 alice 名下的班段
    plan2 = schedule(client, group,
                     [unavailable("alice"), full("bob"), full("carol")])
    assert plan2["schedule_version"] == 2
    changed = {(d["session_id"], d["start_slot"]): d["person_id"]
               for d in plan2["changed_segments"]}
    alice_segments = {key for key, pid in mapping1.items() if pid == "alice"}
    assert set(changed) == alice_segments
    assert all(pid == "carol" for pid in changed.values())
    # bob 的班段与 alice 旧段之外的安排不动
    mapping2 = segment_map(plan2)
    for key, pid in mapping1.items():
        if pid == "bob":
            assert mapping2[key] == "bob"
    # 未换人班段保留原 duty_id 与 token：逐班段对比，人员未变则凭据必须不变
    for sess2 in plan2["sessions"]:
        for seg2 in sess2["segments"]:
            seg1 = find_segment(plan1, sess2["session_id"], seg2["start_slot"])
            if seg2["person_id"] == seg1["person_id"]:
                assert seg2["duty_id"] == seg1["duty_id"]
                assert seg2["duty_token"] == seg1["duty_token"]
            else:
                assert seg2["duty_id"] != seg1["duty_id"]


def test_infeasible_refill_changes_nothing_and_reports_segments(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1, s2, _staff2 = two_sessions(client, group)
    plan1 = schedule(client, group, [full("alice"), full("bob")])
    # 两人同时请假，又没有新人 -> 四个班段都无法同时覆盖
    detail = schedule(client, group, [unavailable("alice"), unavailable("bob")],
                      expected=409)
    issues = detail["uncoverable_segments"]
    assert len(issues) == 4
    # 两人的窗口都与节目错开：没有任何合格且可用的人覆盖这些班段
    assert all(i["reason"] == "no_qualified_available_interpreter" for i in issues)
    # 原班表、原凭据保留
    cov = client.get(f"/dispatch-groups/{group['group_id']}/coverage",
                     headers={"X-Dispatcher-Token": group["dispatcher_token"]}).json()
    assert cov["schedule_version"] == 1
    assert segment_map(cov) == segment_map(plan1)
    # 人员名单也回滚（alice/bob 仍是全窗口可用）
    info = client.get(f"/dispatch-groups/{group['group_id']}",
                      headers={"X-Dispatcher-Token": group["dispatcher_token"]}).json()
    assert {p["person_id"] for p in info["interpreters"]} == {"alice", "bob"}
    assert all(p["availability_windows"][0]["starts_at"] == START
               for p in info["interpreters"])


def test_published_segment_locks_even_against_silence_and_availability(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff = make_session_with_token(client, "hall-A")
    s2, _ = make_session_with_token(client, "hall-B")
    register(client, group, s1)
    register(client, group, s2)
    plan1 = schedule(client, group, [full("alice"), full("bob")])
    first = find_segment(plan1, s1, 0)
    # 格 0 记静音 -> 该班段任一格已发布，整段锁定
    r = client.put(f"/sessions/{s1}/slots/0/silence",
                   headers={"X-Staff-Token": staff})
    assert r.status_code == 201
    # 锁定段人员"请假"也不能被换；新窗口只需覆盖未发布部分（格1 起，10:00:02）
    locked_pid = first["person_id"]
    plan2 = schedule(client, group, [
        person(locked_pid, [(iso(10, 0, 2), END)]),  # 不覆盖已发布的格0
        full("bob"), full("carol"),
    ])
    again = find_segment(plan2, s1, 0)
    assert again["locked"] is True
    assert again["person_id"] == locked_pid
    assert again["duty_id"] == first["duty_id"]  # 凭据保留

    # 新窗口连未发布部分（格1..3）都覆盖不了 -> 锁定段人员固定，整批拒绝
    schedule(client, group, [
        person(locked_pid, [(iso(10, 0, 4), END)]),
        full("bob"), full("carol"),
    ], expected=409)
    # 人手少到锁定段之外排不下，同样整批拒绝、原安排不变
    schedule(client, group, [
        person(locked_pid, [(iso(10, 0, 2), END)]),
        unavailable("bob"),
    ], expected=409)
    cov = client.get(f"/dispatch-groups/{group['group_id']}/coverage",
                     headers={"X-Dispatcher-Token": group["dispatcher_token"]}).json()
    assert cov["schedule_version"] == plan2["schedule_version"]


# --------------------------------------------------------------------------- #
# 换人失权：旧当班不能传、暂存声音不复活
# --------------------------------------------------------------------------- #


def test_reassigned_duty_loses_upload_right_and_buffered_audio_stays_disqualified(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1, s2, _staff2 = two_sessions(client, group)
    plan1 = schedule(client, group, [full("alice"), full("bob"), full("carol")])
    seg4 = find_segment(plan1, s1, 4)
    # 先给未发布的格 4 暂存一条声音
    r = client.post(f"/duties/{seg4['duty_id']}/slots/4/audio", content=WAV_A,
                    headers={"X-Duty-Token": seg4["duty_token"]})
    assert r.status_code == 201 and r.json()["published"] is False
    old_pid = seg4["person_id"]

    # 把该段人员撤掉换人
    remaining = [p for p in ("alice", "bob", "carol") if p != old_pid] + ["dave"]
    plan2 = schedule(client, group, [full(pid) for pid in remaining])
    new_seg4 = find_segment(plan2, s1, 4)
    assert new_seg4["person_id"] != old_pid
    assert new_seg4["duty_id"] != seg4["duty_id"]

    # 旧当班再传被拒
    r = client.post(f"/duties/{seg4['duty_id']}/slots/4/audio", content=WAV_A,
                    headers={"X-Duty-Token": seg4["duty_token"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "outside_duty_range"

    # 格 4 只剩失权旧声音，新当班必须重新明确提交
    host = client.get(f"/sessions/{s1}/host",
                      headers={"X-Staff-Token": staff1},
                      params={"start": 4, "limit": 1}).json()
    view = host["slots"][0]
    assert view["state"] == "disqualified_only"
    assert view["owner"]["interpreter"] == new_seg4["person_id"]

    # 后来同一个人重新当班该段，旧声音也不自动带回：新 duty 仍是 waiting
    plan3 = schedule(client, group, [full(pid) for pid in
                                     [p for p in remaining if p != "dave"] + [old_pid]])
    back = find_segment(plan3, s1, 4)
    if back["person_id"] == old_pid:
        host = client.get(f"/sessions/{s1}/host",
                          headers={"X-Staff-Token": staff1},
                          params={"start": 4, "limit": 1}).json()
        assert host["slots"][0]["state"] == "disqualified_only"


def test_published_audio_and_billing_sources_unchanged_after_swap(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1, s2, _staff2 = two_sessions(client, group)
    plan1 = schedule(client, group, [full("alice"), full("bob")])
    seg0 = find_segment(plan1, s1, 0)
    # 格 0..3 连续上传并发布
    for slot in range(4):
        r = client.post(f"/duties/{seg0['duty_id']}/slots/{slot}/audio",
                        content=WAV_A,
                        headers={"X-Duty-Token": seg0["duty_token"]})
        assert r.status_code == 201
    assert client.get(f"/sessions/{s1}/program").json()["frontier"] == 4
    # 补班换掉未开播的 [4,8) 段，已发布声音字节不变
    schedule(client, group, [unavailable("alice"), full("bob"), full("carol")])
    assert client.get(f"/sessions/{s1}/slots/0/audio").content == WAV_A
    prog = client.get(f"/sessions/{s1}/program").json()
    assert prog["entries"][0]["duty_id"] == seg0["duty_id"]


# --------------------------------------------------------------------------- #
# 接入后原接口的护栏
# --------------------------------------------------------------------------- #


def test_managed_session_rejects_manual_scheduling(client: TestClient) -> None:
    group = make_group(client)
    s1, staff1, _s2, _ = two_sessions(client, group)
    schedule(client, group, [full("alice"), full("bob")])
    r = client.put(f"/sessions/{s1}/duties",
                   json={"interpreter": "mallory", "start_slot": 6},
                   headers={"X-Staff-Token": staff1})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "session_managed_by_dispatch_group"


def test_silence_cannot_start_unscheduled_session_or_exceed_range(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1 = make_session_with_token(client, "late")
    register(client, group, s1, starts_at=iso(12), slot_count=4,
             handover_slots=())
    # 尚无班表 -> 静音不能提前开场
    r = client.put(f"/sessions/{s1}/slots/0/silence",
                   headers={"X-Staff-Token": staff1})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "dispatch_schedule_not_ready"

    plan = schedule(client, group, [
        person("alice", [(iso(12), iso(13))])])
    seg = find_segment(plan, s1, 0)
    # 登记范围之外不能上传
    r = client.post(f"/duties/{seg['duty_id']}/slots/4/audio", content=WAV_C,
                    headers={"X-Duty-Token": seg["duty_token"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "slot_outside_registered_range"
    r = client.put(f"/sessions/{s1}/slots/4/silence",
                   headers={"X-Staff-Token": staff1})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "slot_outside_registered_range"


def test_unmanaged_session_keeps_legacy_endpoints(client: TestClient) -> None:
    sid, staff = make_session(client)
    r = client.put(f"/sessions/{sid}/duties",
                   json={"interpreter": "alice", "start_slot": 0},
                   headers={"X-Staff-Token": staff})
    assert r.status_code == 201  # 未接入场次照常使用原接口


# --------------------------------------------------------------------------- #
# 并发与重启
# --------------------------------------------------------------------------- #


def test_same_change_accepted_before_air_but_rejected_after_segment_publishes(
    client: TestClient,
) -> None:
    """演示用例的断言版：同一份可用时间变更，未播出可补、有格播出即拒。"""
    group = make_group(client)
    s1, _t1, s2, staff2 = two_sessions(client, group)
    schedule(client, group, [full("alice"), full("bob"), full("carol")])
    # bob 缩短到只覆盖前 4 格：B 的 [4,8) 未播出，可挪给 carol
    change = [
        full("alice"),
        person("bob", [(START, iso(10, 0, 8))]),
        full("carol"),
    ]
    plan = schedule(client, group, change)
    moved = next(d for d in plan["changed_segments"]
                 if d["session_id"] == s2 and d["start_slot"] == 4)
    assert moved["previous_person_id"] == "bob" and moved["person_id"] == "carol"

    # 另起一个干净世界：先让 bob 的 B[4,8) 播出一格，再提交同一份变更
    group2 = make_group(client, "g2")
    h1, _ = make_session_with_token(client, "A2")
    h2, staff_h2 = make_session_with_token(client, "B2")
    register(client, group2, h1)
    register(client, group2, h2)
    base = schedule(client, group2,
                    [full("alice"), full("bob"), full("carol")])
    seg0 = find_segment(base, h2, 0)
    seg4 = find_segment(base, h2, 4)
    assert seg4["person_id"] == "bob"
    for slot in range(4):
        r = client.post(f"/duties/{seg0['duty_id']}/slots/{slot}/audio",
                        content=WAV_A,
                        headers={"X-Duty-Token": seg0["duty_token"]})
        assert r.status_code == 201
    r = client.put(f"/sessions/{h2}/slots/4/silence",
                   headers={"X-Staff-Token": staff_h2})
    assert r.status_code == 201
    detail = schedule(client, group2, change, expected=409)
    issue = next(i for i in detail["uncoverable_segments"]
                 if i["session_id"] == h2 and i["start_slot"] == 4)
    assert issue["reason"] == "locked_segment_holder_unavailable"
    # 原安排与凭据保留
    cov = client.get(f"/dispatch-groups/{group2['group_id']}/coverage",
                     headers={"X-Dispatcher-Token": group2["dispatcher_token"]}).json()
    kept = find_segment(cov, h2, 4)
    assert cov["schedule_version"] == 1
    assert kept["person_id"] == "bob" and kept["duty_token"] == seg4["duty_token"]


def test_concurrent_refills_serialize_and_never_half_apply(
    client: TestClient,
) -> None:
    group = make_group(client)
    s1, staff1, s2, _staff2 = two_sessions(client, group)
    schedule(client, group, [full("alice"), full("bob")])
    errors: list[int] = []
    versions: list[int] = []
    lock = threading.Lock()

    def refill(people: list[str]) -> None:
        r = client.post(
            f"/dispatch-groups/{group['group_id']}/schedule",
            headers={"X-Dispatcher-Token": group["dispatcher_token"]},
            json={"interpreters": [full(pid) for pid in people]})
        with lock:
            errors.append(r.status_code)
            if r.status_code == 201:
                versions.append(r.json()["schedule_version"])

    t1 = threading.Thread(target=refill, args=(["alice", "bob", "carol"],))
    t2 = threading.Thread(target=refill, args=(["alice", "bob", "dave"],))
    t1.start(); t2.start(); t1.join(); t2.join()
    # 两个都可解：串行落实，版本号连续，绝不会出现半步状态
    assert sorted(errors) == [201, 201]
    assert sorted(versions) == [2, 3]
    cov = client.get(f"/dispatch-groups/{group['group_id']}/coverage",
                     headers={"X-Dispatcher-Token": group["dispatcher_token"]}).json()
    # 每个会场班段数完整（各 2 段），没有任一会场停在旧表、另一会场在新表
    for sess in cov["sessions"]:
        assert len(sess["segments"]) == 2
        assert all(seg["person_id"] for seg in sess["segments"])


def test_stale_base_version_rejected(client: TestClient) -> None:
    group = make_group(client)
    two_sessions(client, group)
    schedule(client, group, [full("alice"), full("bob")])
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/schedule",
        headers={"X-Dispatcher-Token": group["dispatcher_token"]},
        json={"base_version": 1, "interpreters": [full("alice"), full("bob")]})
    assert r.status_code == 201  # 基于当前版本 1，成功
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/schedule",
        headers={"X-Dispatcher-Token": group["dispatcher_token"]},
        json={"base_version": 1, "interpreters": [full("alice"), full("bob")]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_schedule_version"


def test_schedule_survives_restart(tmp_path) -> None:
    db_file = tmp_path / "restart_dispatch.db"
    app = create_app(db_file)
    with TestClient(app) as c:
        group = make_group(c)
        s1, _t1, s2, _t2 = two_sessions(c, group)
        plan = schedule(c, group, [full("alice"), full("bob")])
        expected = segment_map(plan)
        version = plan["schedule_version"]
        seg = find_segment(plan, s1, 0)
        # 已发布一格
        r = c.post(f"/duties/{seg['duty_id']}/slots/0/audio", content=WAV_A,
                   headers={"X-Duty-Token": seg["duty_token"]})
        assert r.status_code == 201
    # 重启：同库文件重建应用
    app2 = create_app(db_file)
    with TestClient(app2) as c2:
        cov = c2.get(f"/dispatch-groups/{group['group_id']}/coverage",
                     headers={"X-Dispatcher-Token": group["dispatcher_token"]}).json()
        assert cov["schedule_version"] == version
        assert segment_map(cov) == expected
        assert c2.get(f"/sessions/{s1}/slots/0/audio").content == WAV_A
