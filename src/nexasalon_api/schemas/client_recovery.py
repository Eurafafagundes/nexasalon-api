import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from nexasalon_api.models.enums import (
    RecoveryContactChannel,
    RecoveryContactResult,
    RecoveryCycleStatus,
)

RecoverySubtab = Literal["pending", "in_progress", "concluded"]
RecoverySort = Literal[
    "days_desc", "days_asc", "total_spent_desc", "total_spent_asc",
    "last_visit_desc", "last_visit_asc", "next_contact_asc",
]

CONTACT_NOTES_MAX_LENGTH = 2000


class ContactLogCreate(BaseModel):
    """`POST /clients/{client_id}/recovery/contacts` — registra UM
    contato (item 11). `next_contact_at` obrigatório só quando o
    `contact_result` exige acompanhamento (validado no service, nunca
    só aqui — a regra depende do valor de outro campo); nunca aceita
    data no passado."""

    # Opcional — a UI simplificada de 3 cartões não pede o canal; nunca
    # inventamos um valor quando não informado (fica `None`, preservado
    # assim no histórico, já é `nullable` no banco).
    contact_channel: RecoveryContactChannel | None = None
    contact_result: RecoveryContactResult
    notes: str | None = Field(default=None, max_length=CONTACT_NOTES_MAX_LENGTH)
    next_contact_at: datetime | None = None
    # Só exigido/usado quando `contact_result == "return_scheduled"` —
    # o agendamento real já deve existir (fluxo "Agendar retorno" da
    # Agenda), nunca criado por este endpoint.
    linked_appointment_id: uuid.UUID | None = None

    @field_validator("notes", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class AssignmentUpdate(BaseModel):
    """`PATCH /clients/{client_id}/recovery/assignment` — transferir o
    responsável pelo ciclo ativo. `new_assigned_user_id=None` desatribui
    (fica "sem responsável", visível a qualquer um com acesso à fila)."""

    new_assigned_user_id: uuid.UUID | None
    expected_version: int = Field(ge=0)


class LinkAppointmentCreate(BaseModel):
    """`POST /clients/{client_id}/recovery/link-appointment` — chamado
    DEPOIS que o agendamento de retorno já foi criado pelo fluxo normal
    da Agenda (nunca cria agendamento aqui)."""

    appointment_id: uuid.UUID
    expected_version: int = Field(ge=0)


class RecoverySettingsUpdate(BaseModel):
    default_no_return_days: int | None = Field(default=None, gt=0)
    default_follow_up_days: int | None = Field(default=None, gt=0)
    default_snooze_days: int | None = Field(default=None, gt=0)
    whatsapp_message_template: str | None = Field(default=None, max_length=1000)


class RecoverySettingsRead(BaseModel):
    default_no_return_days: int
    default_follow_up_days: int
    default_snooze_days: int
    whatsapp_message_template: str


class RecoveryCycleRead(BaseModel):
    id: uuid.UUID
    status: RecoveryCycleStatus
    assigned_user_id: uuid.UUID | None
    assigned_user_name: str | None
    next_contact_at: datetime | None
    linked_appointment_id: uuid.UUID | None
    version: int


class RecoveryQueueRowRead(BaseModel):
    """Uma linha da fila — `phone`/`whatsapp_href`/`total_spent` podem
    vir `None` quando o ator não tem a permissão correspondente
    (`clients.view_contact_data`/`finance.view`), NUNCA mascarados só no
    frontend (ver `api/v1/client_recovery.py`, mesmo contrato de
    `ClientProfile`)."""

    client_id: uuid.UUID
    client_name: str
    client_since: datetime
    phone: str | None
    whatsapp_href: str | None
    last_visit_at: datetime | None
    days_since_last_visit: int | None
    last_professional_names: list[str]
    last_service_names: list[str]
    next_appointment_at: datetime | None
    total_spent: Decimal | None
    is_overdue: bool
    cycle: RecoveryCycleRead | None


class RecoveryQueueResponse(BaseModel):
    items: list[RecoveryQueueRowRead]
    total: int
    page: int
    page_size: int


class RecoverySummaryRead(BaseModel):
    pending_count: int
    in_progress_count: int
    # Clientes distintos encerrados como recuperado/sem interesse no
    # período (nunca ciclos) — `concluded_count` abaixo é que conta
    # ciclos, ver `services/client_recovery.py::RecoverySummary`.
    recovered_count: int
    not_interested_count: int
    concluded_count: int
    date_from: datetime
    date_to: datetime


class RecoveryEventRead(BaseModel):
    id: int
    event_type: str
    actor_user_name: str | None
    contact_channel: RecoveryContactChannel | None
    contact_result: RecoveryContactResult | None
    notes: str | None
    previous_status: RecoveryCycleStatus | None
    new_status: RecoveryCycleStatus | None
    next_contact_at: datetime | None
    linked_appointment_id: uuid.UUID | None
    created_at: datetime


class RecoveryTimelineResponse(BaseModel):
    items: list[RecoveryEventRead]
