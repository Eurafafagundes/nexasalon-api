import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Etapa I, item "Alteração de status" — quando existem outros
    # agendamentos relacionados (mesma cliente, mesmo dia), o frontend
    # oferece "Somente este atendimento" (padrão) vs. "Todos os
    # atendimentos de [cliente] hoje". Nunca pergunta quando só existe
    # um atendimento (ver `GET /appointments/{id}/related`).
    scope: Literal["only_this", "all_related"] = "only_this"


class AppointmentItemUpdate(BaseModel):
    """PATCH parcial de UM item já existente (Etapa F — item "editar
    valor e duração no agendamento" + "drag and drop"). Diferente do PUT
    `AppointmentReplace` (que apaga e recria TODOS os itens), esta edita
    o `AppointmentItem` EM PLACE — necessário porque, uma vez que existe
    uma Comanda aberta linkada (`OrderItem.appointment_item_id`,
    `ondelete=RESTRICT`), apagar o item quebraria essa referência.

    Cada campo é opcional e `None` significa "não mexer neste campo"
    (diferente de `AppointmentItemCreate.price_override`, onde `None`
    significa "usar o preço do catálogo" — schemas diferentes de
    propósito, para não colidir as duas semânticas). Serve tanto pra:

      - editar preço/duração no drawer da Agenda (`price_override`
        e/ou `duration_override`, com `reason` obrigatório quando o
        preço muda de fato — `services/appointments.py::
        update_appointment_item` valida isso, nunca só o frontend);
      - mover pelo drag-and-drop (`professional_id` e/ou `start_at`).

    Pelo menos um campo precisa vir preenchido."""

    professional_id: uuid.UUID | None = None
    start_at: datetime | None = None
    price_override: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    duration_override: int | None = Field(default=None, gt=0, le=1440)
    reason: str | None = Field(default=None, max_length=255)
    force_overlap: bool = False

    @field_validator("start_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("start_at deve incluir informação de fuso horário (ISO 8601 com offset).")
        return value

    @model_validator(mode="after")
    def _check_at_least_one_field(self) -> "AppointmentItemUpdate":
        if all(
            f is None
            for f in (self.professional_id, self.start_at, self.price_override, self.duration_override)
        ):
            raise ValueError(
                "Informe ao menos um campo para editar (professional_id, start_at, "
                "price_override ou duration_override)."
            )
        return self


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


class AppointmentRelatedRead(BaseModel):
    """`GET /appointments/{id}/related` (Etapa I, item "Alteração de
    status") — `related` nunca inclui o próprio agendamento consultado.
    O frontend só oferece o diálogo "Somente este / Todos os
    atendimentos de X hoje" quando `related` não está vazio."""

    model_config = ConfigDict(from_attributes=False)

    client_name: str
    related: list[AppointmentRead]
