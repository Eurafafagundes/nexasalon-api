import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class OrganizationChoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    organization_name: str
    membership_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str


class TokenPairRead(BaseModel):
    """Só o access_token vai no corpo — o refresh token nunca aparece
    aqui, é entregue como cookie HttpOnly (ver `api/v1/auth.py`).
    Frontend não deve tentar ler/guardar refresh token em JS."""

    access_token: str
    token_type: str = "bearer"
    organization_id: uuid.UUID
    membership_id: uuid.UUID


class LoginResponse(BaseModel):
    """`status == "session"`: login concluído, `tokens` preenchido —
    frontend já pode navegar pra dentro do sistema.
    `status == "select_organization"`: usuário tem mais de uma empresa
    ativa; frontend deve exibir `organizations` e chamar
    `/auth/select-organization` com `org_selection_token` e a organização
    escolhida."""

    status: Literal["session", "select_organization"]
    tokens: TokenPairRead | None = None
    org_selection_token: str | None = None
    organizations: list[OrganizationChoiceRead] | None = None


class SelectOrganizationRequest(BaseModel):
    org_selection_token: str
    organization_id: uuid.UUID


class AcceptInviteRequest(BaseModel):
    invite_token: str
    password: str = Field(min_length=8)


class ResetPasswordRequest(BaseModel):
    """Consumo do link gerado por `POST /users/{id}/reset-password`
    (admin) — ver `services/auth.py::reset_password`."""

    reset_token: str
    password: str = Field(min_length=8)


# Não existem mais `RefreshRequest`/`LogoutRequest`: refresh/logout leem o
# refresh token do cookie HttpOnly (`Request.cookies`), nunca de um campo
# de body — o objetivo do cookie HttpOnly é justamente o token nunca
# passar pelo JS do frontend, então ele também não deveria "ir e voltar"
# como texto num JSON.


class CurrentUserRead(BaseModel):
    id: uuid.UUID
    email: str
    name: str


class CurrentOrganizationRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class CurrentMembershipRead(BaseModel):
    id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    professional_id: uuid.UUID | None


class MeResponse(BaseModel):
    """Formato pensado para o frontend montar menus por permissão e o
    seletor de empresas sem precisar de outra chamada: `organizations`
    traz TODAS as memberships ativas do usuário (inclusive a atual)."""

    user: CurrentUserRead
    organization: CurrentOrganizationRead
    membership: CurrentMembershipRead
    permissions: list[str]
    organizations: list[OrganizationChoiceRead]
