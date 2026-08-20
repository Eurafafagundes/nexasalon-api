"""Schemas do fluxo de Inventário (contagem do sistema vs. contagem
real). Ver `services/inventory_counts.py` para o raciocínio do
fechamento (gera `StockMovement` de ajuste só onde há diferença, exige
todos os itens contados antes de fechar)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from nexasalon_api.models.enums import InventoryCountStatus


class InventoryCountCreate(BaseModel):
    branch_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=1000)


class InventoryCountItemCount(BaseModel):
    """`counted_quantity=None` reabre o campo pra "ainda não contado"
    (item "nunca sobrescrever silenciosamente" — o fechamento recusa
    fechar enquanto sobrar item nesse estado)."""

    counted_quantity: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)


class InventoryCountItemReadOut(BaseModel):
    """`difference` é calculada aqui (não persistida no banco) —
    `counted_quantity - system_quantity`, `None` enquanto o item ainda
    não foi contado."""

    id: uuid.UUID
    product_id: uuid.UUID
    system_quantity: Decimal
    counted_quantity: Decimal | None
    difference: Decimal | None = None

    @classmethod
    def from_model(cls, item) -> "InventoryCountItemReadOut":
        difference = (item.counted_quantity - item.system_quantity) if item.counted_quantity is not None else None
        return cls(
            id=item.id,
            product_id=item.product_id,
            system_quantity=item.system_quantity,
            counted_quantity=item.counted_quantity,
            difference=difference,
        )


class InventoryCountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    branch_id: uuid.UUID
    status: InventoryCountStatus
    notes: str | None
    created_by: uuid.UUID
    created_by_name: str
    closed_at: datetime | None
    closed_by: uuid.UUID | None
    closed_by_name: str | None
    created_at: datetime


class InventoryCountDetail(InventoryCountRead):
    items: list[InventoryCountItemReadOut] = []
