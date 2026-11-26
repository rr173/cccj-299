#!/usr/bin/env python3
"""官方精选端到端演示：草稿编辑 -> 批准 -> 发行 -> 回听 -> 撤回下架。

流程
----
1. 建场次并发布 0..5 格原节目；
2. 工作人员建草稿：先插入摘录 [0..2]，再插入同一原声的第二次出现 [0..0]；
3. 演示位置移动不算冲突：两个编辑都基于同一旧版本，各自改不同的摘录；
4. 演示填过又清空的缝隙不能沿用；
5. 审核员批准所需格并发行，听众逐格回听（不合成音频，直接取原格字节）；
6. 撤回重叠批准中的一条 -> 仍在播；撤回最后一条 -> 整版下架；
7. 原节目仍可回听。

用法::

    python demo/run_curated_demo.py [--db ...] [--out demo/out_curated]
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

AUDIO = {slot: make_mono_wav(200 + 40 * slot) for slot in range(6)}


def check(resp, expected: int = 200) -> dict:
    if resp.status_code != expected:
        raise SystemExit(
            f"API 调用失败: {resp.request.method} {resp.request.url}\n"
            f"  期望 {expected}，实际 {resp.status_code}: {resp.text}"
        )
    return resp.json() if resp.content else {}


def gaps(view: dict) -> dict[int, int]:
    return {g["position"]: g["gap_id"] for g in view["gaps"]}


def spans(view: dict) -> list[tuple[int, int]]:
    return [(e["start_slot"], e["end_slot"]) for e in view["excerpts"]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out_dir = Path(args.out or Path(__file__).parent / "out_curated").resolve()
    db_path = Path(args.db or out_dir / "curated_demo.db").resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    app = create_app(db_path)
    client = TestClient(app)
    session = check(client.post("/sessions", json={"title": "会场B-精选合编"}), 201)
    sid = session["session_id"]
    staff_h = {"X-Staff-Token": session["staff_token"]}
    reviewer_h = {"X-Reviewer-Token": session["reviewer_token"]}
    print(f"==> 场次 #{sid}，数据库 {db_path.name}")

    duty = check(client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "alice", "start_slot": 0}, headers=staff_h), 201)
    for slot in range(6):
        check(client.post(
            f"/duties/{duty['duty_id']}/slots/{slot}/audio",
            content=AUDIO[slot],
            headers={"X-Duty-Token": duty["duty_token"]}), 201)
    print(f"==> 原节目已发布 0..{client.get(f'/sessions/{sid}').json()['frontier'] - 1} 格")

    def get_draft() -> dict:
        return check(client.get(f"/sessions/{sid}/curated/draft", headers=staff_h))

    def edit(version: int, operations: list[dict], expected: int = 200) -> dict:
        return check(client.post(f"/sessions/{sid}/curated/draft/edits",
                                 headers=staff_h,
                                 json={"version": version,
                                       "operations": operations}), expected)

    # 1. 草稿：摘录 [0..2]，再在尾部插入同一原声的第二次出现 [0..0]
    d0 = get_draft()
    edit(0, [{"kind": "insert", "gap_id": gaps(d0)[0],
              "start_slot": 0, "end_slot": 2}])
    d1 = get_draft()
    edit(1, [{"kind": "insert", "gap_id": gaps(d1)[1], "start_slot": 0}])
    d2 = get_draft()
    print(f"==> 草稿 v{d2['version']}: {spans(d2)}（同一原声 0 出现两次）")

    first, second = (e["excerpt_id"] for e in d2["excerpts"])

    # 2. 两个编辑都基于 v2，各改一次出现 -> 都能合入
    edit(2, [{"kind": "replace", "excerpt_ids": [first], "start_slot": 3}])
    edit(2, [{"kind": "replace", "excerpt_ids": [second], "start_slot": 4}])
    d4 = get_draft()
    print(f"==> 两个基于旧版本 v2 的编辑都合入: {spans(d4)}（位置移动不算冲突）")

    # 3. 填过又清空的缝隙不能沿用
    gap = gaps(d4)[1]
    edit(4, [{"kind": "insert", "gap_id": gap, "start_slot": 5}])
    d5 = get_draft()
    filled = d5["excerpts"][1]["excerpt_id"]
    edit(5, [{"kind": "replace", "excerpt_ids": [filled]}])
    d6 = get_draft()
    try:
        edit(4, [{"kind": "insert", "gap_id": gap, "start_slot": 2}])
    except SystemExit:
        print(f"==> 基于 v4 复用已填过又清空的缝隙被拒；当前 v{d6['version']}: {spans(d6)}")

    # 4. 批准 + 发行
    version = d6["version"]
    # 两条批准都覆盖格 3：撤掉一条时格 3 仍被另一条覆盖。
    a1 = check(client.post(f"/sessions/{sid}/curated/approvals", headers=reviewer_h,
                           json={"start_slot": 3, "end_slot": 3}), 201)
    a2 = check(client.post(f"/sessions/{sid}/curated/approvals", headers=reviewer_h,
                           json={"start_slot": 3, "end_slot": 4}), 201)
    r = client.post(f"/sessions/{sid}/curated/releases", headers=staff_h,
                    json={"version": version}, )
    if r.status_code != 201:
        raise SystemExit(f"发行失败: {r.text}")
    release = r.json()
    print(f"==> 已发行 v{version}，共 {len(release['entries'])} 格: "
          f"{[(e['position'], e['source_slot']) for e in release['entries']]}")

    for entry in release["entries"]:
        r = client.get(entry["audio_url"])
        assert r.status_code == 200
        path = out_dir / f"curated_{entry['position']}_src{entry['source_slot']}.wav"
        path.write_bytes(r.content)
    print(f"==> 逐格取回原声，已导出到 {out_dir.name}/")

    # 5. 撤回重叠批准
    r = check(client.delete(
        f"/sessions/{sid}/curated/approvals/{a1['approval_id']}", headers=reviewer_h))
    assert r["taken_down_release_id"] is None
    print("==> 撤回第一条批准后仍在播:",
          check(client.get(f"/sessions/{sid}/curated"))["status"])
    r = check(client.delete(
        f"/sessions/{sid}/curated/approvals/{a2['approval_id']}", headers=reviewer_h))
    print(f"==> 撤回最后一条覆盖批准，下架 release #{r['taken_down_release_id']}")
    r = client.get(f"/sessions/{sid}/curated")
    print(f"==> /curated -> HTTP {r.status_code}（已下架）")
    r = client.get(f"/sessions/{sid}/curated/slots/0/audio")
    print(f"==> 旧版取音 -> HTTP {r.status_code}（拿不到声音）")

    # 6. 原节目仍可回听
    for slot in range(6):
        r = client.get(f"/sessions/{sid}/slots/{slot}/audio")
        assert r.status_code == 200 and r.content == AUDIO[slot]
    print("==> 原节目 0..5 格仍可回听，字节与上传时一致")

    client.close()
    print("==> 演示完成")


if __name__ == "__main__":
    main()
