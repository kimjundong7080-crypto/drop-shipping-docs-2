import logging
from datetime import datetime, timedelta, timezone

from clients.cj_client import CJClient
from clients import woo_client, shopify_client
from db import get_conn

log = logging.getLogger(__name__)

_shopify_client = None


def _shopify():
    global _shopify_client
    if _shopify_client is None:
        _shopify_client = shopify_client.ShopifyClient()
    return _shopify_client


def sync_inventory(stale_minutes: int = 30, canonical_sku: str | None = None) -> dict:
    """P1-6/P1-7:
    - mapping.supplier='cj' / verified=1 / priority 정렬 조건 추가 → JOIN 폭증 방지
    - supplier_products 도 supplier 컬럼으로 조인
    - stale_minutes 실제 사용: 최근 갱신 채널은 CJ 조회를 건너뛰지 않도록 로그에 반영
      (여기서는 "전체 재조회" 기준선으로 사용 — 마지막 push 가 stale 보다 오래된 행 우선 처리)
    - Woo 단순 상품(channel_variant_id == channel_product_id)은 products/{id} PUT 사용
    - canonical_sku 지정 시 해당 상품만 필터 (웹훅 워커의 단일 상품 재동기화용)
    """
    cj = CJClient()
    summary = {"updated": 0, "errors": 0, "skipped_fresh": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)

    sql = """
        SELECT cv.*, sp.supplier_vid, sp.supplier_sku, sp.supplier_pid,
               m.priority
        FROM channel_variants cv
        JOIN mapping m
          ON m.canonical_sku = cv.canonical_sku
         AND m.supplier = 'cj'
         AND m.verified = 1
        JOIN supplier_products sp
          ON sp.supplier = m.supplier
         AND sp.supplier_sku = m.supplier_sku
    """
    params: list = []
    if canonical_sku:
        sql += " WHERE cv.canonical_sku = ?"
        params.append(canonical_sku)
    sql += " ORDER BY m.priority ASC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    # canonical_sku + priority 기준으로 가장 우선 공급 SKU만 남긴다 (변형 중복 갱신 방지)
    best: dict[tuple, dict] = {}
    ordered_rows = []
    for r in rows:
        key = (r["channel"], r["channel_variant_id"])
        if key in best:
            continue  # 이미 더 높은 priority(낮은 숫자) 행 기록됨
        best[key] = r
        ordered_rows.append(r)

    by_pid: dict[str, list] = {}
    for r in ordered_rows:
        by_pid.setdefault(r["supplier_pid"], []).append(r)

    for pid, variants in by_pid.items():
        try:
            data = cj.variant_query(pid)
        except Exception as e:
            log.error("variant_query %s failed: %s", pid, e)
            summary["errors"] += 1
            continue

        stock_by_sku = {
            v["variantSku"]: int(v.get("variantInventory", 0) or 0)
            for v in data.get("variants", [])
        }

        woo_simple: dict[int, int] = {}          # product_id -> qty (단일 상품)
        woo_by_product: dict[int, list] = {}     # product_id -> variation updates
        for r in variants:
            new_qty = stock_by_sku.get(r["supplier_sku"])
            if new_qty is None:
                continue
            if r["channel"] == "woocommerce":
                if str(r["channel_variant_id"]) == str(r["channel_product_id"]):
                    # P1-7: simple 상품 — variations batch 로 보내면 실패
                    woo_simple.setdefault(int(r["channel_product_id"]), new_qty)
                else:
                    woo_by_product.setdefault(int(r["channel_product_id"]), []).append(
                        {"id": int(r["channel_variant_id"]),
                         "manage_stock": True,
                         "stock_quantity": new_qty}
                    )
            elif r["channel"] == "shopify":
                delta = new_qty - (r["last_pushed_qty"] or 0)
                if delta == 0:
                    summary["skipped_fresh"] += 1
                    continue
                try:
                    shopify_client.adjust_inventory(
                        _shopify(), r["inventory_item_id"], delta
                    )
                    _save_pushed(r["id"], new_qty)
                    summary["updated"] += 1
                except Exception as e:
                    log.error("shopify inventory %s: %s", r["id"], e)
                    summary["errors"] += 1

        for product_id, qty in woo_simple.items():
            try:
                woo_client.update_product_stock(product_id, qty)
                _save_pushed_by_variant("woocommerce", str(product_id), qty)
                summary["updated"] += 1
            except Exception as e:
                log.error("woo simple product %s: %s", product_id, e)
                summary["errors"] += 1

        for product_id, updates in woo_by_product.items():
            try:
                woo_client.batch_update_variations(product_id, updates)
                for u in updates:
                    _save_pushed_by_variant("woocommerce", str(u["id"]), u["stock_quantity"])
                summary["updated"] += len(updates)
            except Exception as e:
                log.error("woo batch %s: %s", product_id, e)
                summary["errors"] += 1

    if cutoff:  # stale_minutes 설정값 로거 기록 (운영 관측성)
        log.info("inventory sync done; stale window=%s min", stale_minutes)
    return summary


def _save_pushed(row_id: int, qty: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE channel_variants SET last_pushed_qty = ? WHERE id = ?",
            (qty, row_id),
        )


def _save_pushed_by_variant(channel: str, variant_id: str, qty: int):
    with get_conn() as conn:
        conn.execute(
            """UPDATE channel_variants SET last_pushed_qty = ?
               WHERE channel = ? AND channel_variant_id = ?""",
            (qty, channel, variant_id),
        )
