#!/usr/bin/env python3
"""跨会场补班端到端演示（可复跑的小型例子）。

场景
====
大会有两个**同时开播**的分会场 A、B，共用同一组口译员。每个会场 8 格（每格
2 秒，共 16 秒），在第 4 格设换人点，各有 [0,4) 与 [4,8) 两个班段；两会场都
需要中英（en）口译。初始由 alice 包会场 A、bob 包会场 B，carol 是同组备班。

要证明的两件事
==============
1. **一个会场缺人/可用时间变更，必须挪动另一个会场尚未播到的班段才能补齐。**
   bob 的可用时间缩短到只覆盖前半段后，他在会场 B **尚未播到**的 [4,8) 班段
   无法继续；服务在满足全部约束的方案里选换人最少的一份，把该段跨会场调给
   carol，并落实为新班表（未换人的班段保留原当班 ID 与凭据）。

2. **第二个班段一旦已有格播出，同一个变更必须被拒绝。**
   重启后先让 bob 的会场 B [4,8) 班段播出一格（静音同样算发布），该班段随即
   锁定、人员不可更换；此时再提交**完全相同**的可用时间变更，bob 的新窗口已
   覆盖不了该锁定段尚未发布的部分，服务找不到完整方案，**整批拒绝，原班表、
   凭据与上传资格一并不变**。

用法::

    python demo/run_dispatch_demo.py [--db demo/out_dispatch/demo.db]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from relay_backend.main import create_app  # noqa: E402
from relay_backend.wav import make_mono_wav  # noqa: E402

VOICE = make_mono_wav(330)
START = "2026-11-26T10:00:00+00:00"
END = "2026-11-26T11:00:00+00:00"
# bob 缩短后的窗口：只覆盖到 10:00:08，即每个会场的前 4 格（[0,4) 班段）。
BOB_EARLY_END = "2026-11-26T10:00:08+00:00"


def check(resp, expected: int = 200) -> dict:
    if resp.status_code != expected:
        raise SystemExit(
            f"API 调用失败: {resp.request.method} {resp.request.url}\n"
            f"  期望 {expected}，实际 {resp.status_code}: {resp.text}"
        )
    return resp.json() if resp.content else {}


def window(person_id: str, start: str, end: str) -> dict:
    return {"person_id": person_id, "languages": ["en"],
            "availability_windows": [{"starts_at": start, "ends_at": end}]}


def full(person_id: str) -> dict:
    return window(person_id, START, END)


def changed_request() -> list[dict]:
    """两次演示提交的是完全相同的一份可用时间变更。"""
    return [full("alice"), window("bob", START, BOB_EARLY_END), full("carol")]


def print_schedule(plan: dict) -> None:
    for sess in plan["sessions"]:
        segs = ", ".join(
            f"[{s['start_slot']},{s['end_slot']})={s['person_id']}"
            + ("(锁定)" if s["locked"] else "")
            for s in sess["segments"])
        print(f"    会场#{sess['session_id']} {sess['title']}: {segs}")


def seg_of(plan: dict, sid: int, start_slot: int) -> dict:
    return next(s for sess in plan["sessions"]
                if sess["session_id"] == sid
                for s in sess["segments"] if s["start_slot"] == start_slot)


def build_world(db_path: Path):
    client = TestClient(create_app(db_path))
    hall_a = check(client.post("/sessions", json={"title": "会场A"}), 201)
    hall_b = check(client.post("/sessions", json={"title": "会场B"}), 201)
    group = check(client.post("/dispatch-groups",
                              json={"name": "同传调度组"}), 201)
    gh = {"X-Dispatcher-Token": group["dispatcher_token"]}
    for hall in (hall_a, hall_b):
        check(client.post(
            f"/dispatch-groups/{group['group_id']}/sessions", headers=gh,
            json={"session_id": hall["session_id"], "starts_at": START,
                  "slot_count": 8, "handover_slots": [4],
                  "languages": ["en"]}), 201)
    plan = check(client.post(
        f"/dispatch-groups/{group['group_id']}/schedule", headers=gh,
        json={"interpreters": [full("alice"), full("bob"), full("carol")]}), 201)
    return client, hall_a, hall_b, group, gh, plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    out_dir = (Path(args.db).parent if args.db
               else Path(__file__).parent / "out_dispatch").resolve()
    db_path = Path(args.db or out_dir / "demo_dispatch.db").resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    print(f"==> 数据库: {db_path}")

    # ------------------------------------------------------------------ #
    # 第一阶段：班段未播出，同样的变更被接受，且必须跨会场挪动未开播班段
    # ------------------------------------------------------------------ #
    client, hall_a, hall_b, group, gh, plan1 = build_world(db_path)
    print("==> 初始班表（修订 v1）:")
    print_schedule(plan1)

    print("==> 提交变更：bob 的可用时间缩短到只覆盖前 4 格")
    plan2 = check(client.post(
        f"/dispatch-groups/{group['group_id']}/schedule", headers=gh,
        json={"interpreters": changed_request()}), 201)
    print(f"==> 补班落实（修订 v{plan2['schedule_version']}），"
          f"换人班段 {plan2['reassigned_segment_count']} 个:")
    print_schedule(plan2)
    for change in plan2["changed_segments"]:
        print(f"    会场#{change['session_id']} [{change['start_slot']},"
              f"{change['end_slot']}) {change['previous_person_id']} -> "
              f"{change['person_id']}")

    # 校验：确实挪动了另一个会场（B）尚未播到的 [4,8) 班段
    moved = next(c for c in plan2["changed_segments"]
                 if c["session_id"] == hall_b["session_id"]
                 and c["start_slot"] == 4)
    assert moved["previous_person_id"] == "bob" and moved["person_id"] == "carol"
    # 校验：未换人的班段保留原 duty_id 与凭据
    before = {(sess["session_id"], s["start_slot"]): s
              for sess in plan1["sessions"] for s in sess["segments"]}
    for sess in plan2["sessions"]:
        for seg in sess["segments"]:
            old = before[(sess["session_id"], seg["start_slot"])]
            if seg["person_id"] == old["person_id"]:
                assert seg["duty_id"] == old["duty_id"]
                assert seg["duty_token"] == old["duty_token"]
    print("    校验通过：跨会场挪动了 B 的未开播 [4,8)；未换人班段凭据保留")

    # 换人后旧当班失权：bob 的旧 duty 不能再传被调走的段
    old_bob = before[(hall_b["session_id"], 4)]
    r = client.post(f"/duties/{old_bob['duty_id']}/slots/4/audio",
                    content=VOICE,
                    headers={"X-Duty-Token": old_bob["duty_token"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "outside_duty_range"
    print("    bob 旧当班对被调走班段的上传被拒: outside_duty_range")
    client.close()

    # ------------------------------------------------------------------ #
    # 第二阶段：重启后先播出该班段一格，再提交同一变更 -> 必须整批拒绝
    # ------------------------------------------------------------------ #
    client = TestClient(create_app(db_path))
    cov = check(client.get(
        f"/dispatch-groups/{group['group_id']}/coverage", headers=gh))
    print(f"==> 重启后仍以最后一次修订 v{cov['schedule_version']} 为准")

    # 为演示"锁定"，把班表恢复成 bob 仍负责 B[4,8) 的初始状态后再发布：
    # 直接在一份新库上重建同一世界，先发布、后提交变更，顺序与第一阶段相反。
    client.close()
    locked_db = out_dir / "demo_locked.db"
    client, hall_a, hall_b, group, gh, plan_l = build_world(locked_db)
    seg0 = seg_of(plan_l, hall_b["session_id"], 0)
    seg4 = seg_of(plan_l, hall_b["session_id"], 4)
    assert seg4["person_id"] == "bob"
    # 连续发布前缀：格 0..3 声音、格 4 静音，frontier 推进到 5，[4,8) 锁定。
    for slot in range(4):
        check(client.post(
            f"/duties/{seg0['duty_id']}/slots/{slot}/audio", content=VOICE,
            headers={"X-Duty-Token": seg0["duty_token"]}), 201)
    check(client.put(f"/sessions/{hall_b['session_id']}/slots/4/silence",
                     headers={"X-Staff-Token": hall_b["staff_token"]}), 201)
    host = check(client.get(f"/sessions/{hall_b['session_id']}/host",
                            headers={"X-Staff-Token": hall_b["staff_token"]}))
    assert host["frontier"] == 5
    print(f"==> 会场B 已发布到格4（静音），bob 的 [4,8) 班段锁定、不可换人")

    # 提交与第一阶段完全相同的变更 -> 这次必须拒绝
    r = client.post(
        f"/dispatch-groups/{group['group_id']}/schedule", headers=gh,
        json={"interpreters": changed_request()})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    print("==> 同一变更在班段有格播出后被拒绝: schedule_infeasible")
    for issue in detail["uncoverable_segments"]:
        print(f"    无法覆盖: 会场#{issue['session_id']} "
              f"[{issue['start_slot']},{issue['end_slot']}) {issue['reason']}"
              + (f"（当班 {issue['person_id']}）" if issue.get("person_id") else ""))

    # 原安排、凭据与上传资格一并不变
    after = check(client.get(
        f"/dispatch-groups/{group['group_id']}/coverage", headers=gh))
    assert after["schedule_version"] == 1
    kept = seg_of(after, hall_b["session_id"], 4)
    assert kept["locked"] is True and kept["person_id"] == "bob"
    assert kept["duty_token"] == seg4["duty_token"]
    print("    校验通过：班表版本仍为 v1、锁定班段人员与凭据保留，原安排运行")

    # 护栏：已接入场次不能走原人工排班接口
    r = client.put(f"/sessions/{hall_a['session_id']}/duties",
                   json={"interpreter": "mallory", "start_slot": 6},
                   headers={"X-Staff-Token": hall_a["staff_token"]})
    assert r.status_code == 409
    print(f"    人工排班接口对已接入场次关闭: {r.json()['detail']['error']}")

    client.close()
    print("==> 演示完成")


if __name__ == "__main__":
    main()
