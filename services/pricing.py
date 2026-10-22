from decimal import Decimal, ROUND_CEILING
from config import cfg


def compute_sell_price(supplier_price: Decimal, freight: Decimal | None = None) -> Decimal:
    """P1-13: (공급가 + 운임) × (1+마진) 후 `.99` 규칙으로 반올림.

    규칙: 마진 적용 금액을 올림(ROUND_CEILING)해 정수 원 단위로 만든 뒤 0.01 을 빼고,
    그 값이 원금액보다 작으면 그대로 사용(예: 25.00 → 24.99). 이미 소수부가 .99 이상이면
    올림 결과가 다시 같은 자릿수가 되어 `.99` 가 유지된다.
    `pricing_rounding`(기본 0.99) 설정을 실제로 반영한다.
    """
    if freight is None:
        freight = cfg.pricing_default_freight
    base = supplier_price + freight
    with_margin = base * (Decimal("1") + cfg.pricing_margin)

    target_frac = cfg.pricing_rounding - Decimal(int(cfg.pricing_rounding))  # 0.99 -> .99
    integer_part = with_margin.to_integral_value(rounding=ROUND_CEILING)
    candidate = (integer_part - (Decimal("1") - target_frac)).quantize(Decimal("0.01"))
    if candidate < with_margin:
        # candidate 가 원금액보다 작으면 한 단계 더 올려 .99 로 맞춤
        candidate = (integer_part + Decimal("1") - (Decimal("1") - target_frac)).quantize(Decimal("0.01"))
    return candidate.quantize(Decimal("0.01"))


def assert_positive_margin(sell: Decimal, cost: Decimal) -> None:
    if sell <= cost:
        raise ValueError(f"negative margin: sell={sell} cost={cost}")
