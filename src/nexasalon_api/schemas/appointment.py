import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexasalon_api.models.enums import AppointmentSource, AppointmentStatus


class AppointmentItemCreate(BaseModel):
    """Entrada do cliente por item: profissional, serviço, início e
    (opcional) `price_override`. Fim e duração continuam SEMPRE
    calculados no servidor (nunca aceitos do cliente). `price_override`
    é a ÚNICA exceção deliberada — evolução do Novo Agendamento, item
    "valor editável por serviço": quando informado, substitui o preço
    efetivo do catálogo (`Service.default_price` /
    `ProfessionalService.price_override`) SÓ para este item, sem
    escrever de volta em nenhum dos dois. Mesmo desenho de
    `OrderItem.price` (camada 3 da cadeia de preço, ver docstring de
    `models/order.py`) — reaproveita o padrão de snapshot já existente,
    não cria um preço "duplicado" novo."""

    professional_id: uuid.UUID
    service_id: uuid.UUID
    start_at: datetime
    price_override: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)

    @field_validator("start_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_at deve incluir informação de fuso horário (ISO 8601 com offset).")
        return value


class AppointmentCreate(BaseModel):
    branch_id: uuid.UUID
    client_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=255)
    items: list[AppointmentItemCreate] = Field(min_length=1)
    force_overlap: bool = False
    # Encaixe (migration 0016) — característica do agendamento,
    # independente de `status` (ver docstring de `models/appointment.py`).
    # NÃO pula nenhuma validação de disponibilidade nesta versão.
    fit_in: bool = False


class AppointmentReplace(AppointmentCreate):
    """PUT — substitui a reserva inteira (unidade, cliente, notas e TODOS
    os itens). Semântica idempotente, igual a `WorkingHoursReplaceRequest`."""


class AppointmentStatusUpdate(BaseModel):
    status: AppointmentStatus


class AppointmentItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_id: uuid.UUID
    professional_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    price: Decimal
    status: AppointmentStatus | None


class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID
    client_id: uuid.UUID
    status: AppointmentStatus
    source: AppointmentSource
    notes: str | None
    fit_in: bool
    starts_at: datetime | None
    ends_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[AppointmentItemRead]
