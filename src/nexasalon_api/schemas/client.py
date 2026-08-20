import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexasalon_api.schemas.order import OrderRead

from nexasalon_api.core.normalize import is_valid_cpf, normalize_cpf, normalize_phone
from nexasalon_api.models.enums import BrazilianState, ClientGender


class ClientBase(BaseModel):
    """Etapa C.1 — cadastro completo. `gender`/endereço (`cep`...
    `address_number`/`complement`) e `cpf` são TODOS opcionais (nunca
    requisito de cadastro nem de agendamento) — por isso o MESMO schema
    já serve tanto o cadastro rápido da Agenda (que só preenche
    `name`/`phone`, ver `components/agenda/client-picker.tsx` no
    frontend) quanto o cadastro completo de Clientes > Novo/Editar, sem
    precisar de um segundo schema "reduzido": campos não enviados
    simplesmente ficam `None`."""

    name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    whatsapp: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    birth_date: date | None = None
    gender: ClientGender | None = None
    notes: str | None = None
    # Todos opcionais de propósito (item "não torne CPF nem endereço
    # completo obrigatórios") — cadastro universal, não específico de
    # nenhum tipo de negócio de beleza.
    cpf: str | None = Field(default=None, max_length=14)
    cep: str | None = Field(default=None, max_length=9)
    state: BrazilianState | None = None
    city: str | None = Field(default=None, max_length=120)
    neighborhood: str | None = Field(default=None, max_length=120)
    address_line: str | None = Field(default=None, max_length=255)
    address_number: str | None = Field(default=None, max_length=20)
    complement: str | None = Field(default=None, max_length=120)

    @field_validator("phone", "whatsapp", mode="after")
    @classmethod
    def _normalize_phone(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_phone(value)

    @field_validator("cpf", mode="after")
    @classmethod
    def _normalize_and_validate_cpf(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = normalize_cpf(value)
        if not is_valid_cpf(digits):
            raise ValueError("CPF inválido.")
        return digits

    @field_validator("cep", mode="after")
    @classmethod
    def _normalize_cep(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = "".join(c for c in value if c.isdigit())
        return digits or None


class ClientCreate(ClientBase):
    pass


class ClientUpdate(ClientBase):
    pass


class ClientRead(ClientBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ClientHistory(BaseModel):
    """Resumo (item "Resumo | Histórico | Observações") — tudo derivado
    de `Order`, nunca gravado no `Client` (ver
    `services/clients.py::get_client_history`)."""

    client_since: datetime
    visits_count: int
    total_spent: Decimal
    orders: list[OrderRead]
