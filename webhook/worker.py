"""웹훅 워커 — webhook_events.processed 큐를 소비해 실제 반영을 수행한다 (P2-19).

수신 API(receiver)는 3초 룰 준수를 위해 검증·중복탐지만 하고 즉시 200 응답한다.
실제 주문 수집/재고 반영/송장 처리는 이 워커가 백그라운드에서 소비한다.

사용법:
    python main.py webhook-worker            # 무한 루프 (--once 로 단발 실행 가능)
"""
import json
import logging

from db import get_conn

log = logging.getLogger(__name__)


def _mark(conn, event_id: int, ok: bool, error: str | None = None) -> None:
    conn.execute(
        """UPDATE webhook_events
           SET processed = ?, last_error = COALESCE(?, last_error)
           WHERE id = ?""",
        (1 if ok else -1, error, event_id),
    )


def handle_event(event: dict) -> dict:
    """topic별 라우팅. 반환값은 처리 요약(감사/로그용)."""
    provider = event["provider"]
    topic = (event["topic"] or "").lower()
    body = {}
    raw = event["raw_json"] if "raw_json" in event.keys() else None
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

    # --- 주문 계열: 신규/변경 → 채널 주문 재수집(trigger only) ---
    order_topics = (
        "orders/create", "orders/updated", "orders/paid", "orders/partially_fulfilled",
        "order.created", "order.updated", "order paid", "order newly created",
        "shop_order_created", "shop_order_paid",
    )
    if any(t in topic for t in ("order",)) and provider in ("shopify", "woocommerce", "cj"):
        from services import order_service
        if provider == "shopify":
            n = order_service.collect_shopify()
        elif provider == "woocommerce":
            n = order_service.collect_woo()
        else:  # cj logistics/order status events → 송장 확인
            n = _handle_cj_logistics(body)
        return {"action": "orders", "collected_or_updated": n}

    # --- 재고 계열: 공급사 재고 변경 → 채널 재고 동기화 ---
    if "stock" in topic or "inventory" in topic:
        from services.inventory_sync import sync_inventory
        result = sync_inventory()
        return {"action": "inventory", "result": result}

    # --- 상품 계열: 가격/설명 변경 → 매핑된 상품 갱신 ---
    if "product" in topic and any(k in topic for k in ("update", "create")):
        from services.product_sync import sync_changed_products
        return {"action": "products", "result": sync_changed_products(provider, body)}

    # --- 분쟁 계열 (추가 기능 §4): CJ dispute 상태 변경 기록 ---
    if "dispute" in topic or "claim" in topic:
        from services.dispute_service import record_dispute_event
        return {"action": "dispute", "result": record_dispute_event(provider, topic, body)}

    log.info("no handler for provider=%s topic=%s (recorded only)", provider, topic)
    return {"action": "ignored"}


def _handle_cj_logistics(body: dict) -> int:
    """CJ 물류/주문 상태 이벤트 → cj_orders.trackNumber 반영."""
    order_number = str(body.get("orderNumber") or "")
    tracking = body.get("trackNumber") or body.get("trackingNumber")
    if not order_number:
        return 0
    with get_conn() as conn:
        row = conn.execute(
            "SELECT local_order_id FROM cj_orders WHERE cj_order_number = ?",
            (order_number,),
        ).fetchone()
    if not row:
        return 0
    if tracking:
        from services.order_service import push_tracking
        push_tracking(row["local_order_id"])
    return 1


def consume_once(limit: int = 20) -> int:
    """미처리(processed=0) 이벤트 소비. 실패한 이벤트(processed=-1)는 재시도하지 않는다
    (수동 개입 원칙). 반환: 처리한 건수."""
    handled = 0
    with get_conn() as conn:
        pending = conn.execute(
            """SELECT e.* FROM webhook_events e
               WHERE e.processed = 0
               ORDER BY e.id LIMIT ?""",
            (limit,),
        ).fetchall()
    for ev in pending:
        try:
            outcome = handle_event(dict(ev))
            with get_conn() as conn:
                _mark(conn, ev["id"], True)
            log.info("webhook event %s handled: %s", ev["id"], outcome)
            handled += 1
        except Exception as e:
            log.exception("webhook event %s failed: %s", ev["id"], e)
            with get_conn() as conn:
                _mark(conn, ev["id"], False, str(e)[:500])
    return handled


def run_loop(interval_sec: int = 5, once: bool = False) -> None:
    import time
    while True:
        n = consume_once()
        if n:
            log.info("worker consumed %d events", n)
        if once:
            break
        time.sleep(interval_sec)
