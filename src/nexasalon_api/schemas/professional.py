import uuid
from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.enums import CommissionType


class ProfessionalBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    branch_id: uuid.UUID | None = None
    photo_url: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=32)
    professional_email: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=120)
    agenda_color: str = Field(default="#8B5CF6", pattern=r"^#[0-9A-Fa-f]{6}$")

    # Configuração de Agenda — controla exclusivamente COMO (e se) este
    # profissional aparece na Agenda/agendamento público. Um profissional
    # pode existir sem agenda própria (`has_schedule=False`, ex.: gerente).
    has_schedule: bool = True
    show_on_main_schedule: bool = True
    allow_online_booking: bool = True
    display_order: int = Field(default=0, ge=0, le=32767)


class ProfessionalCreate(ProfessionalBase):
    pass


class ProfessionalUpdate(ProfessionalBase):
    pass


class ProfessionalRead(ProfessionalBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WorkingHourItem(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0=domingo … 6=sábado")
    start_time: time
    end_time: time
    is_active: bool = True

    @model_validator(mode="after")
    def _check_order(self) -> "WorkingHourItem":
        if self.start_time >= self.end_time:
            raise ValueError("start_time deve ser menor que end_time")
        return self


class WorkingHourRead(WorkingHourItem):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    professional_id: uuid.UUID


class WorkingHoursReplaceRequest(BaseModel):
    items: list[WorkingHourItem] = Field(default_factory=list)


class ProfessionalServiceItem(BaseModel):
    service_id: uuid.UUID
    is_active: bool = True
    # Etapa "Disponibilidade por profissional x serviço no Agendamento
    # Online" — independente de `is_active` ("realiza o serviço"): `false`
    # só tira esta combinação do fluxo público, a agenda interna nunca lê
    # este campo. Nunca `true` quando `is_active=false` (ver
    # `_check_online_booking` abaixo).
    allow_online_booking: bool = True
    duration_override_minutes: int | None = Field(default=None, gt=0)
    price_override: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    commission_type: CommissionType | None = None
    # `gt=0` (não `ge=0`) — Etapa C1: comissão zero não faz sentido nem
    # como percentual nem como valor fixo (ausência de comissão já é
    # representada por `commission_type=None`, nunca por um valor 0).
    commission_value: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def _check_commission(self) -> "ProfessionalServiceItem":
        if self.commission_type == CommissionType.PERCENTAGE and self.commission_value is not None:
            if self.commission_value > 100:
                raise ValueError("commission_value percentual não pode passar de 100")
        if (self.commission_type is None) != (self.commission_value is None):
            raise ValueError("commission_type e commission_value devem ser preenchidos juntos")
        return self

    @model_validator(mode="after")
    def _check_online_booking(self) -> "ProfessionalServiceItem":
        # Etapa "Disponibilidade por profissional x serviço no Agendamento
        # Online" — reforço no Pydantic da mesma regra do CHECK em
        # `models/service.py` (`online_booking_requires_active_service`):
        # um vínculo que o profissional não realiza mais nunca pode ficar
        # marcado como disponível online.
        if not self.is_active and self.allow_online_booking:
            raise ValueError(
                "allow_online_booking não pode ser true quando is_active é false "
                "(profissional não realiza este serviço)."
            )
        return self


class ProfessionalServiceRead(BaseModel):
    """NÃO herda de `ProfessionalServiceItem` de propósito (bug real
    corrigido). `ProfessionalServiceItem` carrega validação de ESCRITA
    (`gt=0`, `ge=0`, `max_digits`/`decimal_places`, e o
    `@model_validator` que exige `commission_type`/`commission_value`
    preenchidos juntos) — correta para aceitar/rejeitar um payload novo,
    mas nada no banco IMPEDE uma linha antiga violar essas mesmas regras
    (`commission_type`/`commission_value` são colunas independentes, sem
    CHECK constraint). Como `ProfessionalServiceRead` reaproveitava o
    mesmo model_validator via herança, `GET /professionals/{id}/services`
    quebrava (erro de validação Pydantic) ao tentar servializar QUALQUER
    linha nessa condição — o profissional aparecia com "não foi possível
    carregar os serviços" mesmo tendo vínculos reais, porque um único
    registro inconsistente derrubava a lista inteira. Leitura precisa ser
    tolerante a dado histórico; só a escrita (`ProfessionalServiceItem`,
    usado em `ProfessionalServicesReplaceRequest`) deve continuar
    estrita — isso não muda aqui."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    professional_id: uuid.UUID
    service_id: uuid.UUID
    is_active: bool
    allow_online_booking: bool = False
    duration_override_minutes: int | None = None
    price_override: Decimal | None = None
    commission_type: CommissionType | None = None
    commission_value: Decimal | None = None


class ProfessionalServicesReplaceRequest(BaseModel):
    items: list[ProfessionalServiceItem] = Field(default_factory=list)
