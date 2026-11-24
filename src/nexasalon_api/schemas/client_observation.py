import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ObservationOrigin = Literal["general", "appointment", "order"]

OBSERVATION_CONTENT_MAX_LENGTH = 4000


class ObservationCreate(BaseModel):
    """`POST /clients/{client_id}/observations` — sempre cria uma
    observação GERAL nova e independente (item "não sobrescrever
    automaticamente a observação geral anterior"). Observações de
    agendamento/comanda continuam editadas pelos endpoints de origem
    (`PATCH /appointments/{id}/notes`, `PATCH /orders/{id}/observation`)
    — nunca criadas por aqui."""

    content: str = Field(min_length=1, max_length=OBSERVATION_CONTENT_MAX_LENGTH)

    @field_validator("content", mode="after")
    @classmethod
    def _strip_and_require(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Conteúdo da observação não pode ser vazio.")
        return stripped


class ObservationUpdate(BaseModel):
    """`PATCH /clients/{client_id}/observations/{observation_id}` —
    edita uma observação GERAL existente. `expected_version` OBRIGATÓRIO
    (mesmo padrão de `OrderObservationUpdate.expected_observation_version`,
    migration 0039): o service recusa com 409 se não bater com a versão
    atual, antes de qualquer outra decisão."""

    content: str = Field(min_length=1, max_length=OBSERVATION_CONTENT_MAX_LENGTH)
    expected_version: int = Field(ge=0)

    @field_validator("content", mode="after")
    @classmethod
    def _strip_and_require(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Conteúdo da observação não pode ser vazio.")
        return stripped


class ObservationCard(BaseModel):
    """Um card da central de Observações — representa, sem distinção de
    schema, uma observação GERAL (`ClientObservation`), a observação da
    VISITA (`Appointment.notes`) ou a da COMANDA (`Order.observation`).
    `id` é sempre o id da ENTIDADE DE ORIGEM (o id de `ClientObservation`
    para `origin="general"`, o `Appointment.id` para `"appointment"`, o
    `Order.id` para `"order"`) — nunca um id sintético, pra permitir
    editar/auditar direto contra a origem.

    `order_number`/`appointment_starts_at` vêm preenchidos só para a
    origem correspondente — o frontend monta o rótulo ("Comanda #540",
    "Agendamento de 24/11/2026 às 14:00") com o MESMO padrão de
    formatação de data já usado no resto do app, nunca uma string
    pré-formatada pelo backend (evita duplicar/discordar do fuso da
    organização).

    `created_at`/`created_by_name`/`updated_at`/`updated_by_name` podem
    vir `None` — "Data de criação não registrada"/"Autor não registrado"
    (item explícito do pedido) são estados LEGÍTIMOS, nunca erro."""

    id: uuid.UUID
    origin: ObservationOrigin
    order_number: int | None = None
    appointment_starts_at: datetime | None = None
    # Id do Appointment por trás do card — sempre == `id` quando
    # `origin="appointment"`; resolvido via `Order.appointment_id`
    # quando `origin="order"` (abre o MESMO drawer de agendamento que
    # "Ver comanda" já abre hoje no resto do app, ver `OrderDrawer` —
    # ele é indexado por `appointment_id`, nunca por `order.id`
    # diretamente). `None` para `origin="general"` (não existe
    # agendamento/comanda de origem).
    appointment_id: uuid.UUID | None = None
    content: str
    version: int
    created_at: datetime | None
    created_by_name: str | None
    updated_at: datetime | None
    updated_by_name: str | None
    is_legacy_import: bool
    can_edit: bool
    can_view_history: bool


class ObservationListResponse(BaseModel):
    items: list[ObservationCard]
    total: int
    page: int
    page_size: int


class ObservationHistoryEntry(BaseModel):
    """Uma entrada da linha do tempo de um `AuditLog` (ver
    `services/client_observations.py::get_observation_history`) — nunca
    inventa descrição: `action`/`user_name`/conteúdo vêm exatamente do
    que foi gravado na mesma transação da alteração."""

    action: Literal["create", "update"]
    at: datetime
    user_name: str | None
    old_content: str | None
    new_content: str | None


class ObservationHistoryResponse(BaseModel):
    items: list[ObservationHistoryEntry]
