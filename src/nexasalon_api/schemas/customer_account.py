"""Schemas da Conta da Cliente (Etapa L, Blocos 5/6/9/10) —
`api/v1/customer_auth.py`. Deliberadamente enxutos: nunca pedem
CPF/endereço/gênero/nascimento (Bloco 5, item explícito do pedido) —
esse cadastro é só identidade + contato, o cadastro completo continua
sendo o `Client` de cada organização, resolvido/vinculado à parte
(Bloco 8)."""
import uuid
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

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
    tem motivo de ver, nunca preço/comissão/nota interna.

    Etapa N5 — `can_cancel`/`can_reschedule` já vêm PRONTOS do backend
    (mesma regra usada por `cancel_by_customer`/`reschedule_by_customer`
    — nunca uma segunda interpretação no frontend, "não hardcode 24 na
    UI"). `cancel_lead_time_blocked`/`reschedule_lead_time_blocked` só
    ficam `true` quando a ÚNICA razão de `can_*` ser `false` é a janela
    de antecedência (não a configuração desligada nem o status) — é o
    sinal pro frontend mostrar um botão desabilitado COM explicação em
    vez de simplesmente esconder a ação (item explícito do pedido)."""

    id: uuid.UUID
    organization_name: str
    service_name: str
    professional_name: str
    starts_at: datetime | None
    ends_at: datetime | None
    status: AppointmentStatus
    can_cancel: bool
    can_reschedule: bool
    cancel_lead_time_blocked: bool
    reschedule_lead_time_blocked: bool
    change_min_hours: int


class PublicAppointmentCancelRequest(BaseModel):
    """Motivo é OPCIONAL (item explícito "sem criar complexidade
    excessiva") — só vai pro `AuditLog`, nunca bloqueia o cancelamento
    se ausente."""

    reason: str | None = Field(default=None, max_length=500)


class PublicAppointmentRescheduleRequest(BaseModel):
    """Só troca DATA/HORÁRIO (item explícito do pedido) — serviço,
    profissional, duração e preço nunca fazem parte deste payload."""

    start_at: datetime

    @field_validator("start_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_at deve incluir informação de fuso horário (ISO 8601 com offset).")
        return value
