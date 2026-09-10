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


class AvailabilityCheckRead(BaseModel):
    """Resposta de `GET /agenda/availability-check` — item "mensagens
    específicas de indisponibilidade": diagnóstico de UM horário
    específico (profissional+serviço+início+duração), reaproveitando
    100% a MESMA validação de `create_appointment`
    (`services/appointments.py::_build_item_snapshot`, só sem persistir
    nada) — nunca uma segunda regra de disponibilidade no backend, e o
    frontend nunca precisa adivinhar/duplicar essa regra: só formata os
    campos abaixo, já classificados aqui.

    `reason` é o mesmo vocabulário já usado em `DomainError.details`
    pros erros de criação (`professional_hours`/`business_hours`/
    `schedule_block`/`conflict`) — `None` quando `available=True`, ou
    quando o motivo é um caso genérico não coberto por esses 4 (ex.:
    profissional inativo) — a UI cai no fallback genérico nesse caso.
    `professional_name`/`window_end`/`computed_end`/`duration_minutes`
    só vêm preenchidos quando o `reason` for `professional_hours` ou
    `business_hours` E houver uma janela real pra reportar (ver
    `_professional_hours_rejection`/`_business_hours_rejection`) —
    `None` no caso "sem jornada nenhuma cadastrada esse dia", que não
    tem horário de fim pra mostrar."""

    available: bool
    reason: str | None = None
    message: str | None = None
    professional_name: str | None = None
    window_end: str | None = None
    computed_end: str | None = None
    duration_minutes: int | None = None
