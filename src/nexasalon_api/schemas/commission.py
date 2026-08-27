"""Schemas de Comissões (Etapa C3/C4) — ver `services/commissions.py` pra
fonte de verdade de cada campo. `commission_status=None` num item de
detalhamento é sempre "histórico anterior ao controle de comissões"
(`OrderItem` fechado antes da migration 0036) — nunca confundir com
`not_configured` (que É um `commission_status` explícito).

Etapa C4 — "A pagar"/"Pago" nunca são campos próprios: sempre
derivados de `commission_status` + `commission_settlement_id` (ver
docstring de `services/commissions.py`, seção C4)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from nexasalon_api.models.enums import CommissionStatus, CommissionType


class CommissionProfessionalSummaryRead(BaseModel):
    professional_id: uuid.UUID
    professional_name: str
    production: Decimal
    commission_total: Decimal
    paid_total: Decimal
    pending_total: Decimal
    unconfigured_count: int
    unconfigured_amount: Decimal
    historical_count: int


class CommissionOverviewRead(BaseModel):
    date_from: datetime
    date_to: datetime
    production: Decimal
    known_commission_total: Decimal
    paid_total: Decimal
    pending_total: Decimal
    unconfigured_count: int
    unconfigured_amount: Decimal
    historical_count: int
    professionals: list[CommissionProfessionalSummaryRead]


class CommissionItemRead(BaseModel):
    order_item_id: uuid.UUID
    order_id: uuid.UUID
    order_number: int
    closed_at: datetime
    client_name: str
    service_name: str
    price: Decimal
    # `None` = histórico (fechado antes da migration 0036) — nunca
    # "not_configured" fingido.
    commission_status: CommissionStatus | None
    commission_type_snapshot: CommissionType | None
    commission_value_snapshot: Decimal | None
    commission_amount_snapshot: Decimal | None
    # `None` = "A pagar" (quando calculada) ou irrelevante; preenchido
    # = "Pago", id do `CommissionSettlement` que liquidou este item.
    commission_settlement_id: uuid.UUID | None


class CommissionDetailRead(BaseModel):
    professional_id: uuid.UUID
    professional_name: str
    date_from: datetime
    date_to: datetime
    items: list[CommissionItemRead]


# ---------------------------------------------------------------------------
# Etapa C4 — Ajustes auditáveis.
# ---------------------------------------------------------------------------


class CommissionAdjustmentCreate(BaseModel):
    professional_id: uuid.UUID
    # Positivo (bônus) ou negativo (correção/desconto) — nunca zero.
    amount: Decimal
    reason: str = Field(min_length=1, max_length=500)
    order_item_id: uuid.UUID | None = None

    @field_validator("amount")
    @classmethod
    def _amount_not_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("O valor do ajuste não pode ser zero.")
        return value

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Informe o motivo do ajuste.")
        return stripped


class CommissionAdjustmentRead(BaseModel):
    id: uuid.UUID
    professional_id: uuid.UUID
    order_item_id: uuid.UUID | None
    commission_settlement_id: uuid.UUID | None
    amount: Decimal
    reason: str
    created_by_name: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Etapa C4 — Fechamento/Pagamento de Comissão (settlements).
# ---------------------------------------------------------------------------


class CommissionSettlementCreate(BaseModel):
    professional_id: uuid.UUID
    date_from: datetime
    date_to: datetime
    # Ajustes pendentes escolhidos pra entrar nesta liquidação (opcional
    # — a maioria dos pagamentos não inclui nenhum).
    adjustment_ids: list[uuid.UUID] = Field(default_factory=list)


class CommissionSettlementRead(BaseModel):
    id: uuid.UUID
    professional_id: uuid.UUID
    professional_name: str
    period_start: datetime
    period_end: datetime
    production_total: Decimal
    commission_total: Decimal
    created_by_name: str | None
    paid_at: datetime


class CommissionSettlementItemRead(BaseModel):
    order_item_id: uuid.UUID
    order_id: uuid.UUID
    order_number: int
    closed_at: datetime
    client_name: str
    service_name: str
    price: Decimal
    commission_amount_snapshot: Decimal


class CommissionSettlementAdjustmentRead(BaseModel):
    id: uuid.UUID
    order_item_id: uuid.UUID | None
    amount: Decimal
    reason: str
    created_by_name: str | None
    created_at: datetime


class CommissionSettlementDetailRead(BaseModel):
    settlement: CommissionSettlementRead
    items: list[CommissionSettlementItemRead]
    adjustments: list[CommissionSettlementAdjustmentRead]
