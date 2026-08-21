import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from nexasalon_api.models.enums import AppointmentSource, AppointmentStatus


class AgendaItemRead(BaseModel):
    """Uma linha da agenda — um `AppointmentItem` "achatado" com os
    campos do `Appointment` pai que fazem sentido pra visualização em
    grade (cliente, unidade, status efetivo, origem). Montado
    manualmente na rota (não via `from_attributes` puro) porque parte
    dos campos vem do relacionamento `item.appointment`, não de
    colunas do item.

    `source` (Etapa K, ajuste): reaproveita a coluna
    `appointments.source` já existente (nenhuma migration nova) — só
    passou a ser exposta aqui pra que a Comanda aberta a partir da
    própria tela da Agenda também saiba se o agendamento veio da
    página pública, sem precisar de um fetch extra."""

    id: uuid.UUID
    appointment_id: uuid.UUID
    branch_id: uuid.UUID
    client_id: uuid.UUID
    service_id: uuid.UUID
    professional_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    price: Decimal
    status: AppointmentStatus
    source: AppointmentSource


class AvailabilitySlotRead(BaseModel):
    start_at: datetime
    end_at: datetime
