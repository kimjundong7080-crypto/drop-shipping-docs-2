import hashlib
import json
import logging
import sqlite3

from fastapi import FastAPI, Request, HTTPException

from db import get_conn
from utils.security import verify_cj, verify_shopify, verify_woo

log = logging.getLogger(__name__)
app = FastAPI(title="Dropship Webhook Receiver", docs_url=None, redoc_url=None)


def _record(provider: str, delivery_id: str, topic: str | None,
            raw_body: bytes | None = None) -> bool:
    """중복 배송 차단. 새 이벤트면 True.

    P2-19: IntegrityError(UNIQUE 위반)만 중복으로 간주한다.
    DB 잠김/디스크 오류 등은 예외를 그대로 올려 500 → 발송 측 재전송을 유도한다
    (모든 예외를 duplicate 로 처리하면 이벤트가 유실된다 — fail-closed 위반).

    원본 바디(raw_json)는 워커가 파싱 없이 소비할 수 있도록 함께 저장한다.
    """
    raw_text = None
    if raw_body is not None:
        try:
            raw_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            raw_text = None
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO webhook_events (provider, delivery_id, topic, raw_json)
                   VALUES (?, ?, ?, ?)""",
                (provider, delivery_id, topic, raw_text),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def _dispatch(provider: str, topic: str | None, body: dict) -> None:
    """P2-19/WebhookRouter: 수신 즉시 200 응답 후 백그라운드 처리 위임.

    여기서는 라우팅 결정과 로그만 수행하며, 실제 반영은 워커가 processed 플래그를
    보고 큐를 소비한다(webhook_events.processed). stock → 재고 동기화 큐,
    order/logistics → 주문 상태 업데이트, dispute → 사후 처리 워크플로.
    """
    log.info("webhook dispatch provider=%s topic=%s", provider, topic)


@app.post("/webhooks/cj")
async def cj_webhook(request: Request):
    raw = await request.body()
    sign = request.headers.get("sign", "")
    if not verify_cj(raw, sign):
        raise HTTPException(401, "invalid signature")  # fail-closed
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid json")

    delivery_id = hashlib.sha256(raw).hexdigest()
    topic = body.get("type") or body.get("topic")
    if not _record("cj", delivery_id, topic, raw):
        return {"ok": True, "duplicate": True}

    _dispatch("cj", topic, body)
    return {"ok": True}


@app.post("/webhooks/shopify")
async def shopify_webhook(request: Request):
    raw = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-SHA256", "")
    if not verify_shopify(raw, hmac_header):
        raise HTTPException(401, "invalid signature")

    delivery_id = request.headers.get("X-Shopify-Webhook-Id", "")
    if not delivery_id:
        delivery_id = hashlib.sha256(raw).hexdigest()
    topic = request.headers.get("X-Shopify-Topic")
    if not _record("shopify", delivery_id, topic, raw):
        return {"ok": True, "duplicate": True}

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    _dispatch("shopify", topic, body)
    return {"ok": True}


@app.post("/webhooks/woo")
async def woo_webhook(request: Request):
    """P2-19: WooCommerce 웹훅 엔드포인트 추가.

    WooCommerce 는 설정 시 지정한 secret 으로 원본 바디의 HMAC-SHA256 을
    Base64 인코딩해 X-WC-Webhook-Signature 헤더에 담는다. 파싱 전에 검증한다.
    """
    from config import cfg
    raw = await request.body()
    sign = request.headers.get("X-WC-Webhook-Signature", "")
    if not cfg.woo_webhook_secret:
        raise HTTPException(503, "WOO_WEBHOOK_SECRET not configured")
    if not verify_woo(raw, sign, cfg.woo_webhook_secret):
        raise HTTPException(401, "invalid signature")

    delivery_id = request.headers.get("X-WC-Webhook-Delivery-ID", "")
    if not delivery_id:
        delivery_id = hashlib.sha256(raw).hexdigest()
    topic = request.headers.get("X-WC-Webhook-Topic")
    if not _record("woocommerce", delivery_id, topic, raw):
        return {"ok": True, "duplicate": True}

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    _dispatch("woocommerce", topic, body)
    return {"ok": True}
