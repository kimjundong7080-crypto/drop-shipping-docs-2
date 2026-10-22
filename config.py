import os
from dataclasses import dataclass, field
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # CJ
    cj_base: str = os.environ.get("CJ_BASE", "https://developers.cjdropshipping.com/api2.0/v1")
    cj_email: str = field(default_factory=lambda: os.environ.get("CJ_EMAIL", ""), repr=False)
    cj_api_key: str = field(default_factory=lambda: os.environ.get("CJ_API_KEY", ""), repr=False)
    cj_open_id: str = field(default_factory=lambda: os.environ.get("CJ_OPEN_ID", ""), repr=False)
    cj_qps: float = float(os.environ.get("CJ_QPS", "1"))
    cj_sandbox: int = int(os.environ.get("CJ_SANDBOX", "0"))

    # WooCommerce
    wc_url: str = os.environ.get("WC_URL", "")
    wc_ck: str = field(default_factory=lambda: os.environ.get("WC_CK", ""), repr=False)
    wc_cs: str = field(default_factory=lambda: os.environ.get("WC_CS", ""), repr=False)
    wp_user: str = os.environ.get("WP_USER", "")
    wp_app_password: str = field(default_factory=lambda: os.environ.get("WP_APP_PASSWORD", ""), repr=False)

    # Shopify
    shopify_shop: str = os.environ.get("SHOPIFY_SHOP", "")
    shopify_token: str = field(default_factory=lambda: os.environ.get("SHOPIFY_TOKEN", ""), repr=False)
    shopify_api_version: str = os.environ.get("SHOPIFY_API_VERSION", "2025-10")
    shopify_client_secret: str = field(default_factory=lambda: os.environ.get("SHOPIFY_CLIENT_SECRET", ""), repr=False)
    shopify_location_id: str = os.environ.get("SHOPIFY_LOCATION_ID", "")

    # Local server
    local_api_key: str = field(default_factory=lambda: os.environ.get("LOCAL_API_KEY", ""), repr=False)
    local_bind_host: str = os.environ.get("LOCAL_BIND_HOST", "127.0.0.1")
    local_bind_port: int = int(os.environ.get("LOCAL_BIND_PORT", "8000"))
    webhook_bind_port: int = int(os.environ.get("WEBHOOK_BIND_PORT", "8001"))

    # Webhook secrets (P2-19: Woo 웹훅 서명 검증용)
    woo_webhook_secret: str = field(default_factory=lambda: os.environ.get("WOO_WEBHOOK_SECRET", ""), repr=False)

    # Pricing
    pricing_margin: Decimal = Decimal(os.environ.get("PRICING_MARGIN", "0.35"))
    pricing_rounding: Decimal = Decimal(os.environ.get("PRICING_ROUNDING", "0.99"))
    pricing_currency: str = os.environ.get("PRICING_CURRENCY", "USD")
    pricing_default_freight: Decimal = Decimal(os.environ.get("PRICING_DEFAULT_FREIGHT", "0"))
    # PriceGuard 하한 (추가 기능 §1): 이 마진 미만이면 인상 보류 + 경고
    pricing_min_margin: Decimal = Decimal(os.environ.get("PRICING_MIN_MARGIN", "0.10"))

    # Dispute / RefundGuard (추가 기능 §5·§11)
    dispute_max_refund_per_order: Decimal = Decimal(
        os.environ.get("DISPUTE_MAX_REFUND_PER_ORDER", "500"))

    # Orders
    woo_order_statuses: tuple = tuple(
        s.strip() for s in os.environ.get("WOO_ORDER_STATUSES", "processing,on-hold").split(",") if s.strip()
    )


cfg = Config()

# P2-16: import 시점에는 어떤 자격증명도 요구하지 않는다(명령별 require_* 지연 검증).
_REQUIRED_KEYS: dict[str, str] = {
    "cj": "CJ_EMAIL / CJ_API_KEY / CJ_OPEN_ID",
    "woo": "WC_URL / WC_CK / WC_CS",
    "wp": "WP_USER / WP_APP_PASSWORD",
    "shopify": "SHOPIFY_SHOP / SHOPIFY_TOKEN / SHOPIFY_CLIENT_SECRET / SHOPIFY_LOCATION_ID",
    "local": "LOCAL_API_KEY",
}
_CHECKED: set[str] = set()


def require(*groups: str) -> None:
    """특정 기능 그룹에 필요한 환경변수를 지연 검증. 누락은 한 번에 보고한다."""
    missing = []
    for g in groups:
        keys = [k.strip() for k in _REQUIRED_KEYS[g].split("/")]
        absent = [k for k in keys if not os.environ.get(k)]
        if absent:
            missing.append(f"{g}: {', '.join(absent)}")
    if missing:
        raise RuntimeError(
            "Missing required env — " + "; ".join(missing)
            + ". Set them in .env (see .env.example)."
        )
    _CHECKED.update(groups)
