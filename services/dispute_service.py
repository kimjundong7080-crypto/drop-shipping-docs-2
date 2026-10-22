"""DisputeService — 분쟁·반품·환불 파이프라인 (추가 기능 §5, P1).

CJ 분쟁 절차 3단계:
  ① 분쟁 가능 상품 목록 조회  → GET /dispute/getOrderProductsList
  ② 분쟁 확인(사유·금액)      → 로컬 검증(RefundGuard 한도)
  ③ 분쟁 생성                → POST /dispute/applyForRefundOrResending

안전장치(보완사항 §6·§10):
- cj_dispute_id UNIQUE + 결정적 로컬 키로 이중 분쟁 생성 방지(멱등 쓰기).
- 생성 결과는 disputes 테이블에 기록하고 감사 로그에 남긴다.
- 상위 완료가 불확실한 쓰기는 재시도하지 않는다(UNCERTAIN 원칙 동일 적용).
"""
import json
import logging
from decimal import Decimal

from clients.cj_client import CJClient
from db import get_conn
from utils.audit import append_audit

log = logging.getLogger(__name__)


def _limits() -> tuple[Decimal, Decimal]:
    from config import cfg
    per_order = Decimal(getattr(cfg, "dispute_max_refund_per_order", "500"))
    return per_order, per_order  # 일별 한도는 TODO(운영 데이터 축적 후 분리)


def list_disputable(local_order_id: int) -> list[dict]:
    """① CJ 주문의 분쟁 가능 상품 목록."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cj_order_id FROM cj_orders WHERE local_order_id = ?",
            (local_order_id,),
        ).fetchone()
    if not row or not row["cj_order_id"]:
        raise ValueError("no created cj order for this local order")
    cj = CJClient()
    data = cj.get("/dispute/getOrderProductsList",
                  {"orderId": row["cj_order_id"], "pageNum": 1, "pageSize": 50})
    return (data or {}).get("list") or []


def create_dispute(local_order_id: int, cj_line_id: str, reason: str,
                   amount: str | Decimal, refund: bool = True,
                   confirm: bool = False) -> dict:
    """②+③ 분쟁 생성. confirm=False 이면 dry-run(검증만).

    RefundGuard: 주문별 환불 한도를 초과하면 차단(fail-closed).
    멱등: 동일 (local_order_id, cj_line_id) 에 대해 이미 open/generated 분쟁이
    있으면 재생성하지 않는다.
    """
    amt = Decimal(str(amount))
    if amt <= 0:
        raise ValueError("dispute amount must be positive")
    per_order, _ = _limits()
    if amt > per_order:
        raise PermissionError(
            f"refund {amt} exceeds per-order guard {per_order} — manual approval required")

    with get_conn() as conn:
        cj_row = conn.execute(
            "SELECT cj_order_id FROM cj_orders WHERE local_order_id = ?",
            (local_order_id,),
        ).fetchone()
    if not cj_row or not cj_row["cj_order_id"]:
        raise ValueError("no created cj order")

    # cj_line_id 기준 멱등 검사 (raw_json 안에 라인 ID 가 포함된 레코드)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, cj_dispute_id, raw_json FROM disputes
               WHERE local_order_id = ? AND status IN ('open','generated')""",
            (local_order_id,),
        ).fetchall()
    for r in rows:
        if cj_line_id and cj_line_id in (r["raw_json"] or ""):
            return {"skipped": "already_exists", "dispute_id": r["cj_dispute_id"],
                    "local_pk": r["id"]}

    payload = {
        "orderId": cj_row["cj_order_id"],
        "orderProductLineId": cj_line_id,
        "reason": reason,
        "amount": str(amt),
        "applyType": "REFUND" if refund else "RESENDING",
    }
    if not confirm:
        return {"dry_run": True, "payload": payload}

    cj = CJClient()
    result = cj.request("POST", "/dispute/applyForRefundOrResending",
                        json_body=payload)
    data = result.get("data")
    dispute_id = str(data) if isinstance(data, (int, str)) else None

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO disputes
               (cj_order_id, local_order_id, cj_dispute_id, reason, amount,
                status, raw_json)
               VALUES (?, ?, ?, ?, ?, 'generated', ?)""",
            (cj_row["cj_order_id"], local_order_id, dispute_id, reason,
             str(amt), json.dumps({"payload": payload, "response": result},
                                  default=str)),
        )
        pk = cur.lastrowid
    append_audit("system", "cj.dispute.create", f"order:{local_order_id}",
                 after={"dispute_pk": pk, "cj_dispute_id": dispute_id,
                        "amount": str(amt)})
    return {"dispute_pk": pk, "cj_dispute_id": dispute_id, "status": "generated"}


# CJ 분쟁 종결 상태 (추가 기능 §4): 환불 환입을 대변에 기록하는 상태
CLOSED_REFUND_STATUSES = ("REFUNDED", "SUCCESS", "COMPLETE", "COMPLETED")


def update_dispute_status(dispute_pk: int, status: str) -> None:
    """dispute 웹훅/폴링 결과 반영 (환불/재배송/거절).

    RUN-REPORT P2-1: 분쟁이 환불로 종결되면 cost_ledger 에 'dispute_credit'
    대변 항목을 자동 기록한다. 중복 기록 방지 — disputes 행에 환입 반영 여부를
    raw_json 내 플래그로 표시하며, 같은 상태가 다시 전달돼도 1회만 기록한다.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM disputes WHERE id = ?",
                           (dispute_pk,)).fetchone()
        if row is None:
            raise ValueError(f"unknown dispute id {dispute_pk}")
        conn.execute(
            "UPDATE disputes SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, dispute_pk),
        )
        up = (status or "").upper()
        already_credited = '"credited": true' in (row["raw_json"] or "")
        if (up in CLOSED_REFUND_STATUSES and not already_credited
                and row["amount"] and Decimal(str(row["amount"])) > 0):
            # P2-1: 같은 get_conn 트랜잭션 안에서 대변 기록 — 중첩 연결로
            # SQLite write-lock 을 자기 자신과 경합시키는 것 방지.
            from services.cost_ledger import record_in_conn
            record_in_conn(conn, row["local_order_id"], "dispute_credit",
                           row["amount"], note=f"dispute:{dispute_pk} {up}")
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            raw["credited"] = True
            conn.execute("UPDATE disputes SET raw_json = ? WHERE id = ?",
                         (json.dumps(raw, default=str), dispute_pk))


def record_dispute_event(provider: str, topic: str, body: dict) -> dict:
    """웹훅 워커용 (추가 기능 §4): CJ/채널 분쟁 상태 변경 이벤트 반영.

    body 에서 dispute 번호(orderClaimNo 등)와 상태를 매칭해 disputes 테이블을
    갱신한다. 매칭 실패 시 기록만 남긴다(수동 개입 원칙 — 임의 매칭 금지).
    """
    claim_no = str(body.get("orderClaimNo") or body.get("disputeId")
                   or body.get("claimNo") or "")
    status = (body.get("status") or body.get("claimStatus") or "").upper()
    if not claim_no or not status:
        return {"skipped": "no claim number/status in event"}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM disputes WHERE cj_dispute_id = ? ORDER BY id DESC LIMIT 1",
            (claim_no,),
        ).fetchone()
        if not row:
            log.info("dispute event for unknown ref %s (topic=%s)", claim_no, topic)
            return {"skipped": f"no local dispute for {claim_no}"}
        update_dispute_status(row["id"], status)
    append_audit("system", "dispute.webhook", f"dispute:{row['id']}",
                 after={"provider": provider, "topic": topic,
                        "provider_ref": claim_no, "status": status})
    return {"dispute_id": row["id"], "status": status}
