import json
import logging
from clients import woo_client
from db import get_conn
from utils.audit import append_audit

log = logging.getLogger(__name__)


def sync_woo_reviews() -> int:
    n = 0
    for r in woo_client.list_reviews():
        with get_conn() as conn:
            # P0-4: REPLACE(DELETE+INSERT) 대신 UPSERT — comment_replies 연결 유지
            conn.execute("""
                INSERT INTO comments
                (channel, channel_comment_id, product_ref, author, body,
                 rating, status, parent_id, created_at, raw_json)
                VALUES ('woocommerce', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, channel_comment_id) DO UPDATE SET
                  product_ref = excluded.product_ref,
                  author      = excluded.author,
                  body        = excluded.body,
                  rating      = excluded.rating,
                  status      = excluded.status,
                  parent_id   = excluded.parent_id,
                  created_at  = excluded.created_at,
                  raw_json    = excluded.raw_json
            """, (
                str(r["id"]), str(r.get("product_id")), r.get("reviewer"),
                r.get("review", ""), int(r.get("rating") or 0),
                "approved" if r.get("status") == "approved" else "pending",
                str(r.get("parent", 0)) if r.get("parent") else None,
                r.get("date_created"), json.dumps(r, default=str),
            ))
        n += 1
    return n


def draft_reply(comment_id: int, body: str) -> None:
    with get_conn() as conn:
        c = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not c:
            raise ValueError("comment not found")
        # P0-4: REPLACE 가 sent 상태까지 덮어쓰지 않도록 UPSERT + sent 보호
        conn.execute("""
            INSERT INTO comment_replies
            (comment_id, channel, body, status)
            VALUES (?, ?, ?, 'draft')
            ON CONFLICT(comment_id) DO UPDATE SET
              channel = excluded.channel,
              body    = excluded.body,
              status  = CASE WHEN comment_replies.status = 'sent'
                             THEN comment_replies.status ELSE 'draft' END
        """, (comment_id, c["channel"], body))


def send_reply(comment_id: int) -> dict:
    with get_conn() as conn:
        c = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        r = conn.execute(
            "SELECT * FROM comment_replies WHERE comment_id = ?", (comment_id,)
        ).fetchone()
    if not c or not r:
        raise ValueError("missing comment or draft")
    if r["status"] == "sent":
        return {"skipped": "already_sent"}

    if c["channel"] != "woocommerce":
        raise NotImplementedError("Shopify 리뷰는 앱 어댑터 필요")

    resp = woo_client.reply_to_review(int(c["channel_comment_id"]), r["body"])
    with get_conn() as conn:
        conn.execute(
            "UPDATE comment_replies SET channel_reply_id = ?, status = 'sent' "
            "WHERE comment_id = ?",
            (str(resp["id"]), comment_id),
        )
    append_audit("system", "comment.reply.send", f"comment:{comment_id}",
                 after={"reply_id": resp["id"]})
    return {"reply_id": resp["id"]}
