import logging
from decimal import Decimal

from clients.cj_client import CJClient
from clients import woo_client, shopify_client
from db import get_conn
from models import NormalizedProduct
from services.normalizer import build_normalized
from services.pricing import compute_sell_price
from utils.audit import append_audit

log = logging.getLogger(__name__)

_shopify_client = None


def _shopify():
    """P2-23: import 시점 ShopifyClient() 생성 제거 — 지연 생성."""
    global _shopify_client
    if _shopify_client is None:
        _shopify_client = shopify_client.ShopifyClient()
    return _shopify_client


def _apply_pricing(p: NormalizedProduct, cj: CJClient | None = None) -> NormalizedProduct:
    """P1-13: 운임 계산을 가격 산정에 연결.

    FreightEstimator(추가 기능 §2): freightCalculate 는 국가별이라 주문 시점에
    정확히 계산되며, 상품 등록 단계에서는 기본 운임 설정(PRICING_DEFAULT_FREIGHT)을
    반영한다. (주문 시 재계산은 TODO/운영 정책)

    PriceGuard(추가 기능 §1): 마진 하한(PRICE_MIN_MARGIN) 미달이면 해당 변형의
    가격 인상을 보류하고 경고한다 — 조용한 마이너스 마진 판매를 막는다.
    """
    from config import cfg
    freight = cfg.pricing_default_freight
    min_margin = getattr(cfg, "pricing_min_margin", Decimal("0"))
    for v in p.variants:
        supplier_price = v.price
        new_price = compute_sell_price(v.price, freight=freight)
        cost = supplier_price + freight
        if cost > 0 and (new_price - cost) / cost < min_margin:
            log.warning("price guard: sku %s margin below floor (%s < %s) — hold at cost+freight",
                        v.supplier_sku, (new_price - cost) / cost, min_margin)
            v.price = max(new_price, cost.quantize(Decimal("0.01")))
        else:
            v.price = new_price
    return p


def sync_one(cj_raw: dict, cj: CJClient) -> dict:
    pid = str(cj_raw["pid"])
    sku_data = cj.variant_query(pid)
    normalized = build_normalized(cj_raw, sku_data)
    if not normalized.variants:
        return {"skipped": "no_variants"}

    _apply_pricing(normalized)
    canonical = normalized.canonical_sku

    with get_conn() as conn:
        # P0-4 계열: REPLACE 대신 ON CONFLICT DO UPDATE (FK id 안정성)
        conn.execute(
            """INSERT INTO canonical_products (canonical_sku, title)
               VALUES (?, ?)
               ON CONFLICT(canonical_sku) DO UPDATE SET title = excluded.title""",
            (canonical, normalized.title),
        )
        for v in normalized.variants:
            conn.execute(
                """INSERT INTO supplier_products
                   (supplier, supplier_pid, supplier_sku, supplier_vid, raw_json, last_synced)
                   VALUES ('cj', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(supplier, supplier_sku) DO UPDATE SET
                     supplier_pid = excluded.supplier_pid,
                     supplier_vid = excluded.supplier_vid,
                     raw_json     = excluded.raw_json,
                     last_synced  = excluded.last_synced""",
                (pid, v.supplier_sku, v.supplier_vid, str(cj_raw)),
            )
            conn.execute(
                """INSERT INTO mapping
                   (canonical_sku, supplier, supplier_sku, priority, verified)
                   VALUES (?, 'cj', ?, 1, 1)
                   ON CONFLICT(canonical_sku, supplier, supplier_sku) DO UPDATE SET
                     priority = excluded.priority,
                     verified = excluded.verified""",
                (canonical, v.supplier_sku),
            )

    results = {}
    for name in ("woocommerce", "shopify"):
        try:
            existing = _channel_existing(name, canonical)
            if existing:
                # P1-10: 재동기화 — 채널 매핑이 있으면 create 대신 update 경로
                results[name] = _update_channel(name, existing, normalized)
                append_audit("system", "product.update",
                             f"{name}:{canonical}", after={"path": "update"})
            else:
                out = _create_channel(name, normalized, canonical)
                results[name] = "ok"
                append_audit("system", "product.create",
                             f"{name}:{canonical}", after={"id": str(out)[:120]})
        except Exception as e:
            log.exception("upload %s failed: %s", name, e)
            results[name] = f"error: {e}"
    return results


def _channel_existing(channel: str, canonical_sku: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT channel_product_id FROM channel_variants
               WHERE channel = ? AND canonical_sku = ? LIMIT 1""",
            (channel, canonical_sku),
        ).fetchone()
    return dict(row) if row else None


def _create_channel(name: str, p: NormalizedProduct, canonical: str):
    if name == "woocommerce":
        return woo_client.create_product(p, canonical)
    return shopify_client.create_product(_shopify(), p, canonical)


def _update_channel(name: str, existing: dict, p: NormalizedProduct) -> str:
    if name == "shopify":
        price_by_sku = {v.supplier_sku: str(v.price) for v in p.variants}
        shopify_client.update_product_prices(
            _shopify(), existing["channel_product_id"], price_by_sku)
        return "updated"
    # WooCommerce: 변형 SKU 단위로 prices/stock PUT
    woo_client.update_product_details(p, int(existing["channel_product_id"]))
    return "updated"


def sync_by_keyword(keyword: str, pages: int = 1):
    cj = CJClient()
    for page in range(1, pages + 1):
        for raw in cj.list_products(keyword, page=page):
            log.info("syncing %s", raw.get("productNameEn"))
            yield sync_one(raw, cj)


def sync_changed_products(provider: str, body: dict) -> dict:
    """웹훅 워커용 (P2-19): 공급사/채널 상품 변경 이벤트 → 해당 canonical 상품만 재동기화.

    provider == 'cj' 인 경우 payload 의 productId(supplier_sku) 로 매핑된
    canonical 상품을 찾아 채널 가격/재고를 갱신한다. 그 외 채널의 product/update
    이벤트는 기록만 하고 재수집을 트리거하지 않는다(무한 루프 방지).
    """
    if provider != "cj":
        return {"skipped": f"provider {provider} product events are record-only"}
    supplier_sku = str(body.get("productId") or body.get("supplierSku") or "")
    if not supplier_sku:
        return {"skipped": "no product id in event"}
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT canonical_sku FROM mapping
               WHERE supplier_sku = ? AND verified = 1""",
            (supplier_sku,),
        ).fetchall()
    if not rows:
        return {"skipped": f"no verified mapping for {supplier_sku}"}
    from services.inventory_sync import sync_inventory
    updated = []
    for r in rows:
        res = sync_inventory(canonical_sku=r["canonical_sku"])
        updated.append({"canonical_sku": r["canonical_sku"], "result": res})
    return {"updated": updated}
