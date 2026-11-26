"""核账、付款单与回执的小型行为测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from relay_backend.main import create_app

from conftest import WAV_A, WAV_B, assign, make_session


def _staff_headers(token: str) -> dict[str, str]:
    return {"X-Staff-Token": token}


def _publish_four_slots(client: TestClient, sid: int, staff: str) -> tuple[dict, dict]:
    d1 = assign(client, sid, staff, "alice", 0)
    d2 = assign(client, sid, staff, "bob", 2)
    h1 = {"X-Duty-Token": d1["duty_token"], "Content-Type": "audio/wav"}
    h2 = {"X-Duty-Token": d2["duty_token"], "Content-Type": "audio/wav"}

    # 格 1 是缺口；格 2、3 虽已上传，但只能暂存，不能进入连续发布前缀。
    assert client.post(f"/duties/{d1['duty_id']}/slots/0/audio",
                       content=WAV_A, headers=h1).status_code == 201
    for slot in (2, 3):
        assert client.post(f"/duties/{d2['duty_id']}/slots/{slot}/audio",
                           content=WAV_B, headers=h2).status_code == 201
    assert client.get(f"/sessions/{sid}/program").json()["frontier"] == 1
    return d1, d2


def test_gap_backfill_unlocks_tail_and_negative_instruction_still_receipts(
    client: TestClient,
) -> None:
    sid, staff = make_session(client)
    headers = _staff_headers(staff)
    d1, d2 = _publish_four_slots(client, sid, staff)

    # 缺口未补时，核到格 4 会越过已发布连续范围。
    premature = client.post(
        f"/sessions/{sid}/billing/versions",
        headers=headers,
        json={
            "base_version": 0,
            "cutoff_slot": 4,
            "entries": [
                {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 2,
                 "payee_id": "A", "unit_amount_cents": 100},
                {"duty_id": d2["duty_id"], "start_slot": 2, "end_slot": 4,
                 "payee_id": "B", "unit_amount_cents": 100},
            ],
        },
    )
    assert premature.status_code == 409
    assert premature.json()["detail"]["error"] == \
        "cutoff_beyond_published_range"

    # 第一次只能核已发布的格 0。
    first = client.post(
        f"/sessions/{sid}/billing/versions",
        headers=headers,
        json={
            "base_version": 0,
            "cutoff_slot": 1,
            "entries": [
                {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 2,
                 "payee_id": "A", "unit_amount_cents": 100},
            ],
        },
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["version"] == 1
    assert first_body["cutoff_slot"] == 1
    assert first_body["entries"][0]["voiced_slot_count"] == 1
    assert first_body["entries"][0]["amount_cents"] == 100

    # 补齐格 1 后，连续前缀推进到 4，后段 bob 的两个原声格此时才可计费。
    h1 = {"X-Duty-Token": d1["duty_token"], "Content-Type": "audio/wav"}
    assert client.post(f"/duties/{d1['duty_id']}/slots/1/audio",
                       content=WAV_A, headers=h1).status_code == 201
    assert client.get(f"/sessions/{sid}/program").json()["frontier"] == 4

    second = client.post(
        f"/sessions/{sid}/billing/versions",
        headers=headers,
        json={
            "base_version": 1,
            "cutoff_slot": 4,
            "entries": [
                {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 2,
                 "payee_id": "A", "unit_amount_cents": 100},
                {"duty_id": d2["duty_id"], "start_slot": 2, "end_slot": 4,
                 "payee_id": "B", "unit_amount_cents": 100},
            ],
        },
    )
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["version"] == 2
    by_duty = {d["duty_id"]: d for d in second_body["duties"]}
    assert by_duty[d1["duty_id"]]["voiced_slot_count"] == 2
    assert by_duty[d1["duty_id"]]["amount_cents"] == 200
    assert by_duty[d2["duty_id"]]["voiced_slot_count"] == 2
    assert by_duty[d2["duty_id"]]["amount_cents"] == 200

    # 出纳依据账版 2 给 A 开出 200 分的待回执指令。
    created = client.post(
        f"/sessions/{sid}/payment-orders",
        headers=headers,
        json={
            "request_id": "payout-A-1",
            "account_version": 2,
            "payee_ids": ["A"],
        },
    )
    assert created.status_code == 201, created.text
    order = created.json()
    order_id = order["payment_order_id"]
    item_id = order["items"][0]["payment_item_id"]
    assert order["items"][0]["amount_cents"] == 200
    balance = client.get(f"/sessions/{sid}/payees/A/balance",
                         headers=headers).json()
    assert balance["occupied_cents"] == 200
    assert balance["balance_cents"] == 0

    # 更正 A 的费率：两个格各 50，应付变为 100；旧指令仍占用 200，余额为 -100。
    corrected = client.post(
        f"/sessions/{sid}/billing/versions",
        headers=headers,
        json={
            "base_version": 2,
            "cutoff_slot": 4,
            "entries": [
                {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 2,
                 "payee_id": "A", "unit_amount_cents": 50},
                {"duty_id": d2["duty_id"], "start_slot": 2, "end_slot": 4,
                 "payee_id": "B", "unit_amount_cents": 100},
            ],
        },
    )
    assert corrected.status_code == 201, corrected.text
    balance = client.get(f"/sessions/{sid}/payees/A/balance",
                         headers=headers).json()
    assert balance["current_payable_cents"] == 100
    assert balance["occupied_cents"] == 200
    assert balance["balance_cents"] == -100

    # 旧付款指令金额不变，仍可凭金额相符的外部回执确认已付。
    receipt = client.post(
        f"/sessions/{sid}/payment-orders/{order_id}/receipts",
        headers=headers,
        json={"receipts": [
            {"payment_item_id": item_id, "status": "paid",
             "external_receipt_no": "EXT-A-200", "amount_cents": 200},
        ]},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["items"][0]["status"] == "paid"

    balance = client.get(f"/sessions/{sid}/payees/A/balance",
                         headers=headers).json()
    assert balance["paid_cents"] == 200
    assert balance["occupied_cents"] == 0
    assert balance["balance_cents"] == -100

    # 旧账版 2 仍保留更正前的 200 分，不随 v3 改变。
    old_v2 = client.get(f"/sessions/{sid}/billing/versions/2",
                        headers=headers).json()
    assert old_v2["payees"][0]["amount_cents"] == 200

    # 后续应付增加到 300 时，先抵掉 -100 的多付差额，剩余余额为 100。
    make_whole = client.post(
        f"/sessions/{sid}/billing/versions",
        headers=headers,
        json={
            "base_version": 3,
            "cutoff_slot": 4,
            "entries": [
                {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 2,
                 "payee_id": "A", "unit_amount_cents": 150},
                {"duty_id": d2["duty_id"], "start_slot": 2, "end_slot": 4,
                 "payee_id": "B", "unit_amount_cents": 100},
            ],
        },
    )
    assert make_whole.status_code == 201, make_whole.text
    balance = client.get(f"/sessions/{sid}/payees/A/balance",
                         headers=headers).json()
    assert balance["current_payable_cents"] == 300
    assert balance["balance_cents"] == 100

    # 用相同请求标识和相同旧内容重试，只返回原单，不会再占用或多付。
    retry = client.post(
        f"/sessions/{sid}/payment-orders",
        headers=headers,
        json={
            "request_id": "payout-A-1",
            "account_version": 2,
            "payee_ids": ["A"],
        },
    )
    assert retry.status_code == 200
    assert retry.json()["payment_order_id"] == order_id
    assert retry.json()["items"][0]["payment_item_id"] == item_id
    orders = client.get(f"/sessions/{sid}/payment-orders",
                        headers=headers).json()["payment_orders"]
    assert len(orders) == 1

    # 旧账版仍保留当时的截止格、格数和分项金额。
    old = client.get(f"/sessions/{sid}/billing/versions/1",
                     headers=headers).json()
    assert old["cutoff_slot"] == 1
    assert old["entries"][0]["voiced_slot_count"] == 1
    assert old["entries"][0]["amount_cents"] == 100


def test_idempotency_conflict_stale_version_receipt_atomicity_and_unpaid_reissue(
    client: TestClient,
) -> None:
    sid, staff = make_session(client)
    h = _staff_headers(staff)
    d1 = assign(client, sid, staff, "alice", 0)
    upload_h = {"X-Duty-Token": d1["duty_token"],
                "Content-Type": "audio/wav"}
    for slot in range(2):
        assert client.post(f"/duties/{d1['duty_id']}/slots/{slot}/audio",
                           content=WAV_A, headers=upload_h).status_code == 201

    audit = client.post(
        f"/sessions/{sid}/billing/versions", headers=h,
        json={"base_version": 0, "cutoff_slot": 2, "entries": [
            {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 2,
             "payee_id": "A", "unit_amount_cents": 100},
        ]},
    )
    assert audit.status_code == 201, audit.text

    order_req = {
        "request_id": "req-1", "account_version": 1, "payee_ids": ["A"],
    }
    first = client.post(f"/sessions/{sid}/payment-orders",
                        headers=h, json=order_req)
    assert first.status_code == 201, first.text
    order = first.json()
    oid = order["payment_order_id"]
    iid = order["items"][0]["payment_item_id"]

    same = client.post(f"/sessions/{sid}/payment-orders",
                       headers=h, json=order_req)
    assert same.status_code == 200
    assert same.json()["payment_order_id"] == oid

    conflict_body = {**order_req, "account_version": 99}
    conflict = client.post(f"/sessions/{sid}/payment-orders",
                           headers=h, json=conflict_body)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == \
        "idempotency_key_content_conflict"

    # 新请求不能借用旧账版。
    stale = client.post(
        f"/sessions/{sid}/payment-orders", headers=h,
        json={"request_id": "req-2", "account_version": 0,
              "payee_ids": ["A"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_account_version"

    # 另一收款方没有正余额，不开单。
    empty = client.post(
        f"/sessions/{sid}/payment-orders", headers=h,
        json={"request_id": "req-empty", "account_version": 1,
              "payee_ids": ["NOBODY"]},
    )
    assert empty.status_code == 200
    assert empty.json()["payment_order_id"] is None
    assert client.get(f"/sessions/{sid}/payment-orders",
                      headers=h).json()["payment_orders"] != []

    # 一个批次中第二行使用重复回执号/矛盾结果：整批拒绝，第一行也不能生效。
    bad_batch = client.post(
        f"/sessions/{sid}/payment-orders/{oid}/receipts", headers=h,
        json={"receipts": [
            {"payment_item_id": iid, "status": "paid",
             "external_receipt_no": "DUP", "amount_cents": 200},
            {"payment_item_id": 999999, "status": "unpaid"},
        ]},
    )
    assert bad_batch.status_code == 404
    item = client.get(f"/sessions/{sid}/payment-orders/{oid}",
                      headers=h).json()["items"][0]
    assert item["status"] == "pending"

    unpaid = client.post(
        f"/sessions/{sid}/payment-orders/{oid}/receipts", headers=h,
        json={"receipts": [{"payment_item_id": iid, "status": "unpaid"}]},
    )
    assert unpaid.status_code == 200
    assert unpaid.json()["items"][0]["status"] == "unpaid"

    # 重复未付无副作用；改成已付属于矛盾结果。
    replay_unpaid = client.post(
        f"/sessions/{sid}/payment-orders/{oid}/receipts", headers=h,
        json={"receipts": [{"payment_item_id": iid, "status": "unpaid"}]},
    )
    assert replay_unpaid.status_code == 200
    contradictory = client.post(
        f"/sessions/{sid}/payment-orders/{oid}/receipts", headers=h,
        json={"receipts": [
            {"payment_item_id": iid, "status": "paid",
             "external_receipt_no": "LATE", "amount_cents": 200},
        ]},
    )
    assert contradictory.status_code == 409

    balance = client.get(f"/sessions/{sid}/payees/A/balance",
                         headers=h).json()
    assert balance["occupied_cents"] == 0
    assert balance["balance_cents"] == 200

    # 未付释放占用后，可用新请求按当前余额开新单；重试旧请求仍是旧单。
    second_order = client.post(
        f"/sessions/{sid}/payment-orders", headers=h,
        json={"request_id": "req-3", "account_version": 1,
              "payee_ids": ["A"]},
    )
    assert second_order.status_code == 201
    assert second_order.json()["payment_order_id"] != oid
    old_retry = client.post(f"/sessions/{sid}/payment-orders",
                           headers=h, json=order_req)
    assert old_retry.json()["payment_order_id"] == oid
    orders = client.get(f"/sessions/{sid}/payment-orders",
                        headers=h).json()["payment_orders"]
    assert [o["id"] for o in orders] == [oid, second_order.json()["id"]]


def test_concurrent_new_orders_do_not_double_occupy_balance(
    client: TestClient,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    sid, staff = make_session(client)
    h = _staff_headers(staff)
    d1 = assign(client, sid, staff, "alice", 0)
    d2 = assign(client, sid, staff, "bob", 1)
    for duty, slot, wav in (
        (d1, 0, WAV_A),
        (d2, 1, WAV_B),
    ):
        uh = {"X-Duty-Token": duty["duty_token"],
              "Content-Type": "audio/wav"}
        assert client.post(f"/duties/{duty['duty_id']}/slots/{slot}/audio",
                           content=wav, headers=uh).status_code == 201
    assert client.post(
        f"/sessions/{sid}/billing/versions", headers=h,
        json={"base_version": 0, "cutoff_slot": 2, "entries": [
            {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 1,
             "payee_id": "A", "unit_amount_cents": 100},
            {"duty_id": d2["duty_id"], "start_slot": 1, "end_slot": 2,
             "payee_id": "B", "unit_amount_cents": 100},
        ]},
    ).status_code == 201

    barrier = threading.Barrier(6)

    def distinct_order(i: int) -> tuple[int, int | None]:
        local = TestClient(client.app)
        barrier.wait()
        r = local.post(
            f"/sessions/{sid}/payment-orders", headers=h,
            json={"request_id": f"distinct-{i}", "account_version": 1,
                  "payee_ids": ["A"]},
        )
        return r.status_code, r.json().get("payment_order_id")

    with ThreadPoolExecutor(max_workers=6) as pool:
        distinct_results = list(pool.map(distinct_order, range(6)))
    created = [x for x in distinct_results if x[0] == 201]
    assert len(created) == 1
    assert all(x[1] is None for x in distinct_results if x[0] == 200)

    barrier = threading.Barrier(6)

    def same_order(_i: int) -> tuple[int, int | None]:
        local = TestClient(client.app)
        barrier.wait()
        r = local.post(
            f"/sessions/{sid}/payment-orders", headers=h,
            json={"request_id": "same-key", "account_version": 1,
                  "payee_ids": ["B"]},
        )
        return r.status_code, r.json().get("payment_order_id")

    with ThreadPoolExecutor(max_workers=6) as pool:
        same_results = list(pool.map(same_order, range(6)))
    order_ids = {order_id for _status, order_id in same_results}
    assert len(order_ids) == 1
    assert None not in order_ids
    assert sorted(status for status, _oid in same_results).count(201) == 1

    orders = client.get(f"/sessions/{sid}/payment-orders",
                        headers=h).json()["payment_orders"]
    assert len(orders) == 2
    balance_a = client.get(f"/sessions/{sid}/payees/A/balance",
                           headers=h).json()
    assert balance_a["occupied_cents"] == 100
    assert balance_a["balance_cents"] == 0


def test_receipt_number_session_unique_and_restart_persistence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "billing_restart.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        sid, staff = make_session(client)
        h = _staff_headers(staff)
        d1 = assign(client, sid, staff, "alice", 0)
        d2 = assign(client, sid, staff, "bob", 1)
        for duty, slot, wav in (
            (d1, 0, WAV_A),
            (d2, 1, WAV_B),
        ):
            uh = {"X-Duty-Token": duty["duty_token"],
                  "Content-Type": "audio/wav"}
            assert client.post(f"/duties/{duty['duty_id']}/slots/{slot}/audio",
                               content=wav, headers=uh).status_code == 201
        assert client.post(
            f"/sessions/{sid}/billing/versions", headers=h,
            json={"base_version": 0, "cutoff_slot": 2, "entries": [
                {"duty_id": d1["duty_id"], "start_slot": 0, "end_slot": 1,
                 "payee_id": "A", "unit_amount_cents": 10},
                {"duty_id": d2["duty_id"], "start_slot": 1, "end_slot": 2,
                 "payee_id": "B", "unit_amount_cents": 20},
            ]},
        ).status_code == 201

        order = client.post(
            f"/sessions/{sid}/payment-orders", headers=h,
            json={"request_id": "batch", "account_version": 1,
                  "payee_ids": ["A", "B"]},
        ).json()
        assert len(order["items"]) == 2
        items = {i["payee_id"]: i for i in order["items"]}

        atomic_duplicate = client.post(
            f"/sessions/{sid}/payment-orders/{order['id']}/receipts",
            headers=h,
            json={"receipts": [
                {"item_id": items["A"]["id"], "status": "paid",
                 "receipt_no": "BATCH-DUP", "amount_cents": 10},
                {"item_id": items["B"]["id"], "status": "paid",
                 "receipt_no": "BATCH-DUP", "amount_cents": 20},
            ]},
        )
        assert atomic_duplicate.status_code == 409
        pending_order = client.get(
            f"/sessions/{sid}/payment-orders/{order['id']}", headers=h
        ).json()
        assert {i["status"] for i in pending_order["items"]} == {"pending"}

        paid_a = client.post(
            f"/sessions/{sid}/payment-orders/{order['id']}/receipts",
            headers=h,
            json={"receipts": [
                {"item_id": items["A"]["id"], "status": "paid",
                 "receipt_no": "SHARED-NO", "amount_cents": 10},
            ]},
        )
        assert paid_a.status_code == 200, paid_a.text
        repeat_paid = client.post(
            f"/sessions/{sid}/payment-orders/{order['id']}/receipts",
            headers=h,
            json={"receipts": [
                {"item_id": items["A"]["id"], "status": "paid",
                 "receipt_no": "SHARED-NO", "amount_cents": 10},
            ]},
        )
        assert repeat_paid.status_code == 200
        assert repeat_paid.json()["items"][0]["status"] == "paid"

        duplicate = client.post(
            f"/sessions/{sid}/payment-orders/{order['id']}/receipts",
            headers=h,
            json={"receipts": [
                {"item_id": items["B"]["id"], "status": "paid",
                 "receipt_no": "SHARED-NO", "amount_cents": 20},
            ]},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["error"] == \
            "external_receipt_no_already_used"

    # 重新打开同一 SQLite 文件：账版、单据、回执和余额都还在。
    app2 = create_app(db_path)
    with TestClient(app2) as client2:
        h = _staff_headers(staff)
        versions = client2.get(f"/sessions/{sid}/billing/versions",
                              headers=h).json()
        assert versions["current_version"] == 1
        assert versions["versions"][0]["duties"][0]["voiced_slot_count"] == 1

        loaded_order = client2.get(
            f"/sessions/{sid}/payment-orders/{order['id']}", headers=h
        ).json()
        statuses = {i["payee_id"]: i["status"] for i in loaded_order["items"]}
        assert statuses == {"A": "paid", "B": "pending"}

        balances = {
            b["payee_id"]: b
            for b in client2.get(f"/sessions/{sid}/balances",
                                 headers=h).json()["balances"]
        }
        assert balances["A"]["paid_cents"] == 10
        assert balances["A"]["balance_cents"] == 0
        assert balances["B"]["occupied_cents"] == 20
        assert balances["B"]["balance_cents"] == 0
