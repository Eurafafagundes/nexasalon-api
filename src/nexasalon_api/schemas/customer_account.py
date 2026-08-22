"""Schemas da Conta da Cliente (Etapa L, Blocos 5/6/9/10) —
`api/v1/customer_auth.py`. Deliberadamente enxutos: nunca pedem
CPF/endereço/gênero/nascimento (Bloco 5, item explícito do pedido) —
esse cadastro é só identidade + contato, o cadastro completo continua
sendo o `Client` de cada organização, resolvido/vinculado à parte
(Bloco 8)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from nexasalon_api.core.normalize import normalize_phone
from nexasalon_api.models.enums import AppointmentStatus


class CustomerRegisterRequest(BaseModel):
    """Bloco 5 — cadastro manual: só nome, WhatsApp, e-mail, senha."""

    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str = Field(min_length=8, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    password_confirm: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def _check_password_confirm(self) -> "CustomerRegisterRequest":
        if self.password != self.password_confirm:
            raise ValueError("As senhas informadas não coincidem.")
        return self

    def normalized_phone(self) -> str:
        return normalize_phone(self.phone)


class CustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class CustomerGoogleLoginRequest(BaseModel):
    """Bloco 6 — o frontend usa o Google Identity Services no navegador
    e manda só o ID TOKEN resultante; o backend nunca vê senha do
    Google, nunca troca authorization code, só VERIFICA a assinatura e
    o `aud` desse token (ver `services/google_oauth.py`)."""

    id_token: str = Field(min_length=1)


class CustomerUpdateMeRequest(BaseModel):
    """Bloco 6 — "pedir WhatsApp se necessário" depois do login via
    Google (que não fornece telefone). Só o telefone é editável aqui;
    nome/e-mail vêm da identidade verificada (Google) ou do cadastro
    manual e não são o foco desta etapa."""

    phone: str = Field(min_length=8, max_length=32)

    def normalized_phone(self) -> str:
        return normalize_phone(self.phone)


class CustomerAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    phone: str | None
    email_verified_at: datetime | None


class CustomerAuthResult(BaseModel):
    """Resposta de register/login/google — token pronto pra uso
    imediato (sessão da cliente, ver `core/security.py::
    create_customer_access_token`). `phone_required=True` sinaliza pro
    frontend exibir o passo "informe seu WhatsApp" antes de seguir pra
    confirmação do agendamento (caso comum: primeiro login via Google)."""

    access_token: str
    customer: CustomerAccountRead
    phone_required: bool


class PublicMyAppointmentRead(BaseModel):
    """Bloco 10 — "Meus agendamentos". Schema PRÓPRIO (não reaproveita
    `AppointmentRead` interno) — mesma disciplina de segurança do resto
    do Agendamento Online público: só os campos que a própria cliente
    tem motivo de ver, nunca preço/comissão/nota interna."""

    id: uuid.UUID
    organization_name: str
    service_name: str
    professional_name: str
    starts_at: datetime | None
    ends_at: datetime | None
    status: AppointmentStatus
