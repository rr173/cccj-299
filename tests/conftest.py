"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_backend.main import create_app  # noqa: E402
from relay_backend.wav import make_mono_wav  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "test_relay.db"
    app = create_app(db_file)
    with TestClient(app) as c:
        c.db_file = db_file  # type: ignore[attr-defined]
        yield c


def make_session(client: TestClient, title: str = "test") -> tuple[int, str]:
    r = client.post("/sessions", json={"title": title})
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], data["staff_token"]


def assign(client: TestClient, sid: int, staff: str, interpreter: str, start: int) -> dict:
    r = client.put(
        f"/sessions/{sid}/duties",
        json={"interpreter": interpreter, "start_slot": start},
        headers={"X-Staff-Token": staff},
    )
    assert r.status_code == 201, r.text
    return r.json()


def upload(client: TestClient, duty_id: int, token: str, slot: int, wav: bytes) -> dict:
    r = client.post(
        f"/duties/{duty_id}/slots/{slot}/audio",
        content=wav,
        headers={"X-Duty-Token": token, "Content-Type": "audio/wav"},
    )
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


# 两位口译员使用可区分的频率
WAV_A = make_mono_wav(300)
WAV_B = make_mono_wav(500)
