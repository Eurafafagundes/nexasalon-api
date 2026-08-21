import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from nexasalon_api.models.enums import MembershipStatus


class EmployeeInviteRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    role_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    # Etapa G — fluxo PRINCIPAL passa a ser o admin definir a senha
    # inicial diretamente aqui (nunca retornada/logada, só o hash é
    # persistido — ver `core/security.hash_password`). Continua opcional
    # a nível de schema/API só pra não quebrar o fluxo antigo de convite
    # por link/token (ainda suportado no backend, só não é mais usado
    # pelo frontend principal — ver `add_or_invite_employee`).
    password: str | None = Field(default=None, min_length=8)


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
    quando a membership entrou `INVITED` (fluxo antigo, sem senha
    informada). `credential_mode` diz ao frontend qual dos 3 caminhos
    aconteceu, pra mostrar a mensagem certa — nunca inclui senha/hash:

      - "password_set": o admin informou `password` e ela foi aplicada
        (usuário novo, sem credencial anterior) — acesso já fica ACTIVE.
      - "existing_account_linked": o e-mail já pertencia a um usuário
        COM senha própria (de outra organização) — a membership entra
        ACTIVE reaproveitando a credencial existente; qualquer `password`
        informada aqui é ignorada de propósito (nunca sobrescreve a
        senha de login de outra organização silenciosamente).
      - "invite_link": nenhuma senha foi informada — cai no fluxo antigo
        de convite por link/token (mantido no backend por compatibilidade,
        não é mais o caminho usado pela UX principal)."""

    membership: MembershipRead
    invite_token: str | None
    credential_mode: str


class ResendInviteResponse(BaseModel):
    invite_token: str


class ResetPasswordLinkResponse(BaseModel):
    """Resposta de `POST /users/{id}/reset-password` — fluxo antigo por
    link de uso único (mantido no backend por compatibilidade). A UX
    principal agora usa `PATCH /users/{id}/set-password`
    (`SetPasswordRequest`/`admin_set_password`), onde o administrador
    define a nova senha diretamente."""

    reset_token: str


class SetPasswordRequest(BaseModel):
    """Etapa G — o proprietário/admin define a nova senha do funcionário
    DIRETAMENTE (substitui o fluxo principal por link/token). O backend
    só persiste o hash (`core/security.hash_password`); a senha nunca é
    logada nem retornada em nenhuma resposta. Serve tanto pra:

      - redefinir a senha de uma membership já ACTIVE/SUSPENDED (a senha
        antiga para de funcionar imediatamente — o hash é substituído,
        não há como as duas coexistirem);
      - ativar diretamente uma membership ainda INVITED, sem precisar do
        convite/token (`admin_set_password` também vira o status pra
        ACTIVE nesse caso)."""

    password: str = Field(min_length=8)
