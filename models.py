from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Variant:
    supplier_sku: str
    supplier_vid: str
    price: Decimal
    stock: int
    options: dict[str, str] = field(default_factory=dict)
    image: str | None = None


@dataclass
class NormalizedProduct:
    supplier: str
    supplier_pid: str
    title: str
    description_html: str
    images: list[str]
    variants: list[Variant]
    category: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def canonical_sku(self) -> str:
        return f"{self.supplier.upper()}-{self.supplier_pid}"
