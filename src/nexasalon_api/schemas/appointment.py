import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexasalon_api.models.enums import AppointmentSource, AppointmentStatus


class AppointmentItemCreate(BaseModel):
    """Entrada do cliente por item: SÓ profissional, serviço e início.
    Fim, duração e preço são sempre calculados no servidor (snapshot
    efetivo no momento do agendamento) — nunca aceitos do cliente, pra
    impedir adulteração de preço/duração pelo frontend."""

    professional_id: uuid.UUID
    service_id: uuid.UUID
    start_at: datetime

    @field_validator("start_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_at deve incluir informação de fuso horário (ISO 8601 com offset).")
        return value


class AppointmentCreate(BaseModel):
    branch_id: uuid.UUID
    client_id: uuid.UUID
    notes: str | None = None
    items: list[AppointmentItemCreate] = Field(min_length=1)
    force_overlap: bool = False


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
    starts_at: datetime | None
    ends_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[AppointmentItemRead]
