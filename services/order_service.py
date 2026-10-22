import json
import logging

from decimal import Decimal

import requests

from clients.cj_client import CJClient, CJAPIError
from clients import woo_client, shopify_client
from config import cfg
from db import get_conn
from utils.audit import append_audit

log = logging.getLogger(__name__)

_shopify_client = None


def _shopify():
    """P2-23: 지연 생성 — 모듈 import 시 자격증명 불요."""
    global _shopify_client
    if _shopify_client is None:
        _shopify_client = shopify_client.ShopifyClient()
    return _shopify_client


# ---------------------------------------------------------------- 수집

def collect_woo() -> int:
    """P1-12: 전체 페이지네이션 + 상태 목록 설정화 + date_paid 기반 결제 상태."""
    n = 0
    orders = woo_client.iter_all_orders(list(woo_cfg_statuses()))
    for o in orders:
        paid = bool(o.get("date_paid"))
        financial_status = "paid" if paid else "unpaid"
        _upsert_order(
            channel="woocommerce",
            channel_order_id=str(o["id"]),
            channel_order_num=o.get("number"),
            status=o.get("status"),
            financial_status=financial_status,
            total=o.get("total"),
            currency=o.get("currency"),
            email=(o.get("billing") or {}).get("email"),
            shipping_address=o.get("shipping") or {},
            raw=o,
            items=[{
                "channel_line_id": str(li["id"]),
                "channel_item_name": li.get("name"),
                "supplier_sku": li.get("sku"),
                "quantity": li["quantity"],
                "unit_price": li.get("price"),
            } for li in o.get("line_items", [])],
        )
        n += 1
    return n


def woo_cfg_statuses() -> tuple:
    from config import cfg
    return cfg.woo_order_statuses


def collect_shopify() -> int:
    n = 0
    client = _shopify()
    for o in shopify_client.collect_orders(client):
        addr = o.get("shippingAddress") or {}
        _upsert_order(
            channel="shopify",
            channel_order_id=o["id"],
            channel_order_num=o.get("name"),
            status="open",
            financial_status=o.get("displayFinancialStatus"),
            total=o["totalPriceSet"]["shopMoney"]["amount"],
            currency=o["totalPriceSet"]["shopMoney"]["currencyCode"],
            email=o.get("email"),
            shipping_address=addr,
            raw=o,
            items=[{
                "channel_line_id": e["node"]["id"],
                "channel_item_name": e["node"].get("name"),
                "supplier_sku": e["node"].get("sku"),
                "quantity": e["node"]["quantity"],
                "unit_price": e["node"]["originalUnitPriceSet"]["shopMoney"]["amount"],
            } for e in o["lineItems"]["edges"]],
        )
        n += 1
    return n


# ---------------------------------------------------------------- 저장

def _upsert_order(channel, channel_order_id, channel_order_num, status,
                  financial_status, total, currency, email, shipping_address,
                  raw, items):
    """P0-4: INSERT OR REPLACE 대신 ON CONFLICT DO UPDATE — orders.id 안정성 확보."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO orders
            (channel, channel_order_id, channel_order_num, status,
             financial_status, total, currency, customer_email,
             shipping_address, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, channel_order_id) DO UPDATE SET
              channel_order_num = excluded.channel_order_num,
              status            = excluded.status,
              financial_status  = excluded.financial_status,
              total             = excluded.total,
              currency          = excluded.currency,
              customer_email    = excluded.customer_email,
              shipping_address  = excluded.shipping_address,
              raw_json          = excluded.raw_json,
              fetched_at        = CURRENT_TIMESTAMP
        """, (
            channel, channel_order_id, channel_order_num, status,
            financial_status, total, currency, email,
            json.dumps(shipping_address, default=str), json.dumps(raw, default=str),
        ))
        order_row = conn.execute(
            "SELECT id FROM orders WHERE channel = ? AND channel_order_id = ?",
            (channel, channel_order_id),
        ).fetchone()
        oid = order_row["id"]

        conn.execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
        for it in items:
            mapped = 1 if _resolve_mapping(it["supplier_sku"]) else 0
            conn.execute("""
                INSERT INTO order_items
                (order_id, channel_line_id, channel_item_name, supplier_sku,
                 quantity, unit_price, mapped)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (oid, it["channel_line_id"], it.get("channel_item_name"),
                  it["supplier_sku"], it["quantity"], it["unit_price"], mapped))


def _resolve_mapping(supplier_sku: str | None) -> dict | None:
    if not supplier_sku:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM mapping WHERE supplier_sku = ? AND verified = 1
               ORDER BY priority LIMIT 1""",
            (supplier_sku,),
        ).fetchone()
    return dict(row) if row else None


def preview_order(order_id: int) -> dict:
    with get_conn() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        items = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
        cj = conn.execute(
            "SELECT * FROM cj_orders WHERE local_order_id = ?", (order_id,)
        ).fetchone()
    if not order:
        raise ValueError("order not found")
    return {
        "order": dict(order),
        "items": [dict(i) for i in items],
        "cj_order": dict(cj) if cj else None,
    }


# ---------------------------------------------------------------- 페이로드

def _country_code(addr: dict, channel: str) -> str:
    """P0-3: 채널별 국가 코드 추출.
    Shopify: country 는 전체 이름("United States")이므로 countryCodeV2(ISO)를 사용.
    WooCommerce: country 가 이미 ISO 코드.
    """
    if channel == "shopify":
        code = addr.get("countryCodeV2") or ""
        if not code:
            raise ValueError(
                "shopify address missing countryCodeV2 — cannot build CJ payload")
        return code.upper()[:2]
    code = addr.get("country") or ""
    if len(code) > 2:  # 구 데이터/이상치 방어: 전체 이름이면 거부
        raise ValueError(f"unexpected non-ISO country value: {code!r}")
    return code.upper()


def _build_cj_payload(order_row: dict, items: list[dict], is_sandbox: int) -> dict:
    products = []
    for it in items:
        mapping = _resolve_mapping(it["supplier_sku"])
        if not mapping:
            raise ValueError(f"unmapped supplier_sku: {it['supplier_sku']}")
        with get_conn() as conn:
            sp = conn.execute(
                """SELECT supplier_vid FROM supplier_products
                   WHERE supplier = 'cj' AND supplier_sku = ?""",
                (it["supplier_sku"],),
            ).fetchone()
        if not sp or not sp["supplier_vid"]:
            raise ValueError(f"missing vid for {it['supplier_sku']}")
        products.append({
            "vid": sp["supplier_vid"],
            "quantity": str(it["quantity"]),
            # NOTE(CJ 스펙 재확인 필요): shippingName 은 물류 옵션명 필드로,
            # SKU 대입은 임의 사용이다. createOrder 응답 검증 전까지 유지하되
            # 공식 문서 확인 후 제거 예정.
            "shippingName": it["supplier_sku"],
        })

    addr = json.loads(order_row["shipping_address"]) if order_row["shipping_address"] else {}
    channel = order_row["channel"]
    return {
        # P0-2: 결정적 주문번호(멱등키). 재시도해도 동일 → CJ 중복 주문 방지.
        "orderNumber": _cj_order_number(order_row["id"]),
        "shippingCountryCode": _country_code(addr, channel),
        "shippingCountry": addr.get("country", ""),
        "shippingProvince": addr.get("state", addr.get("province", "")),
        "shippingCity": addr.get("city", ""),
        "shippingAddress": addr.get("address_1", addr.get("address1", "")),
        "shippingAddress2": addr.get("address_2", addr.get("address2", "")),
        "shippingCustomerName": f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip()
                               or addr.get("name", ""),
        "shippingZip": addr.get("postcode", addr.get("zip", "")),
        "shippingPhone": addr.get("phone", ""),
        "logisticName": order_row.get("preferred_logistic") or "CJPacket Ordinary",
        "fromCountryCode": "CN",
        "remark": f"{channel} / {order_row['channel_order_num']}",
        "isSandbox": is_sandbox,  # NOTE: 실 API에서 유효한 필드인지 확인 필요(샌드박스는 별도 base URL)
        "products": products,
    }


def _cj_order_number(local_order_id: int) -> str:
    """DB에 저장된 주문번호가 있으면 재사용, 없으면 결정적으로 생성."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cj_order_number FROM cj_orders WHERE local_order_id = ?",
            (local_order_id,),
        ).fetchone()
        if row and row["cj_order_number"] and not row["cj_order_number"].startswith("PENDING-"):
            return row["cj_order_number"]
        number = f"LOCAL-{local_order_id}"
        conn.execute(
            """UPDATE cj_orders SET cj_order_number = ?, updated_at = CURRENT_TIMESTAMP
               WHERE local_order_id = ? AND cj_order_number LIKE 'PENDING-%'""",
            (number, local_order_id),
        )
    return number


# ---------------------------------------------------------------- 발주

def _extract_cj_order_id(result: dict) -> str:
    """P0-1: data(dict) 안에서 실제 주문 ID 필드를 명시적으로 추출."""
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("orderId", "orderNumber", "id"):
            val = data.get(key)
            if val not in (None, ""):
                return str(val)
        raise ValueError(f"CJ response has no order id field: {result}")
    if isinstance(data, (int, str)) and str(data).strip():
        return str(data)
    raise ValueError(f"CJ createOrder returned unexpected data: {result}")


def _cj_result_ok(result: dict) -> bool:
    """CJ API 공통 응답(result/code/message) 검증 — result=false 는 실패로 본다.

    requests.raise_for_status 는 HTTP 200 + body {"result": false} 를 잡지 못한다.
    """
    if not isinstance(result, dict):
        return False
    if result.get("result") is False:
        return False
    code = result.get("code")
    if code not in (None, "", 200, "200", 0, "0"):
        return False
    return True


class CJUncertainWriteError(RuntimeError):
    """CJ createOrder 응답이 불확실(타임아웃/5xx/파싱 실패)한 상태.

    보완사항 §6·§10 / 추가 기능 §11: 상위 완료가 불확실한 쓰기는 재시도 불가(non-retryable).
    자동 재시도 대신 getOrderDetail 로 현재 상태를 조사해야 한다.
    """


def _record_uncertain(order_id: int, cj_number: str, err: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE cj_orders SET status = 'UNCERTAIN', updated_at = CURRENT_TIMESTAMP
               WHERE local_order_id = ?""",
            (order_id,),
        )
    append_audit("system", "cj.order.uncertain", f"order:{order_id}",
                 after={"cj_order_number": cj_number, "error": err[:300]})


def _lookup_created(cj: "CJClient", order_number: str) -> dict | None:
    """멱등 조회: LOCAL-{id} 주문번호로 CJ 측 생성 여부를 확인한다 (재시도 대신 조사)."""
    try:
        detail = cj.get("/shopping/order/getOrderList",
                        {"orderNumber": order_number, "pageNum": 1, "pageSize": 5})
    except Exception as e:  # 조회 자체가 실패하면 여전히 불확실
        log.warning("cj order lookup failed: %s", e)
        return None
    items = (detail or {}).get("list") or []
    for it in items:
        if str(it.get("orderNumber") or "") == order_number:
            oid = it.get("orderId") or it.get("id")
            if oid:
                return {"cj_order_id": str(oid),
                        "status": it.get("orderStatus") or "CREATED"}
    return None


def sandbox_preview(order_id: int) -> dict:
    """P0-5: 드라이런 — 페이로드 조립과 마진 가드만 검증하고 API 호출은 하지 않는다.
    실제 CJ 샌드박스 호출이 필요하면 --confirm-cj-sandbox 로 수행한다."""
    p = preview_order(order_id)
    payload = _build_cj_payload(p["order"], p["items"], is_sandbox=1)
    _assert_margin_ok(p, payload)
    return {"dry_run": True, "payload": payload}


def _assert_margin_ok(p: dict, payload: dict) -> None:
    """마이너스 마진 가드 (PriceGuard, 추가 기능 §1): 판매가 합 ≤ 공급원가 합 + 예상 운임
    이면 발주를 차단한다. 운영 중 실수 주문을 막는 fail-closed 게이트.
    """
    sell_total = Decimal(str(p["order"]["total"] or 0))
    cost_total = Decimal("0")
    for it in p["items"]:
        cost_total += Decimal(str(it["unit_price"] or 0)) * Decimal(str(it["quantity"]))
    freight = Decimal(str(payload.get("estimatedFreight") or cfg.pricing_default_freight or 0))
    if sell_total > 0 and sell_total <= cost_total + freight:
        raise ValueError(
            f"price guard: negative/zero margin — sell={sell_total} "
            f"cost+freight={cost_total + freight}. Order {p['order']['id']} blocked.")
    if sell_total == 0:
        log.warning("margin guard: order total is 0 — verify before placing")


def place_order(order_id: int, confirm: bool = False,
                sandbox: bool = False) -> dict:
    """P0-2/P0-5: --confirm 없이는 실주문 금지. 실패 시 FAILED 기록 후 재시도 허용.

    운영 안전(보완사항 §6·§10): 응답이 불확실한 쓰기(타임아웃/5xx/파싱 실패)는
    UNCERTAIN 으로 기록하고 자동 재시도하지 않는다. 대신 결정적 주문번호
    (LOCAL-{order_id})로 CJ 주문 목록을 조회해 생성 여부를 확인한다.
    """
    if not confirm and not sandbox:
        raise PermissionError(
            "place_order requires confirm=True (or sandbox=True) to hit the live API")

    # 1) PENDING 선기록 (결정적 주문번호로 멱등 보장)
    order_number = f"LOCAL-{order_id}"
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM cj_orders WHERE local_order_id = ?", (order_id,)
        ).fetchone()
        if existing and existing["status"] in ("CREATED", "SHIPPED"):
            return {"skipped": "already_exists", "status": existing["status"],
                    "cj_order_id": existing["cj_order_id"]}
        if existing and existing["status"] == "UNCERTAIN":
            # 재시도 금지 — 조회로 확인 후 CREATED 로 승격하거나 수동 개입 필요
            found = _lookup_created(CJClient(), existing["cj_order_number"])
            if found:
                with get_conn() as conn:
                    conn.execute(
                        """UPDATE cj_orders SET cj_order_id = ?, status = 'CREATED',
                           updated_at = CURRENT_TIMESTAMP WHERE local_order_id = ?""",
                        (found["cj_order_id"], order_id),
                    )
                append_audit("system", "cj.order.reconciled", f"order:{order_id}",
                             after={"cj_order_id": found["cj_order_id"]})
                return {"reconciled": True, **found}
            raise RuntimeError(
                f"cj order {existing['cj_order_number']} is UNCERTAIN and not found "
                "on CJ yet — do NOT retry blindly; check getOrderList manually")
        if existing and existing["status"] in ("PENDING", "FAILED"):
            # 재시도: 같은 레코드 재사용 (주문번호는 LOCAL-{id} 로 고정)
            pass
        else:
            conn.execute(
                """INSERT INTO cj_orders (local_order_id, cj_order_number, status, is_sandbox)
                   VALUES (?, ?, 'PENDING', ?)""",
                (order_id, order_number, 1 if sandbox else 0),
            )

    p = preview_order(order_id)
    payload = _build_cj_payload(p["order"], p["items"], is_sandbox=1 if sandbox else 0)
    _assert_margin_ok(p, payload)

    cj = CJClient()
    try:
        result = cj.create_order(payload, sandbox=sandbox)
        if not _cj_result_ok(result):
            # 응답 본문은 받았으나 result=false → 주문 미생성(확정 실패)으로 판단
            raise RuntimeError(f"CJ createOrder rejected: {result}")
        cj_order_id = _extract_cj_order_id(result)
    except (requests.Timeout, requests.ConnectionError) as e:
        # 타임아웃/연결절벽 = 상위 완료가 불확실 → 재시도 불가 상태.
        # UNCERTAIN 으로 기록하고 조회(_lookup_created)로 확인해야 한다 (보완사항 §10).
        _record_uncertain(order_id, order_number, str(e))
        raise CJUncertainWriteError(
            f"CJ createOrder outcome unknown ({e}); order {order_number} marked "
            "UNCERTAIN — reconcile via getOrderList, do not auto-retry") from e
    except CJAPIError as e:
        # RUN-REPORT P2-3: 4xx 거부/result=false 는 '주문 미생성' 확정 실패.
        # HTTPError 와 달리 이 경로는 반드시 FAILED 로 기록해 재시도를 허용한다.
        with get_conn() as conn:
            conn.execute(
                """UPDATE cj_orders SET status = 'FAILED', updated_at = CURRENT_TIMESTAMP
                   WHERE local_order_id = ?""",
                (order_id,),
            )
        append_audit("system", "cj.order.failed", f"order:{order_id}",
                     after={"error": str(e)[:300]})
        raise
    except (ValueError, KeyError) as e:
        # 응답은 받았으나 파싱 실패 → 생성 여부 불확실
        _record_uncertain(order_id, order_number, str(e))
        raise CJUncertainWriteError(
            f"CJ createOrder response unparsable ({e}); order {order_number} "
            "marked UNCERTAIN — reconcile manually, do not auto-retry") from e
    except Exception as e:
        # 그 외 명시적 거부는 확정 실패 → FAILED 기록 후 재시도 허용
        with get_conn() as conn:
            conn.execute(
                """UPDATE cj_orders SET status = 'FAILED', updated_at = CURRENT_TIMESTAMP
                   WHERE local_order_id = ?""",
                (order_id,),
            )
        append_audit("system", "cj.order.failed", f"order:{order_id}",
                     after={"error": str(e)[:300]})
        raise

    with get_conn() as conn:
        conn.execute(
            """UPDATE cj_orders
               SET cj_order_id = ?, cj_order_number = ?, status = 'CREATED',
                   updated_at = CURRENT_TIMESTAMP
               WHERE local_order_id = ?""",
            (cj_order_id, payload["orderNumber"], order_id),
        )

    append_audit("system", "cj.order.create", f"order:{order_id}",
                 after={"cj_order_id": cj_order_id, "sandbox": sandbox})

    # 원가 장부 (추가 기능 §1/§6): 실주문 성공 시 공급가·운임을 차변에 1회 기록.
    # 실패해도 주문 발주 자체를 되돌릴 수 없으므로 best-effort + 명시 로그.
    if not sandbox:
        try:
            from services.cost_ledger import record as ledger_record
            for it in payload.get("items", []):
                ledger_record(order_id, "supplier_cost",
                              it.get("declaredValue") or it.get("cost", "0"),
                              note=f"cj:{cj_order_id} sku:{it.get('sku')}")
            freight = payload.get("shippingFee")
            if freight not in (None, "", "0"):
                ledger_record(order_id, "freight", freight,
                              note=f"cj:{cj_order_id}")
        except Exception as e:  # 장부 오류로 발주를 실패시키지 않는다
            log.warning("cost_ledger record failed order=%s err=%s",
                        order_id, str(e)[:200])

    if not sandbox and p["order"]["channel"] == "woocommerce":
        woo_client.push_order_note(int(p["order"]["channel_order_id"]),
                                   f"CJ order {cj_order_id} created")
        woo_client.update_order_status(int(p["order"]["channel_order_id"]), "processing")

    return {"cj_order_id": cj_order_id, "status": "CREATED", "sandbox": sandbox}


# ---------------------------------------------------------------- 송장

def push_tracking(order_id: int) -> dict:
    """P1-11: 송장번호를 실제로 확보한 경우에만 SHIPPED 처리."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cj_orders WHERE local_order_id = ?", (order_id,)
        ).fetchone()
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    if not row or not row["cj_order_id"]:
        raise ValueError("no cj order")
    if not order:
        raise ValueError("order not found")

    cj = CJClient()
    detail = cj.query_order(row["cj_order_id"])
    tracking = detail.get("trackNumber") or detail.get("trackingNumber")
    carrier = detail.get("logisticName") or row["logistic_name"] or "CJ"

    if not tracking:
        # 송장 없음 → 상태 변경 없이 대기 표시
        with get_conn() as conn:
            conn.execute(
                """UPDATE cj_orders SET logistic_name = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE local_order_id = ?""",
                (carrier, order_id),
            )
        return {"status": "no_tracking"}

    with get_conn() as conn:
        conn.execute(
            """UPDATE cj_orders SET tracking_number = ?, logistic_name = ?,
               status = 'SHIPPED', updated_at = CURRENT_TIMESTAMP
               WHERE local_order_id = ?""",
            (tracking, carrier, order_id),
        )

    if order["channel"] == "shopify":
        result = shopify_client.fulfill_order(
            _shopify(), order["channel_order_id"], tracking,
            f"https://t.17track.net/en#nums={tracking}", carrier,
        )
        if result.get("skipped"):
            log.info("shopify order %s already fulfilled", order["channel_order_id"])
    else:
        woo_client.push_order_note(int(order["channel_order_id"]),
                                   f"Tracking: {tracking} ({carrier})")

    append_audit("system", "order.tracking.push", f"order:{order_id}",
                 after={"tracking": tracking, "carrier": carrier})
    return {"tracking": tracking, "carrier": carrier}
