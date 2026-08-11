import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership


def get(session: Session, membership_id: uuid.UUID) -> OrganizationMembership | None:
    return session.get(OrganizationMembership, membership_id)


def get_by_user_and_org(
    session: Session, user_id: uuid.UUID, organization_id: uuid.UUID
) -> OrganizationMembership | None:
    stmt = select(OrganizationMembership).where(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.organization_id == organization_id,
    )
    return session.scalars(stmt).first()


def list_active_for_user(session: Session, user_id: uuid.UUID) -> list[OrganizationMembership]:
    """Lista memberships ATIVAS do usuário em QUALQUER organização —
    usado no fluxo de login para decidir entre entrar direto (1 org) ou
    pedir seleção (múltiplas orgs).

    Só retorna linhas de verdade se a sessão tiver `app.current_user_id`
    setado (via `set_config`, feito pelo service de auth antes de chamar
    esta função) — a policy `tenant_isolation` de `organization_memberships`
    (migration 0006) usa essa variável como cláusula de auto-acesso,
    exatamente para viabilizar esta query sem depender de
    `app.current_org_id` (que ainda não existe nesse ponto do login)."""
    stmt = select(OrganizationMembership).where(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.status == MembershipStatus.ACTIVE,
    )
    return list(session.scalars(stmt).all())


def list_for_organization(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[OrganizationMembership]:
    stmt = select(OrganizationMembership).where(
        OrganizationMembership.organization_id == organization_id
    )
    if not include_inactive:
        stmt = stmt.where(OrganizationMembership.status == MembershipStatus.ACTIVE)
    return list(session.scalars(stmt.order_by(OrganizationMembership.created_at)).all())


def create(
    session: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    status: MembershipStatus = MembershipStatus.INVITED,
) -> OrganizationMembership:
    membership = OrganizationMembership(
        user_id=user_id,
        organization_id=organization_id,
        role_id=role_id,
        branch_id=branch_id,
        status=status,
    )
    session.add(membership)
    session.flush()
    return membership


def save(session: Session, membership: OrganizationMembership) -> OrganizationMembership:
    session.flush()
    return membership
