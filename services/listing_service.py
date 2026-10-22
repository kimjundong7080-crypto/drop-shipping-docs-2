from clients import woo_client, shopify_client
from db import get_conn
from utils.audit import append_audit

_shopify_client = None


def _shopify():
    """P2-23: 모듈 import 시점 ShopifyClient() 생성 제거 — 지연 생성."""
    global _shopify_client
    if _shopify_client is None:
        _shopify_client = shopify_client.ShopifyClient()
    return _shopify_client


def set_listing(canonical_sku: str, channels: list[str], visible: bool) -> dict:
    out = {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_variants WHERE canonical_sku = ?", (canonical_sku,)
        ).fetchall()
    if not rows:
        raise ValueError(f"no channel variants for {canonical_sku}")

    publication_ids = None
    if "shopify" in channels:
        publication_ids = [p["id"] for p in shopify_client.list_publications(_shopify())]

    seen = set()
    for r in rows:
        if r["channel"] not in channels:
            continue
        key = (r["channel"], r["channel_product_id"])
        if key in seen:
            continue
        seen.add(key)
        try:
            if r["channel"] == "woocommerce":
                woo_client.set_listing_visibility(int(r["channel_product_id"]), visible)
            elif r["channel"] == "shopify":
                shopify_client.set_listing(
                    _shopify(), r["channel_product_id"], publication_ids, visible
                )
            out[f"{r['channel']}:{r['channel_product_id']}"] = "ok"
            append_audit("system", "listing.set", f"{r['channel']}:{canonical_sku}",
                         after={"visible": visible})
        except Exception as e:
            out[f"{r['channel']}:{r['channel_product_id']}"] = f"error: {e}"
    return out
