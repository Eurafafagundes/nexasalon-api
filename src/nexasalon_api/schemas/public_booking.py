"""Schemas do Agendamento Online público (Etapa K) — endpoints
SEM autenticação (`api/v1/public_booking.py`). Cada schema aqui é
deliberadamente MAIS ENXUTO que o equivalente interno (`OrganizationRead`,
`ServiceRead`, `ProfessionalRead`, `AppointmentRead`): item de segurança
explícito do pedido — "endpoints públicos devem expor somente dados
necessários para agendamento", "nunca expor dados internos, financeiro,
estoque, usuários, permissões ou informações privadas dos profissionais".
Nenhum destes schemas usa `from_attributes=True` direto num model que
tenha campo sensível sem, antes, revisar CADA campo exposto — só os
campos explicitamente listados abaixo saem da API.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from nexasalon_api.models.enums import AppointmentStatus


class PublicOrganizationRead(BaseModel):
    """Só o suficiente pra montar o cabeçalho da página pública (logo,
    nome) — nunca CNPJ, endereço, contato interno, configurações."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    logo_url: str | None
    timezone: str


class PublicServiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    default_duration_minutes: int
    default_price: Decimal


class PublicProfessionalRead(BaseModel):
    """Nunca `phone`/`professional_email` (dado privado do
    profissional, item explícito do pedido de segurança) — só o
    suficiente pra escolher: nome e foto."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    photo_url: str | None


class PublicAvailabilitySlotRead(BaseModel):
    start_at: datetime
    end_at: datetime


class PublicBookingCreate(BaseModel):
    """`professional_id=None` = "Qualquer profissional" (item explícito
    do pedido) — resolvido no backend, nunca escolhido às cegas pelo
    cliente. `force_overlap` de propósito NÃO EXISTE neste schema — o
    fluxo público nunca oferece encaixe forçado.

    Etapa L, Bloco 4/5 — evolução do fluxo: "Dados" virou "Identificação"
    (login/conta obrigatórios, ver `api/v1/public_booking.py::create_booking`,
    que agora exige `get_current_customer`). Os dados da cliente
    (nome/telefone/e-mail) NÃO vêm mais neste payload — vêm da
    `CustomerAccount` autenticada, resolvida/vinculada ao `Client` da
    organização por `services/customer_accounts.py` (Bloco 8)."""

    service_id: uuid.UUID
    professional_id: uuid.UUID | None = None
    start_at: datetime

    @field_validator("start_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_at deve incluir informação de fuso horário (ISO 8601 com offset).")
        return value


class PublicBookingRead(BaseModel):
    """Confirmação — só o essencial pra cliente conferir o que reservou;
    nunca preço/comissão/dado financeiro (mesmo item de segurança do
    módulo)."""

    id: uuid.UUID
    status: AppointmentStatus
    starts_at: datetime | None
    ends_at: datetime | None
