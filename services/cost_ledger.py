"""CostLedger — 주문별 원가 장부 (추가 기능 §6, P1).

orders.total / order_items.unit_price 만으로는 "이 주문이 얼마를 남겼는가"를
계산할 수 없다. 이 모듈은 공급가 + 운임 + 채널 수수료 + 환불/분쟁 금액을
cost_ledger 테이블에 기록(이중 기장 개념의 차변/대변 엔트리)하고,
SKU·채널 단위 마진 리포트를 제공한다.

entry_type: supplier_cost | freight | channel_fee | refund | dispute_credit
"""
import logging
from decimal import Decimal

from db import get_conn

log = logging.getLogger(__name__)

DEBIT_TYPES = ("supplier_cost", "freight", "channel_fee")   # 비용(차변)
CREDIT_TYPES = ("refund", "dispute_credit")                 # 환입(대변)


def record_in_conn(conn, order_id: int, entry_type: str,
                   amount: str | Decimal, currency: str | None = None,
                   note: str | None = None) -> int:
    """호출 측이 열어둔 커넥션에 기록 — 중첩 get_conn 의 SQLite write-lock
    자기경합(database is locked)을 피한다 (RUN-REPORT P2-1 검증 중 발견)."""
    if entry_type not in DEBIT_TYPES + CREDIT_TYPES:
        raise ValueError(f"unknown entry_type: {entry_type}")
    amt = Decimal(str(amount))
    cur = conn.execute(
        """INSERT INTO cost_ledger (order_id, entry_type, amount, currency, note)
           VALUES (?, ?, ?, ?, ?)""",
        (order_id, entry_type, str(amt), currency, note),
    )
    return cur.lastrowid


def record(order_id: int, entry_type: str, amount: str | Decimal,
           currency: str | None = None, note: str | None = None) -> int:
    """원가/환불 엔트리 기록.멱등성은 보장하지 않으므로 호출 측에서 1회성 이벤트를 확인."""
    with get_conn() as conn:
        return record_in_conn(conn, order_id, entry_type, amount, currency, note)


def order_profit(order_id: int) -> dict:
    """주문별 수익: 매출(orders.total) - Σ비용 + Σ환입."""
    with get_conn() as conn:
        order = conn.execute(
            "SELECT total, currency FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT entry_type, amount FROM cost_ledger WHERE order_id = ?",
            (order_id,),
        ).fetchall()
    if not order:
        raise ValueError("order not found")

    revenue = Decimal(str(order["total"] or 0))
    costs = sum((Decimal(r["amount"]) for r in rows
                 if r["entry_type"] in DEBIT_TYPES), Decimal("0"))
    credits = sum((Decimal(r["amount"]) for r in rows
                   if r["entry_type"] in CREDIT_TYPES), Decimal("0"))
    profit = revenue - costs + credits
    margin = (profit / revenue) if revenue else None
    return {
        "order_id": order_id,
        "revenue": str(revenue),
        "costs": str(costs),
        "credits": str(credits),
        "profit": str(profit),
        "margin": str(margin) if margin is not None else None,
        "currency": order["currency"],
    }


def profit_report(limit: int = 200) -> list[dict]:
    """최근 주문들의 수익 요약 (리포트/CSV 내보내기용)."""
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
    out = []
    for oid in ids:
        try:
            out.append(order_profit(oid))
        except ValueError:
            continue
    return out


def record_from_cj_order(order_id: int, cj_detail: dict,
                         currency: str | None = None) -> None:
    """CJ getOrderDetail 응답에서 실제 공급가/운임을 뽑아 원가를 기록한다.

    API10 대응(보완사항 §9): 필드 존재·범위만 검사하고, 없는 필드는 기록 생략.
    """
    if not isinstance(cj_detail, dict):
        return
    mapping = [
        ("supplier_cost", cj_detail.get("originalAmount")),
        ("freight", cj_detail.get("freightAmount") or cj_detail.get("shippingFee")),
    ]
    for entry_type, raw in mapping:
        if raw in (None, ""):
            continue
        try:
            amt = Decimal(str(raw))
        except Exception:
            log.warning("cost ledger: bad amount %r for %s", raw, entry_type)
            continue
        if amt < 0 or amt > Decimal("1000000"):
            log.warning("cost ledger: out-of-range amount %s (%s)", amt, entry_type)
            continue
        record(order_id, entry_type, amt, currency=currency,
               note="cj getOrderDetail")
