from decimal import Decimal
from models import NormalizedProduct, Variant


def build_normalized(cj_raw: dict, sku_data: dict) -> NormalizedProduct:
    variants = []
    for v in sku_data.get("variants", []):
        options = {}
        std = v.get("variantStandard") or {}
        if isinstance(std, dict):
            options = {k: str(val) for k, val in std.items()}
        variants.append(Variant(
            supplier_sku=v["variantSku"],
            supplier_vid=str(v.get("vid", "")),
            price=Decimal(str(v.get("variantSellPrice", "0"))),
            stock=int(v.get("variantInventory", 0) or 0),
            options=options,
            image=v.get("variantImage"),
        ))

    images = []
    if cj_raw.get("productImage"):
        images.append(cj_raw["productImage"])
    for extra in cj_raw.get("productImageSet", []) or []:
        if extra not in images:
            images.append(extra)

    return NormalizedProduct(
        supplier="cj",
        supplier_pid=str(cj_raw["pid"]),
        title=cj_raw.get("productNameEn", ""),
        description_html=cj_raw.get("description", "") or "",
        images=images,
        variants=variants,
        category=cj_raw.get("categoryName"),
        tags=[t for t in (cj_raw.get("tags") or []) if isinstance(t, str)],
    )
