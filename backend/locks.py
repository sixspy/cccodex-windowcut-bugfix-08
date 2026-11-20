"""窗口锁与争用登记：释锁绑定持有人（BUGFIX-08 修复）。

正确性口径（《持有人释锁册》当天唯一口径）：甲路径。
- 请求用户 != holder 时拒绝删除锁行，正式零改；
- 释锁成功/拒绝/偏离都落票根回执、审计流水与争用，保证五处同态；
- 偏离释锁册口径时拒写并记「盗释偏了」。
"""
from __future__ import annotations

import secrets
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import HOLDER_RELEASE_PATH, connect, write_audit

router = APIRouter(tags=["locks"])

# 甲路径 = 持有人门禁拒绝；乙路径（hold_ticket 同事务核销）不得混用。
CHOSEN_PATH = HOLDER_RELEASE_PATH
LEDGER_PATHS = ("甲", "乙")


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


class LedgerIn(BaseModel):
    path: str
    note: str | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _ticket() -> str:
    return "T-" + secrets.token_hex(8)


def ensure_ledger(conn) -> dict:
    """当天无决议则写入甲路径；已有决议原样保留（当天禁止改口）。"""
    row = conn.execute(
        "SELECT * FROM holder_release_ledger WHERE decided_on=?", (_today(),)
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO holder_release_ledger(decided_on,path,note,created_at)
               VALUES (?,?,?,?)""",
            (
                _today(),
                CHOSEN_PATH,
                "甲路径：请求用户不等于 holder 则拒绝删锁行，正式零改",
                _now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM holder_release_ledger WHERE decided_on=?", (_today(),)
        ).fetchone()
    return dict(row)


def ledger_path(conn) -> str:
    return ensure_ledger(conn)["path"]


def record_contention(conn, lock_key: str, actor: str, action: str, detail: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, actor, action, detail, _now()),
    )


def record_receipt(
    conn,
    lock_key: str,
    actor: str,
    holder: str | None,
    action: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO lock_receipts(ticket,lock_key,actor,holder,action,outcome,detail,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (_ticket(), lock_key, actor, holder, action, outcome, detail, _now()),
    )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = _now()
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now),
        )
        record_receipt(conn, lock_key, holder, holder, "acquire", "持有")
        return True
    if row["holder"] == holder:
        return True
    record_contention(conn, lock_key, holder, "争用", f"持有者={row['holder']}")
    record_receipt(
        conn, lock_key, holder, row["holder"], "acquire", "争用", f"持有者={row['holder']}"
    )
    return False


def release(conn, lock_key: str, holder: str) -> str:
    """持有人门禁释锁，返回 released / denied / missing / deviated。

    非 holder 一律零改：不删除锁行，只记争用、票根与审计。
    """
    path = ledger_path(conn)
    if path != CHOSEN_PATH:
        detail = f"释锁册口径={path}，本进程口径={CHOSEN_PATH}"
        record_contention(conn, lock_key, holder, "盗释偏了", detail)
        record_receipt(conn, lock_key, holder, None, "release", "盗释偏了", detail)
        write_audit(
            conn,
            holder,
            "盗释偏了",
            lock_key,
            {"ledger_path": path, "chosen": CHOSEN_PATH},
        )
        return "deviated"

    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        return "missing"
    if row["holder"] != holder:
        detail = f"持有者={row['holder']}"
        record_contention(conn, lock_key, holder, "盗释", detail)
        record_contention(conn, lock_key, holder, "争用", f"持释对撞 {detail}")
        record_receipt(
            conn, lock_key, holder, row["holder"], "release", "盗释拒绝", detail
        )
        write_audit(
            conn, holder, "lock_release_denied", lock_key, {"holder": row["holder"]}
        )
        return "denied"

    cur = conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?", (lock_key, holder)
    )
    if cur.rowcount == 0:
        # 并发窗口里持有人被换掉：按盗释处理，不动任何行。
        record_contention(conn, lock_key, holder, "盗释", "并发换手")
        record_contention(conn, lock_key, holder, "争用", "并发换手")
        record_receipt(conn, lock_key, holder, holder, "release", "盗释拒绝", "并发换手")
        write_audit(conn, holder, "lock_release_denied", lock_key, {"holder": holder})
        return "denied"
    record_receipt(conn, lock_key, holder, holder, "release", "持有人释锁")
    write_audit(conn, holder, "lock_release", lock_key, {"holder": holder})
    return "released"


def release_held(conn, lock_key: str, holder: str) -> bool:
    """业务 finally 用：只删自己持有的锁行，不产生盗释记录。

    业务请求在 require_lock 失败时不会走到这里（由释放接口承担严格审计），
    因此这里保持静默，避免把争用失败者误记成盗释。
    """
    cur = conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?", (lock_key, holder)
    )
    if cur.rowcount:
        record_receipt(conn, lock_key, holder, holder, "release", "持有人释锁")
        write_audit(conn, holder, "lock_release", lock_key, {"holder": holder})
        return True
    return False


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        # 争用与票根先落库再抛：连接回滚不得吞掉争用记录。
        conn.commit()
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/locks/receipts")
def list_receipts(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lock_receipts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/locks/ledger")
def get_ledger(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = ensure_ledger(conn)
        conn.commit()
        return {
            "decided_on": row["decided_on"],
            "path": row["path"],
            "note": row["note"],
            "chosen": CHOSEN_PATH,
            "consistent": row["path"] == CHOSEN_PATH,
        }


@router.post("/api/locks/ledger")
def set_ledger(body: LedgerIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    if body.path not in LEDGER_PATHS:
        raise HTTPException(400, "口径须为甲或乙")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM holder_release_ledger WHERE decided_on=?", (_today(),)
        ).fetchone()
        if row is not None and row["path"] != body.path:
            write_audit(
                conn,
                user,
                "释锁册改口被拒",
                _today(),
                {"from": row["path"], "to": body.path, "rule": "当天禁止改口"},
            )
            conn.commit()
            raise HTTPException(409, "当天禁止改口：偏离持有人释锁册")
        if row is None:
            conn.execute(
                """INSERT INTO holder_release_ledger(decided_on,path,note,created_at)
                   VALUES (?,?,?,?)""",
                (_today(), body.path, body.note or "", _now()),
            )
        write_audit(conn, user, "释锁册登记", _today(), {"path": body.path})
        conn.commit()
        return {"ok": True, "decided_on": _today(), "path": body.path}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok, "holder": user},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        status = release(conn, body.lock_key, user)
        conn.commit()
        if status == "deviated":
            raise HTTPException(409, "偏离持有人释锁册：拒写并记盗释偏了")
        if status == "missing":
            raise HTTPException(404, "锁不存在")
        if status == "denied":
            raise HTTPException(403, "盗释拒绝：非持有人不得释锁")
        return {"ok": True, "lock_key": body.lock_key, "released": True}
