"""Gestão administrativa de usuários/memberships dentro de UMA
organização — quem chama já é um `ActorContext` com `users.manage`
verificado pela rota (via `require_permission`). `organization_id` aqui
SEMPRE vem do contexto autenticado, nunca de parâmetro de URL/body cru —
mesma regra usada na Etapa 2C."""
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.core.security import create_invite_token
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership, User
from nexasalon_api.repositories import membership_repo, professional_repo, rbac_repo, user_repo


def _get_membership_in_org(
    session: Session, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> OrganizationMembership:
    membership = membership_repo.get(session, membership_id)
    # Checagem explícita de organization_id, não só RLS: mesma defesa em
    # profundidade adotada depois da revisão de segurança da Etapa 2C
    # (não confiar só num filtro implícito/indireto).
    if membership is None or membership.organization_id != organization_id:
        raise NotFoundError("Membership não encontrada.")
    return membership


def _validate_role_for_org(session: Session, organization_id: uuid.UUID, role_id: uuid.UUID):
    role = rbac_repo.get_role(session, role_id)
    if role is None or (role.organization_id is not None and role.organization_id != organization_id):
        raise NotFoundError("Role não encontrada para esta organização.")
    return role


def list_employees(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[OrganizationMembership]:
    return membership_repo.list_for_organization(session, organization_id, include_inactive)


@dataclass(frozen=True)
class EmployeeInviteResult:
    membership: OrganizationMembership
    # Só preenchido quando a membership entra como INVITED — o
    # administrador nunca vê/define a senha do funcionário; ele só
    # recebe este link/token pra repassar (WhatsApp, e-mail manual etc.
    # — envio automático de e-mail é etapa futura). Quando o usuário já
    # tem senha própria (`invite_token is None`), a membership já entra
    # ACTIVE e não existe convite a aceitar.
    invite_token: str | None


def add_or_invite_employee(
    session: Session,
    organization_id: uuid.UUID,
    *,
    email: str,
    name: str,
    role_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> EmployeeInviteResult:
    """Fluxo de convite (o dono NUNCA cria/conhece a senha do
    funcionário):

        User/Membership INVITED -> invite_token temporário (JWT)
            -> funcionário abre o link, define a própria senha
            -> POST /auth/accept-invite -> membership vira ACTIVE

    A decisão de INVITED vs ACTIVE é sobre o `User`, não sobre "já
    existia na base": um `User` pode existir (criado por um convite
    anterior noutra organização) e AINDA não ter senha — nesse caso essa
    membership também precisa de convite próprio, não pode entrar ACTIVE
    direto (bug corrigido nesta revisão: antes bastava a linha existir).

    - `user.password_hash is None` (novo ou ainda sem senha definida)
      -> membership INVITED + gera `invite_token` novo para ESTA
      membership especificamente (cada organização convida separado,
      como no Slack: aceitar o convite da org A não ativa a da org B).
    - `user.password_hash` já definido -> a pessoa já tem credenciais
      próprias; a nova membership entra ACTIVE direto, sem convite.
    """
    _validate_role_for_org(session, organization_id, role_id)

    user = user_repo.get_by_email(session, email)
    if user is None:
        user = user_repo.create(session, email=email, name=name, password_hash=None)

    existing = membership_repo.get_by_user_and_org(session, user.id, organization_id)
    if existing is not None:
        raise ConflictError("Este usuário já possui uma membership nesta organização.")

    status = MembershipStatus.ACTIVE if user.password_hash is not None else MembershipStatus.INVITED

    membership = membership_repo.create(
        session,
        user_id=user.id,
        organization_id=organization_id,
        role_id=role_id,
        branch_id=branch_id,
        status=status,
    )

    invite_token = None
    if status == MembershipStatus.INVITED:
        invite_token = create_invite_token(user_id=user.id, membership_id=membership.id)

    return EmployeeInviteResult(membership=membership, invite_token=invite_token)


def resend_invite(
    session: Session, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> str:
    """Gera um novo `invite_token` para uma membership ainda INVITED —
    o anterior simplesmente expira sozinho (JWT sem estado no banco,
    nada pra revogar explicitamente)."""
    membership = _get_membership_in_org(session, organization_id, membership_id)
    if membership.status != MembershipStatus.INVITED:
        raise ConflictError("Esta membership não está mais aguardando convite.")
    return create_invite_token(user_id=membership.user_id, membership_id=membership.id)


def set_membership_status(
    session: Session,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    status: MembershipStatus,
) -> OrganizationMembership:
    """Ativar/desativar. Ao mudar para não-ACTIVE, o corte de acesso é
    imediato: `refresh()` e o dependency de `get_current_actor` (Etapa
    seguinte) sempre reconferem o status da membership no banco a cada
    requisição — nunca a partir de um valor cacheado no token."""
    membership = _get_membership_in_org(session, organization_id, membership_id)
    membership.status = status
    return membership_repo.save(session, membership)


def assign_role(
    session: Session, organization_id: uuid.UUID, membership_id: uuid.UUID, role_id: uuid.UUID
) -> OrganizationMembership:
    membership = _get_membership_in_org(session, organization_id, membership_id)
    _validate_role_for_org(session, organization_id, role_id)
    membership.role_id = role_id
    return membership_repo.save(session, membership)


def link_professional(
    session: Session,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    professional_id: uuid.UUID,
) -> OrganizationMembership:
    """Vincula o User da membership a um Professional já existente na
    MESMA organização — a FK canônica continua sendo
    `Professional.user_id` (ajuste aprovado na Etapa 2A/2B); esta
    membership não ganha coluna própria de professional_id."""
    membership = _get_membership_in_org(session, organization_id, membership_id)

    professional = professional_repo.get(session, organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado nesta organização.")

    if professional.user_id is not None and professional.user_id != membership.user_id:
        raise ConflictError("Este profissional já está vinculado a outro usuário.")

    professional.user_id = membership.user_id
    professional_repo.save(session, professional)
    return membership


def get_user(session: Session, user_id: uuid.UUID) -> User:
    user = user_repo.get(session, user_id)
    if user is None:
        raise NotFoundError("Usuário não encontrado.")
    return user
