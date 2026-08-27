"""Schemas de Comissões (Etapa C3) — ver `services/commissions.py` pra
fonte de verdade de cada campo. `commission_status=None` num item de
detalhamento é sempre "histórico anterior ao controle de comissões"
(`OrderItem` fechado antes da migration 0036) — nunca confundir com
`not_configured` (que É um `commission_status` explícito)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from nexasalon_api.models.enums import CommissionStatus, CommissionType


class CommissionProfessionalSummaryRead(BaseModel):
    professional_id: uuid.UUID
    professional_name: str
    production: Decimal
    commission_total: Decimal
    unconfigured_count: int
    unconfigured_amount: Decimal
    historical_count: int


class CommissionOverviewRead(BaseModel):
    date_from: datetime
    date_to: datetime
    production: Decimal
    known_commission_total: Decimal
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


class CommissionDetailRead(BaseModel):
    professional_id: uuid.UUID
    professional_name: str
    date_from: datetime
    date_to: datetime
    items: list[CommissionItemRead]
