#!/usr/bin/env python3
"""本地端到端演示（不做实时编解码，也没有界面）。

流程
----
1. 用 SQLite 文件启动 FastAPI 应用（进程内 TestClient，等价于真实 HTTP 路由）；
2. 工作人员安排 alice（0 起）与 bob（4 起）两班；
3. 口译端按*打乱顺序*上传 2 秒单声道 WAV，并演示：
   - 掉线后在本班负责范围内补传；
   - 缺口前的声音先收但不发布；
   - 空格记静音后封闭；
   - 换班导致旧声音失去资格、恢复负责后需重新明确提交；
4. 听众从接力前一格（3）回听，导出每格 WAV 到 demo/out，可用任意播放器验证；
5. 关闭并重新创建应用（同库文件），证明重启后续接、已发布声音仍可回听。

用法::

    python demo/run_demo.py [--db demo/out/demo_relay.db] [--out demo/out]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from relay_backend.main import create_app  # noqa: E402
from relay_backend.wav import make_mono_wav  # noqa: E402

# 每个口译员一个可辨频率，回听时能直接听出是谁
VOICES = {
    "alice": (220, make_mono_wav(220)),
    "bob": (330, make_mono_wav(330)),
    "carol": (440, make_mono_wav(440)),
}


def check(resp, expected: int = 200) -> dict:
    if resp.status_code != expected:
        raise SystemExit(
            f"API 调用失败: {resp.request.method} {resp.request.url}\n"
            f"  期望 {expected}，实际 {resp.status_code}: {resp.text}"
        )
    if resp.content:
        return resp.json()
    return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out_dir = Path(args.out or Path(__file__).parent / "out").resolve()
    db_path = Path(args.db or out_dir / "demo_relay.db").resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    print(f"==> 数据库: {db_path}")
    app = create_app(db_path)
    client = TestClient(app)

    # 1. 建场次 --------------------------------------------------------------
    session = check(client.post("/sessions", json={"title": "会场A-中英接力"}), 201)
    sid = session["session_id"]
    staff = session["staff_token"]
    staff_h = {"X-Staff-Token": staff}
    print(f"==> 场次 #{sid}（每格 {session['slot_seconds']} 秒，格号从 0 开始）")

    # 2. 排班：alice 0..3，bob 4.. -------------------------------------------
    alice = check(client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "alice", "start_slot": 0}, headers=staff_h), 201)
    bob = check(client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "bob", "start_slot": 4}, headers=staff_h), 201)
    print(f"==> 排班: alice=duty#{alice['duty_id']} 起点0, "
          f"bob=duty#{bob['duty_id']} 起点4")

    # 3. 乱序上传 -------------------------------------------------------------
    def up(duty: dict, slot: int, who: str) -> None:
        r = client.post(
            f"/duties/{duty['duty_id']}/slots/{slot}/audio",
            content=VOICES[who][1],
            headers={"X-Duty-Token": duty["duty_token"],
                     "Content-Type": "audio/wav"})
        data = check(r, 201)
        print(f"    上传 格{slot} <- {who}(duty#{duty['duty_id']}) "
              f"published={data['published']} frontier={data['frontier']}")

    print("==> 打乱顺序上传（bob 先传、alice 补传，模拟掉线重连）:")
    up(bob, 4, "bob")       # 新班先交，前面有缺口 -> 先收不发布
    up(alice, 1, "alice")   # alice 掉线回来补格 1；格 0 仍缺 -> 卡在 0
    up(bob, 5, "bob")
    up(alice, 0, "alice")   # 格 0 补齐，frontier 连续推进
    up(alice, 3, "alice")   # 故意跳过格 2
    up(bob, 6, "bob")

    host = check(client.get(f"/sessions/{sid}/host", headers=staff_h))
    print(f"==> 主持端: frontier={host['frontier']} "
          f"卡在格{host['blocked']['slot']}（{host['blocked']['reason']}）")

    # 4. 格 2 等不到人，工作人员记静音 ---------------------------------------
    print("==> 格2 等待无果，工作人员记为静音（之后不再接受补传）")
    check(client.put(f"/sessions/{sid}/slots/2/silence", headers=staff_h), 201)
    # 静音后再传必被拒
    r = client.post(
        f"/duties/{alice['duty_id']}/slots/2/audio",
        content=VOICES["alice"][1],
        headers={"X-Duty-Token": alice["duty_token"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "slot_marked_silence"
    print("    静音后的补传被拒绝: slot_marked_silence")

    # 5. 演示换班失权 + 恢复后重新明确提交 -----------------------------------
    print("==> 临时改班: carol 从格 6 接起，bob 的格6声音失去资格")
    carol = check(client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": "carol", "start_slot": 6}, headers=staff_h), 201)
    print(f"    本次改班取消资格的提交数: {carol['disqualified_submissions']}")
    # carol 起点再推迟到 7，格 6 重新归 bob，但旧声音不会静默恢复
    check(client.patch(f"/duties/{carol['duty_id']}",
                       json={"start_slot": 7}, headers=staff_h))
    host = check(client.get(f"/sessions/{sid}/host",
                            params={"start": 6, "limit": 2}, headers=staff_h))
    s6 = host["slots"][0]
    print(f"    格6 状态: {s6['state']}（owner 恢复为 bob，但旧声音仍 disqualified）")
    up(bob, 6, "bob")       # bob 重新明确提交
    up(carol, 7, "carol")

    # 6. 听众从接力前一格（3）回听，并导出 WAV --------------------------------
    print("==> 听众从接力前一格（格3）回听连续节目:")
    program = check(client.get(f"/sessions/{sid}/program",
                               params={"start": 3, "limit": 10}))
    for entry in program["entries"]:
        if entry["kind"] == "silence":
            print(f"    格{entry['slot']}: [静音]")
            continue
        r = client.get(entry["audio_url"])
        assert r.status_code == 200
        wav_path = out_dir / f"slot_{entry['slot']}_{entry['interpreter']}.wav"
        wav_path.write_bytes(r.content)
        with wave.open(str(wav_path), "rb") as wf:
            dur = wf.getnframes() / wf.getframerate()
        print(f"    格{entry['slot']}: {entry['interpreter']:>5} "
              f"({dur:.1f}s, {VOICES[entry['interpreter']][0]}Hz) -> {wav_path.name}")

    # 7. 模拟服务重启：同库文件重建应用 --------------------------------------
    print("==> 模拟服务重启（关闭应用，用同一 SQLite 文件重新启动）")
    client.close()
    app2 = create_app(db_path)
    client2 = TestClient(app2)
    info = check(client2.get(f"/sessions/{sid}"))
    replay = check(client2.get(f"/sessions/{sid}/program",
                               params={"start": 3, "limit": 10}))
    print(f"    重启后 frontier={info['frontier']}，回听条目数={len(replay['entries'])}")
    r = client2.get(f"/sessions/{sid}/slots/4/audio")
    assert r.status_code == 200 and r.content == VOICES["bob"][1]
    print("    重启后格4声音字节与上传时完全一致，回听不受影响")

    # 8. 尝试各种违规操作，确认被拒 ------------------------------------------
    print("==> 违规操作全部拒绝:")
    cases = [
        ("回填已发布格", client2.post(
            f"/duties/{alice['duty_id']}/slots/0/audio",
            content=VOICES["alice"][1],
            headers={"X-Duty-Token": alice["duty_token"]})),
        ("跨班上传非负责格", client2.post(
            f"/duties/{alice['duty_id']}/slots/8/audio",
            content=VOICES["alice"][1],
            headers={"X-Duty-Token": alice["duty_token"]})),
        ("错误 duty token", client2.post(
            f"/duties/{bob['duty_id']}/slots/8/audio",
            content=VOICES["bob"][1], headers={"X-Duty-Token": "duty_bad"})),
        ("无 staff token 改班", client2.patch(
            f"/duties/{carol['duty_id']}", json={"start_slot": 8})),
    ]
    for label, resp in cases:
        print(f"    {label}: HTTP {resp.status_code} "
              f"{resp.json()['detail']['error']}")
        assert resp.status_code in (401, 403, 404, 409)

    client2.close()
    print(f"==> 演示完成，可回听文件位于: {out_dir}")


if __name__ == "__main__":
    main()
