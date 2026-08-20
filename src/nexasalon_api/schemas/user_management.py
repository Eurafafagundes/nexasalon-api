import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from nexasalon_api.models.enums import MembershipStatus


class EmployeeInviteRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    role_id: uuid.UUID
    branch_id: uuid.UUID | None = None


class AssignRoleRequest(BaseModel):
    role_id: uuid.UUID


class LinkProfessionalRequest(BaseModel):
    professional_id: uuid.UUID


class MembershipRead(BaseModel):
    """Montado manualmente no service/rota (não via `from_attributes`
    direto na membership) porque `user_email`/`user_name`/`role_name`
    vêm de relationships (`User`, `Role`), não de colunas próprias."""

    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    user_name: str
    organization_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    branch_id: uuid.UUID | None
    status: MembershipStatus
    professional_id: uuid.UUID | None
    # "Último acesso" da tela Configurações > Acessos — `None` quando o
    # usuário nunca fez login (ex.: convite ainda pendente).
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EmployeeInviteResponse(BaseModel):
    """Resposta de `POST /users`. `invite_token` só vem preenchido
    quando a membership entrou `INVITED` — é o link que o administrador
    repassa manualmente ao funcionário (envio automático de e-mail é
    etapa futura); o próprio administrador nunca vê a senha."""

    membership: MembershipRead
    invite_token: str | None


class ResendInviteResponse(BaseModel):
    invite_token: str


class ResetPasswordLinkResponse(BaseModel):
    """Resposta de `POST /users/{id}/reset-password` — mesmo padrão do
    convite: um link de uso único que o administrador repassa, nunca uma
    senha. Ver `core/security.py::create_password_reset_token`."""

    reset_token: str
