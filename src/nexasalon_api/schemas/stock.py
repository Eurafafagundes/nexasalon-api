"""Schemas de saldo/movimentação/transferência de estoque. Mesma regra
de `schemas/product.py` pra custo: `StockMovementRead` nunca inclui
`unit_cost`; `StockMovementReadWithCost` inclui — a rota escolhe pelo
`inventory.view_cost` do ator."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.enums import StockMovementDirection, StockMovementReason


class StockLevelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    branch_id: uuid.UUID
    quantity_on_hand: Decimal
    minimum_quantity: Decimal


class StockLevelMinimumUpdate(BaseModel):
    minimum_quantity: Decimal = Field(ge=0, max_digits=12, decimal_places=3)


# Motivos permitidos numa movimentação CRIADA MANUALMENTE (rota
# `POST /stock-movements`) — `TRANSFER_IN`/`TRANSFER_OUT` só nascem de
# `POST /stock-transfers`, e `INVENTORY_COUNT` só do fechamento de um
# inventário (ver `services/stock.py`).
MANUAL_REASONS_BY_DIRECTION: dict[StockMovementDirection, frozenset[StockMovementReason]] = {
    StockMovementDirection.IN: frozenset(
        {StockMovementReason.PURCHASE, StockMovementReason.RETURN, StockMovementReason.ADJUSTMENT}
    ),
    StockMovementDirection.OUT: frozenset(
        {
            StockMovementReason.SALE,
            StockMovementReason.INTERNAL_USE,
            StockMovementReason.DAMAGE,
            StockMovementReason.ADJUSTMENT,
        }
    ),
}


class StockMovementCreate(BaseModel):
    product_id: uuid.UUID
    branch_id: uuid.UUID
    direction: StockMovementDirection
    reason: StockMovementReason
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    observation: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check_reason_matches_direction(self) -> "StockMovementCreate":
        allowed = MANUAL_REASONS_BY_DIRECTION[self.direction]
        if self.reason not in allowed:
            allowed_labels = ", ".join(sorted(r.value for r in allowed))
            raise ValueError(
                f"Motivo '{self.reason.value}' não é válido para direção '{self.direction.value}'. "
                f"Motivos válidos: {allowed_labels}."
            )
        return self


class StockMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    branch_id: uuid.UUID
    direction: StockMovementDirection
    reason: StockMovementReason
    quantity: Decimal
    observation: str | None
    created_by: uuid.UUID
    created_by_name: str
    order_id: uuid.UUID | None
    transfer_id: uuid.UUID | None
    inventory_count_id: uuid.UUID | None
    created_at: datetime


class StockMovementReadWithCost(StockMovementRead):
    unit_cost: Decimal | None


class StockTransferCreate(BaseModel):
    product_id: uuid.UUID
    origin_branch_id: uuid.UUID
    destination_branch_id: uuid.UUID
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    observation: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check_distinct_branches(self) -> "StockTransferCreate":
        if self.origin_branch_id == self.destination_branch_id:
            raise ValueError("Unidade de origem e destino devem ser diferentes.")
        return self


class StockTransferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    origin_branch_id: uuid.UUID
    destination_branch_id: uuid.UUID
    quantity: Decimal
    observation: str | None
    created_by: uuid.UUID
    created_by_name: str
    created_at: datetime
    movements: list[StockMovementRead] = []


class LowStockItem(BaseModel):
    product_id: uuid.UUID
    product_name: str
    branch_id: uuid.UUID
    quantity_on_hand: Decimal
    minimum_quantity: Decimal


class MostConsumedItem(BaseModel):
    product_id: uuid.UUID
    product_name: str
    total_quantity_out: Decimal


class StockFlowPoint(BaseModel):
    date: str
    in_total: Decimal
    out_total: Decimal


class StockOverview(BaseModel):
    """Visão Geral do Estoque. `stock_value` é `None` quando o ator não
    tem `inventory.view_cost` (item "Ver estoque ≠ Ver custo dos
    produtos") — nunca um zero enganoso."""

    products_in_stock: int
    products_out_of_stock: int
    low_stock_count: int
    stock_value: Decimal | None
    low_stock_items: list[LowStockItem]
    most_consumed: list[MostConsumedItem]
    flow: list[StockFlowPoint]
