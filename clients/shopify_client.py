import logging
import time

import requests

from config import cfg, require

log = logging.getLogger(__name__)


class ShopifyThrottled(Exception):
    """429 — Retry-After 대기 후 재시도."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"429 retry-after={retry_after}")


class ShopifyAuthError(Exception):
    """401/403 — 토큰/스코프 문제. 재시도 불가."""


class ShopifyClient:
    def __init__(self):
        require("shopify")
        self.url = (
            f"https://{cfg.shopify_shop}/admin/api/"
            f"{cfg.shopify_api_version}/graphql.json"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": cfg.shopify_token,
            "Content-Type": "application/json",
        })
        self.available = 1000
        self.restore = 100
        self.query_cost_estimate = 10

    def wait_for_budget(self, need: int):
        """P1-9: 요청 전 cost 예산 대기 (복원 속도 기준)."""
        while self.available < need:
            deficit = need - self.available
            wait_s = max(1.0, deficit / max(self.restore, 1))
            log.warning("shopify throttle budget low (%s<%s), sleeping %.1fs",
                        self.available, need, wait_s)
            time.sleep(wait_s)
            self.available = min(1000, self.available + self.restore * wait_s)

    def execute(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            # 비용 예산 확인 후 전송
            self.wait_for_budget(self.query_cost_estimate)
            resp = self.session.post(
                self.url,
                json={"query": query, "variables": variables or {}},
                timeout=60,
            )
            if resp.status_code in (401, 403):
                raise ShopifyAuthError(
                    f"Shopify auth error {resp.status_code} — check token/scopes")
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2"))
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            body = resp.json()
            cost = body.get("extensions", {}).get("cost", {})
            if cost:
                ts = cost.get("throttleStatus", {})
                self.available = ts.get("currentlyAvailable", self.available)
                self.restore = ts.get("restoreRate", self.restore)
                nxt = cost.get("nextQueryCost")
                if nxt is not None:
                    self.query_cost_estimate = int(nxt)
            if body.get("errors"):
                raise RuntimeError(body["errors"])
            data = body.get("data")
            if data is None:
                raise RuntimeError(f"Shopify null data: {body}")
            return data
        raise ShopifyThrottled(2)


PRODUCT_CREATE = """
mutation ProductCreate($input: ProductInput!) {
  productCreate(input: $input) {
    product { id title }
    userErrors { field message }
  }
}
"""

PRODUCT_VARIANTS_UPDATE = """
mutation ProductVariantsUpdate($productId: ID!, $variants: [ProductVariantInput!]!) {
  productVariantsUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku price inventoryItem { id } }
    userErrors { field message }
  }
}
"""

VARIANTS_BULK_CREATE = """
mutation VariantsBulkCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants) {
    productVariants { id sku inventoryItem { id } }
    userErrors { field message }
  }
}
"""

INVENTORY_ADJUST = """
mutation Adjust($input: InventoryAdjustQuantitiesInput!) {
  inventoryAdjustQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message }
  }
}
"""

PUBLICATIONS_QUERY = """
query { publications(first: 20) { nodes { id catalog { title } } } }
"""

PUBLISH = """
mutation($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""

UNPUBLISH = """
mutation($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""

ORDERS_QUERY = """
query Orders($cursor: String) {
  orders(first: 50, after: $cursor,
         query: "fulfillment_status:unfulfilled AND status:open") {
    edges {
      node {
        id name displayFinancialStatus
        totalPriceSet { shopMoney { amount currencyCode } }
        email
        shippingAddress { address1 city province country zip phone name countryCodeV2 }
        lineItems(first: 50) {
          edges { node {
            id quantity sku name
            originalUnitPriceSet { shopMoney { amount } }
          } }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

FULFILLMENT_ORDERS = """
query($orderId: ID!) {
  order(id: $orderId) {
    fulfillmentOrders(first: 10) {
      edges { node {
        id status
        lineItems(first: 50) {
          edges { node { id remainingQuantity lineItem { id } } }
        }
      } }
    }
  }
}
"""

FULFILLMENT_CREATE = """
mutation($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status trackingInfo { number url } }
    userErrors { field message }
  }
}
"""


def _collect_options(p) -> list[dict]:
    """P1-8: 변형 options를 이름별로 수집해 productOptions 정의 생성."""
    by_name: dict[str, list[str]] = {}
    for v in p.variants:
        for k, val in v.options.items():
            vals = by_name.setdefault(k, [])
            if val not in vals:
                vals.append(val)
    return [{"name": name, "values": values}
            for name, values in sorted(by_name.items())]


def create_product(client: ShopifyClient, p, canonical_sku: str) -> dict:
    options = _collect_options(p)
    input_ = {
        "title": p.title,
        "descriptionHtml": p.description_html,
        "status": "DRAFT",
        "tags": p.tags,
    }
    # productVariantsBulkCreate 의 optionValues 는 상품에 옵션이 정의돼야 유효하다.
    # 2개 이하면 productCreate 입력에서 함께 선언할 수 있다.
    if len(options) <= 2:
        input_["productOptions"] = [
            {"name": o["name"], "values": o["values"][:3], "position": i + 1}
            for i, o in enumerate(options)
        ] if options else None
        if input_["productOptions"] is None:
            del input_["productOptions"]

    data = client.execute(PRODUCT_CREATE, {"input": input_})
    errs = data["productCreate"]["userErrors"]
    if errs:
        raise RuntimeError(errs)
    product_gid = data["productCreate"]["product"]["id"]

    # 3개 초과 옵션은 productCreate 에서 선언 불가 → productOptionsCreate 로 추가
    if len(options) > 2:
        opts_mutation = """
        mutation ProductOptionsCreate($productId: ID!, $options: [ProductOptionInput!]!) {
          productOptionsCreate(productId: $productId, options: $options) {
            product { id }
            userErrors { field message }
          }
        }"""
        d = client.execute(opts_mutation, {
            "productId": product_gid,
            "options": [{"name": o["name"], "values": o["values"][:3]}
                        for o in options],
        })
        e = d["productOptionsCreate"]["userErrors"]
        if e:
            raise RuntimeError(e)

    variants_input = [{
        "price": str(v.price),
        "sku": v.supplier_sku,
        "optionValues": [
            {"optionName": k, "name": val} for k, val in v.options.items()
        ],
        "inventoryQuantities": [{
            "locationId": cfg.shopify_location_id,
            "name": "available",
            "quantity": v.stock,
        }],
    } for v in p.variants]

    created = []
    for i in range(0, len(variants_input), 250):
        chunk = variants_input[i:i + 250]
        d = client.execute(VARIANTS_BULK_CREATE, {
            "productId": product_gid,
            "variants": chunk,
        })
        e = d["productVariantsBulkCreate"]["userErrors"]
        if e:
            raise RuntimeError(e)
        created.extend(d["productVariantsBulkCreate"]["productVariants"])

    # P1-8: 응답 순서 보장이 없으므로 SKU 기준으로 원본 변형과 매핑
    stock_by_sku = {v.supplier_sku: v.stock for v in p.variants}
    _record_variants(canonical_sku, product_gid, created, stock_by_sku)
    return {"product_gid": product_gid, "variants": created}


def update_product_prices(client: ShopifyClient, product_gid: str,
                          price_by_sku: dict[str, str]) -> None:
    """재동기화 시 기존 채널 상품 가격 갱신 (중복 생성 방지 경로)."""
    if not price_by_sku:
        return
    variants = [{"sku": sku, "price": price} for sku, price in price_by_sku.items()]
    for i in range(0, len(variants), 100):
        d = client.execute(PRODUCT_VARIANTS_UPDATE, {
            "productId": product_gid,
            "variants": variants[i:i + 100],
        })
        e = d["productVariantsUpdate"]["userErrors"]
        if e:
            raise RuntimeError(e)


def _record_variants(canonical_sku, product_gid, created, stock_by_sku: dict[str, int]):
    from db import get_conn
    with get_conn() as conn:
        for c in created:
            supplier_sku = c.get("sku") or ""
            inventory_item = (c.get("inventoryItem") or {}).get("id")
            conn.execute(
                """INSERT INTO channel_variants
                   (canonical_sku, channel, channel_product_id,
                    channel_variant_id, channel_sku, inventory_item_id,
                    last_pushed_qty)
                   VALUES (?, 'shopify', ?, ?, ?, ?, ?)
                   ON CONFLICT(channel, channel_variant_id) DO UPDATE SET
                     canonical_sku = excluded.canonical_sku,
                     channel_product_id = excluded.channel_product_id,
                     channel_sku = excluded.channel_sku,
                     inventory_item_id = excluded.inventory_item_id""",
                (
                    canonical_sku, product_gid, c["id"], supplier_sku,
                    inventory_item,
                    stock_by_sku.get(supplier_sku, 0),
                ),
            )


def adjust_inventory(client: ShopifyClient, inventory_item_gid: str,
                     delta: int, reason: str = "correction"):
    if delta == 0:
        return
    d = client.execute(INVENTORY_ADJUST, {
        "input": {
            "reason": reason,
            "name": "available",
            "changes": [{
                "delta": delta,
                "inventoryItemId": inventory_item_gid,
                "locationId": cfg.shopify_location_id,
            }],
        }
    })
    errs = d["inventoryAdjustQuantities"]["userErrors"]
    if errs:
        raise RuntimeError(errs)


def list_publications(client: ShopifyClient) -> list[dict]:
    return client.execute(PUBLICATIONS_QUERY)["publications"]["nodes"]


def set_listing(client: ShopifyClient, product_gid: str,
                publication_ids: list[str], visible: bool):
    inputs = [{"publicationId": pid} for pid in publication_ids]
    mutation = PUBLISH if visible else UNPUBLISH
    d = client.execute(mutation, {"id": product_gid, "input": inputs})
    key = "publishablePublish" if visible else "publishableUnpublish"
    if d[key]["userErrors"]:
        raise RuntimeError(d[key]["userErrors"])


def collect_orders(client: ShopifyClient) -> list[dict]:
    out = []
    cursor = None
    while True:
        data = client.execute(ORDERS_QUERY, {"cursor": cursor})
        page = data["orders"]
        out.extend([e["node"] for e in page["edges"]])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return out


def fulfill_order(client: ShopifyClient, order_gid: str,
                  tracking_number: str, tracking_url: str, carrier: str):
    """P1-11: 송장번호 없이는 fulfillment 생성 금지. 이미 처리된 주문은 멱등 skip."""
    if not tracking_number:
        raise ValueError("refusing to fulfill without a tracking number")
    fo = client.execute(FULFILLMENT_ORDERS, {"orderId": order_gid})
    fos = [e["node"] for e in fo["order"]["fulfillmentOrders"]["edges"]]
    if not fos:
        raise RuntimeError("no fulfillment orders")

    line_items = []
    for f in fos:
        for li in f["lineItems"]["edges"]:
            node = li["node"]
            if node["remainingQuantity"] <= 0:
                continue
            line_items.append({
                "fulfillmentOrderId": f["id"],
                "fulfillmentOrderLineItems": [{
                    "id": node["id"],
                    "quantity": node["remainingQuantity"],
                }],
            })
    if not line_items:
        # P1-11: 이미 fulfill 된 주문 재처리 — 에러가 아닌 멱등 skip
        return {"skipped": "nothing_to_fulfill"}

    d = client.execute(FULFILLMENT_CREATE, {
        "fulfillment": {
            "lineItemsByFulfillmentOrder": line_items,
            "trackingInfo": {
                "number": tracking_number,
                "url": tracking_url,
                "company": carrier,
            },
            "notifyCustomer": False,
        }
    })
    errs = d["fulfillmentCreate"]["userErrors"]
    if errs:
        raise RuntimeError(errs)
    return d["fulfillmentCreate"]["fulfillment"]
