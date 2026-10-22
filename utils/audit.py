import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from db import DB_PATH


def _genesis_hash() -> str:
    return "0" * 64


def append_audit(actor: str, action: str, target: str,
                 before: dict | None = None, after: dict | None = None,
                 request_id: str | None = None) -> str:
    """P2-18: 마지막 해시 조회 + INSERT 를 BEGIN IMMEDIATE 단일 트랜잭션으로 수행.

    별도 연결/트랜잭션이면 동시 append 시 prev_hash 체인이 갈라진다.
    IMMEDIATE 로 쓰기 잠금을 선점해 직렬화한다.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = row["entry_hash"] if row else _genesis_hash()
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "before": before,
            "after": after,
            "request_id": request_id,
            "prev_hash": prev,
        }
        entry_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        conn.execute(
            """INSERT INTO audit_log
               (ts, actor, action, target, before_json, after_json,
                request_id, prev_hash, entry_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["ts"], actor, action, target,
                json.dumps(before, default=str) if before else None,
                json.dumps(after, default=str) if after else None,
                request_id, prev, entry_hash,
            ),
        )
        conn.commit()
        return entry_hash
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_audit_chain() -> bool:
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    finally:
        conn.close()
    prev = _genesis_hash()
    for r in rows:
        if r["prev_hash"] != prev:
            return False
        payload = {
            "ts": r["ts"], "actor": r["actor"], "action": r["action"],
            "target": r["target"],
            "before": json.loads(r["before_json"]) if r["before_json"] else None,
            "after": json.loads(r["after_json"]) if r["after_json"] else None,
            "request_id": r["request_id"], "prev_hash": prev,
        }
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        if expected != r["entry_hash"]:
            return False
        prev = r["entry_hash"]
    return True
