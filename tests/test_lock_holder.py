"""BUGFIX-08 红测先行：释锁必须绑定持有人。

红测一：乙（非持有人）能删甲持有的锁行 → 甲仍以为持锁，甲乙双写。
红测二：甲乙对同一 lock_key 一持一释，必须复现「盗释失败」并记「争用」。
红测三：持锁列表 / 票根回执 / 审计流水 / 窗口条带 / 争用 五处同态。
红测四：偏离《持有人释锁册》拒写并记「盗释偏了」。

本文件在业务修复前落地，必须先看到失败。
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "lock.db")
    init_db()
    yield


def _login(c: TestClient, username: str, password: str) -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


@pytest.fixture
def who():
    c = TestClient(app)
    return c, _login(c, "admin", "123456"), _login(c, "closer", "close123")


def _event(biz_key: str) -> dict:
    return {
        "window_name": "W-1102",
        "biz_key": biz_key,
        "event_ts": "2026-11-02T12:00:00",
        "amount": 8.8,
    }


def test_theft_release_rejected_and_no_double_write(who):
    """红测一：乙调用释放接口不得删掉甲的锁，乙也不得借此双写。"""
    c, h_admin, h_closer = who
    key = "ingest:W-1102"
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )

    # 乙释锁（并在请求体里冒充 holder，仍必须按登录身份判定）
    r = c.post(
        "/api/locks/release",
        headers=h_closer,
        json={"lock_key": key, "holder": "admin"},
    )
    assert r.status_code == 403, r.text

    # 锁行仍在甲名下，正式零改
    locks = c.get("/api/locks", headers=h_admin).json()["items"]
    held = [x for x in locks if x["lock_key"] == key]
    assert len(held) == 1
    assert held[0]["holder"] == "admin"

    # 乙不能夺锁，也不能写业务
    assert (
        c.post("/api/locks/acquire", headers=h_closer, json={"lock_key": key}).status_code
        == 409
    )
    assert c.post("/api/events", headers=h_closer, json=_event("ORD-B")).status_code == 409

    # 甲作为持有人仍可写；此时不存在乙的重复写入
    r = c.post("/api/events", headers=h_admin, json=_event("ORD-A"))
    assert r.status_code == 200, r.text
    events = c.get("/api/events", headers=h_admin).json()["items"]
    written = [e["biz_key"] for e in events if e["biz_key"] in ("ORD-A", "ORD-B")]
    assert written == ["ORD-A"]


def test_holder_release_collision_records_contention(who):
    """红测二：同一 lock_key 一持一释对撞，盗释失败且争用留痕。"""
    c, h_admin, h_closer = who
    key = "window:2"
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )

    # 甲持锁期间，乙先释后争，均须失败
    assert (
        c.post("/api/locks/release", headers=h_closer, json={"lock_key": key}).status_code
        == 403
    )
    assert (
        c.post("/api/locks/acquire", headers=h_closer, json={"lock_key": key}).status_code
        == 409
    )

    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    acts = {(x["lock_key"], x["actor"], x["action"]) for x in cont}
    assert (key, "closer", "盗释") in acts
    assert (key, "closer", "争用") in acts

    # 甲仍持锁；甲释放后乙才能拿
    held = [x for x in c.get("/api/locks", headers=h_admin).json()["items"] if x["lock_key"] == key]
    assert len(held) == 1 and held[0]["holder"] == "admin"
    assert (
        c.post("/api/locks/release", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )
    assert (
        c.post("/api/locks/acquire", headers=h_closer, json={"lock_key": key}).status_code
        == 200
    )


def test_five_surfaces_homomorphic(who):
    """红测三：盗释一次后，五处口径必须一致。"""
    c, h_admin, h_closer = who
    key = "window:2"
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )
    assert (
        c.post("/api/locks/release", headers=h_closer, json={"lock_key": key}).status_code
        == 403
    )

    # ① 持锁列表
    lock = next(
        x for x in c.get("/api/locks", headers=h_admin).json()["items"] if x["lock_key"] == key
    )
    assert lock["holder"] == "admin"

    # ② 票根回执
    receipts = c.get("/api/locks/receipts", headers=h_admin).json()["items"]
    mine = [x for x in receipts if x["lock_key"] == key]
    assert mine
    assert mine[0]["outcome"] == "盗释拒绝"
    assert mine[0]["actor"] == "closer"
    assert mine[0]["holder"] == "admin"

    # ③ 审计流水
    audits = c.get("/api/audit?limit=500", headers=h_admin).json()["items"]
    denied = [
        x
        for x in audits
        if x["action"] == "lock_release_denied" and x["target"] == key
    ]
    assert denied
    assert json.loads(denied[0]["payload"])["holder"] == "admin"

    # ④ 窗口条带
    win = next(
        x for x in c.get("/api/windows", headers=h_admin).json()["items"] if x["id"] == 2
    )
    assert win["lock_holder"] == "admin"

    # ⑤ 争用
    cont = [
        x
        for x in c.get("/api/contention", headers=h_admin).json()["items"]
        if x["lock_key"] == key
    ]
    assert any(x["action"] == "盗释" and x["actor"] == "closer" for x in cont)

    # 《持有人释锁册》：当天口径为甲
    ledger = c.get("/api/locks/ledger", headers=h_admin).json()
    assert ledger["path"] == "甲"


def test_ledger_deviation_refused_and_recorded(who):
    c, h_admin, _h_closer = who
    key = "window:2"
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )

    from db import connect

    with connect() as conn:
        conn.execute("DELETE FROM holder_release_ledger")
        conn.execute(
            "INSERT INTO holder_release_ledger(decided_on,path,note,created_at) VALUES (?,?,?,?)",
            (date.today().isoformat(), "乙", "篡改演练", "2026-11-21T00:00:00"),
        )
        conn.commit()

    r = c.post("/api/locks/release", headers=h_admin, json={"lock_key": key})
    assert r.status_code == 409, r.text

    # 锁行不被删除
    held = [x for x in c.get("/api/locks", headers=h_admin).json()["items"] if x["lock_key"] == key]
    assert len(held) == 1 and held[0]["holder"] == "admin"

    audits = c.get("/api/audit?limit=500", headers=h_admin).json()["items"]
    assert any(
        x["action"] == "盗释偏了" and x["target"] == key for x in audits
    ), audits


def test_ledger_same_day_change_refused(who):
    """当天改口必拒：已有甲决议时登记乙返回 409，决议保持甲。"""
    c, h_admin, _h_closer = who
    r = c.post("/api/locks/ledger", headers=h_admin, json={"path": "乙"})
    assert r.status_code == 409, r.text
    assert c.get("/api/locks/ledger", headers=h_admin).json()["path"] == "甲"


def test_release_denies_on_non_holder_but_holder_succeeds(who):
    """只改无权文案不过关：拒绝必须是零改，而非仅提示语变化。"""
    c, h_admin, h_closer = who
    key = "materialize:2"
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )
    r = c.post("/api/locks/release", headers=h_closer, json={"lock_key": key})
    assert r.status_code == 403
    # 锁行仍是甲 → 文案之外的正式态未变
    held = [x for x in c.get("/api/locks", headers=h_admin).json()["items"] if x["lock_key"] == key]
    assert len(held) == 1 and held[0]["holder"] == "admin"
    # 甲（持有人）释放成功；再释一次 404
    assert (
        c.post("/api/locks/release", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )
    assert (
        c.post("/api/locks/release", headers=h_admin, json={"lock_key": key}).status_code
        == 404
    )


def test_contention_loser_not_labelled_as_theft(who):
    """业务接口争用失败只记争用，不得被 finally 误记成盗释。"""
    c, h_admin, h_closer = who
    key = "ingest:W-1102"
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": key}).status_code
        == 200
    )
    r = c.post("/api/events", headers=h_closer, json=_event("ORD-C1"))
    assert r.status_code == 409

    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    mine = [x for x in cont if x["lock_key"] == key]
    assert any(x["actor"] == "closer" and x["action"] == "争用" for x in mine), mine
    assert all(x["action"] != "盗释" for x in mine), mine

    receipts = c.get("/api/locks/receipts", headers=h_admin).json()["items"]
    assert all(x["outcome"] != "盗释拒绝" for x in receipts), receipts
    # 甲仍持锁，业务零改
    held = [x for x in c.get("/api/locks", headers=h_admin).json()["items"] if x["lock_key"] == key]
    assert len(held) == 1 and held[0]["holder"] == "admin"


def test_fail_simulated_materialize_still_releases_only_holder(who):
    """业务失败分支的 finally 释锁同样只认持有人，票根可查。"""
    c, h_admin, h_closer = who
    assert c.post("/api/windows/2/close", headers=h_admin).status_code == 200
    r = c.post(
        "/api/windows/2/materialize", headers=h_admin, json={"fail_simulate": True}
    )
    assert r.status_code == 500
    assert c.get("/api/locks", headers=h_admin).json()["items"] == []
    receipts = c.get("/api/locks/receipts", headers=h_admin).json()["items"]
    outcomes = [(x["actor"], x["outcome"]) for x in receipts]
    assert ("admin", "持有人释锁") in outcomes
    assert ("closer", "盗释拒绝") not in outcomes
