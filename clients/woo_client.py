import logging
from decimal import Decimal

from config import cfg, require
from models import NormalizedProduct
from db import get_conn

log = logging.getLogger(__name__)


def _api():
    """P2-23: import 시점이 아니라 최초 사용 시점에 클라이언트를 생성한다."""
    require("woo")
    from woocommerce import API
    return API(
        url=cfg.wc_url,
        consumer_key=cfg.wc_ck,
        consumer_secret=cfg.wc_cs,
        version="wc/v3",
        timeout=60,
    )


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _extract_attributes(variants):
    options: dict[str, set] = {}
    for v in variants:
        for k, val in v.options.items():
            options.setdefault(k, set()).add(val)
    return [
        {"name": k, "visible": True, "variation": True, "options": sorted(vals)}
        for k, vals in options.items()
    ]


def create_product(p: NormalizedProduct, canonical_sku: str) -> dict:
    attributes = _extract_attributes(p.variants)
    payload = {
        "name": p.title,
        "type": "variable" if len(p.variants) > 1 else "simple",
        "status": "draft",
        "description": p.description_html,
        "images": [{"src": u} for u in p.images],
        "attributes": attributes,
    }
    if len(p.variants) == 1:
        v = p.variants[0]
        payload.update({
            "regular_price": str(v.price),
            "sku": v.supplier_sku,
            "manage_stock": True,
            "stock_quantity": v.stock,
        })

    resp = _api().post("products", payload)
    resp.raise_for_status()
    product = resp.json()

    if len(p.variants) > 1:
        _create_variations(product["id"], p.variants, attributes, canonical_sku)
    else:
        _record_channel_variant(
            canonical_sku, "woocommerce",
            str(product["id"]), str(product["id"]),
            product.get("sku"), p.variants[0].stock,
        )

    return product


def _create_variations(product_id: int, variants, attributes, canonical_sku: str):
    attr_names = [a["name"] for a in attributes]
    items = [{
        "regular_price": str(v.price),
        "sku": v.supplier_sku,
        "manage_stock": True,
        "stock_quantity": v.stock,
        "attributes": [
            {"name": name, "option": v.options[name]}
            for name in attr_names if name in v.options
        ],
    } for v in variants]

    for chunk in _chunks(items, 100):
        resp = _api().post(f"products/{product_id}/variations/batch", {"create": chunk})
        resp.raise_for_status()
        created = resp.json().get("create", [])
        for c in created:
            _record_channel_variant(
                canonical_sku, "woocommerce", str(product_id), str(c["id"]),
                c.get("sku"), 0,
            )


def _record_channel_variant(canonical_sku, channel, product_id, variant_id, sku, qty):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO channel_variants
               (canonical_sku, channel, channel_product_id,
                channel_variant_id, channel_sku, last_pushed_qty)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (canonical_sku, channel, product_id, variant_id, sku, qty),
        )


def batch_update_variations(product_id: int, updates: list[dict]) -> None:
    for chunk in _chunks(updates, 100):
        resp = _api().post(
            f"products/{product_id}/variations/batch", {"update": chunk}
        )
        resp.raise_for_status()


def update_product_details(p: NormalizedProduct, product_id: int) -> None:
    """P1-10: 재동기화 시 기존 Woo 상품 업데이트 경로.

    NOTE(운영 확인 필요): variable 상품의 attributes/options 를 PUT 로 추가·변경하는 것은
    WC REST 에서 제약이 있어, 여기서는 simple 상품은 prices/stock 을 직접 갱신하고
    variable 상품은 variation 의 price/stock 만 batch update 한다. 옵션 구조 변경이
    필요한 경우 수동 개입이 필요하다.
    """
    api = _api()
    if len(p.variants) == 1:
        v = p.variants[0]
        resp = api.put(f"products/{product_id}", {
            "regular_price": str(v.price),
            "manage_stock": True,
            "stock_quantity": v.stock,
        })
        resp.raise_for_status()
        return

    # variable: SKU → variation id 매핑 후 가격/재고만 갱신
    resp = api.get(f"products/{product_id}/variations", params={"per_page": 100})
    resp.raise_for_status()
    id_by_sku = {c.get("sku"): c["id"] for c in resp.json() if c.get("sku")}
    updates = []
    for v in p.variants:
        vid = id_by_sku.get(v.supplier_sku)
        if vid is None:
            log.warning("variation for sku %s not found on product %s",
                        v.supplier_sku, product_id)
            continue
        updates.append({"id": vid, "regular_price": str(v.price),
                        "manage_stock": True, "stock_quantity": v.stock})
    batch_update_variations(product_id, updates)


def set_listing_visibility(product_id: int, visible: bool) -> dict:
    resp = _api().put(
        f"products/{product_id}",
        {"catalog_visibility": "visible" if visible else "hidden"},
    )
    resp.raise_for_status()
    return resp.json()


def list_orders(status: str = "processing", page: int = 1) -> list[dict]:
    resp = _api().get("orders", params={"status": status, "per_page": 100, "page": page})
    resp.raise_for_status()
    return resp.json()


def iter_all_orders(statuses: list[str]) -> list[dict]:
    """P1-12: 상태별 전체 페이지네이션 순회 (page=1 고정 누락 방지)."""
    api = _api()
    out: list[dict] = []
    seen: set[str] = set()
    for status in statuses:
        page = 1
        while True:
            resp = api.get("orders", params={
                "status": status, "per_page": 100, "page": page})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for o in batch:
                key = f"{o['id']}"
                if key not in seen:
                    seen.add(key)
                    out.append(o)
            # X-WP-TotalPages 헤더 기반 종료 (없으면 반환 개수로 판단)
            total_pages = int(resp.headers.get("X-WP-TotalPages", 0) or 0)
            if total_pages and page >= total_pages:
                break
            if len(batch) < 100:
                break
            page += 1
    return out


def update_product_stock(product_id: int, qty: int) -> None:
    """P1-7: 단순(simple) 상품은 products/{id} PUT 으로 재고 갱신."""
    resp = _api().put(f"products/{product_id}", {
        "manage_stock": True, "stock_quantity": qty,
    })
    resp.raise_for_status()


def push_order_note(order_id: int, note: str) -> dict:
    resp = _api().post(
        f"orders/{order_id}/notes",
        {"note": note, "customer_note": False},
    )
    resp.raise_for_status()
    return resp.json()


def update_order_status(order_id: int, status: str) -> dict:
    resp = _api().put(f"orders/{order_id}", {"status": status})
    resp.raise_for_status()
    return resp.json()


# ---- 리뷰 ----
def list_reviews(product_id: int | None = None) -> list[dict]:
    import requests
    require("woo")
    params = {"per_page": 100}
    if product_id:
        params["product"] = product_id
    resp = requests.get(
        f"{cfg.wc_url}/wp-json/wc/v3/products/reviews",
        params=params,
        auth=(cfg.wc_ck, cfg.wc_cs),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def reply_to_review(review_id: int, body: str) -> dict:
    import requests
    require("woo", "wp")
    resp = requests.post(
        f"{cfg.wc_url}/wp-json/wp/v2/comments",
        auth=(cfg.wp_user, cfg.wp_app_password),
        json={"content": body, "parent": review_id, "status": "approve"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
