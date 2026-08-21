"""Schemas de Organization — leitura (item 3 da Etapa D: "Informações do
Estabelecimento") e escrita (`OrganizationUpdate`, gated por
`organization.manage` na rota — ver `api/v1/organizations.py`).

`document` é reusado como CNPJ nesta etapa (ver docstring de
`models/organization.py::Organization` pro raciocínio completo de
reuso vs. campo novo) — por isso normalizado/validado aqui com o MESMO
padrão já usado pra CPF em `schemas/client.py`: nunca confiar só na
máscara do frontend."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexasalon_api.core.normalize import is_valid_cnpj, normalize_cnpj, normalize_slug
from nexasalon_api.models.enums import BrazilianState, OrganizationStatus


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    document: str | None
    online_booking_enabled: bool
    online_booking_auto_confirm: bool
    online_booking_min_lead_minutes: int
    online_booking_max_lead_days: int
    legal_name: str | None
    logo_url: str | None
    email: str | None
    phone: str | None
    whatsapp: str | None
    instagram: str | None
    website: str | None
    timezone: str
    status: OrganizationStatus
    business_type: str | None
    cep: str | None
    state: BrazilianState | None
    city: str | None
    neighborhood: str | None
    address_line: str | None
    address_number: str | None
    complement: str | None
    created_at: datetime
    updated_at: datetime


class OrganizationUpdate(BaseModel):
    """Todos os campos são opcionais (nunca obrigatórios pro salão
    operar — item explícito "não impedir operação... porque CNPJ/
    endereço não foi preenchido"). Diferente de `ClientUpdate` (que é
    full-replace): aqui um campo AUSENTE do payload mantém o valor
    atual (`exclude_unset` no service — ver
    `services/organizations.py::update_organization`), porque
    `Organization.timezone` é NOT NULL com um default de negócio — um
    payload parcial (ex.: só `{"name": "..."}`) não pode zerar essa
    coluna. Um campo enviado explicitamente como `null` limpa o valor
    (ex.: remover um CNPJ cadastrado por engano)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    # Slug da URL pública (Etapa K — Agendamento Online): `None` = campo
    # não enviado, mantém o valor atual (mesma semântica `exclude_unset`
    # do resto deste schema). Diferente do resto: `slug` NUNCA pode ser
    # limpo pra `null` (é NOT NULL no banco) — `services/organizations.py`
    # recusa esse caso explicitamente antes de chegar no banco.
    slug: str | None = Field(default=None, min_length=1, max_length=120)
    online_booking_enabled: bool | None = None
    online_booking_auto_confirm: bool | None = None
    online_booking_min_lead_minutes: int | None = Field(default=None, ge=0, le=10080)
    online_booking_max_lead_days: int | None = Field(default=None, ge=1, le=3650)
    legal_name: str | None = Field(default=None, max_length=255)
    document: str | None = Field(default=None, max_length=18)
    business_type: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    whatsapp: str | None = Field(default=None, max_length=32)
    instagram: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)
    cep: str | None = Field(default=None, max_length=9)
    state: BrazilianState | None = None
    city: str | None = Field(default=None, max_length=120)
    neighborhood: str | None = Field(default=None, max_length=120)
    address_line: str | None = Field(default=None, max_length=255)
    address_number: str | None = Field(default=None, max_length=20)
    complement: str | None = Field(default=None, max_length=120)

    @field_validator("slug", mode="after")
    @classmethod
    def _normalize_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_slug(value)
        if not normalized:
            raise ValueError("Slug inválido — use letras, números e hífen.")
        return normalized

    @field_validator("document", mode="after")
    @classmethod
    def _normalize_and_validate_document(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = normalize_cnpj(value)
        if not is_valid_cnpj(digits):
            raise ValueError("CNPJ inválido.")
        return digits

    @field_validator("phone", "whatsapp", mode="after")
    @classmethod
    def _strip_empty(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("cep", mode="after")
    @classmethod
    def _normalize_cep(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = "".join(c for c in value if c.isdigit())
        return digits or None


class LogoUploadRead(BaseModel):
    logo_url: str
