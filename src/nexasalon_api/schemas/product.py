"""Schemas de Produto. `ProductRead`/`ProductReadWithCost` cobrem o item
"Ver estoque ≠ Ver custo dos produtos": a rota decide qual das duas
serializar por request, de acordo com `inventory.view_cost` estar ou
não no `actor.permissions` (ver `api/v1/products.py`) — nunca serializa
`cost_price` pra quem não tem a permission."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from nexasalon_api.models.enums import ProductUnit


class ProductBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    sku: str | None = Field(default=None, max_length=60)
    unit: ProductUnit = ProductUnit.UNIT
    cost_price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    sale_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    supplier_name: str | None = Field(default=None, max_length=160)
    for_sale: bool = True
    notes: str | None = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(ProductBase):
    pass


class ProductRead(BaseModel):
    """Sem `cost_price` — usada quando o ator NÃO tem `inventory.view_cost`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    category: str | None
    sku: str | None
    unit: ProductUnit
    sale_price: Decimal | None
    supplier_name: str | None
    for_sale: bool
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ProductReadWithCost(ProductRead):
    """Usada quando o ator TEM `inventory.view_cost`."""

    cost_price: Decimal
