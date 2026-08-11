import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.identity import MembershipPermissionOverride
from nexasalon_api.models.rbac import Permission, Role, RolePermission


def get_role(session: Session, role_id: uuid.UUID) -> Role | None:
    return session.get(Role, role_id)


def get_system_role_by_name(session: Session, name: str) -> Role | None:
    stmt = select(Role).where(Role.name == name, Role.organization_id.is_(None))
    return session.scalars(stmt).first()


def list_roles_available(session: Session, organization_id: uuid.UUID) -> list[Role]:
    """Roles de sistema (organization_id NULL) + roles customizadas da
    própria org — mesma regra da policy RLS de `roles` (migration 0003)."""
    stmt = (
        select(Role)
        .where((Role.organization_id.is_(None)) | (Role.organization_id == organization_id))
        .order_by(Role.is_system.desc(), Role.name)
    )
    return list(session.scalars(stmt).all())


def list_role_permission_keys(session: Session, role_id: uuid.UUID) -> set[str]:
    stmt = select(RolePermission.permission_key).where(RolePermission.role_id == role_id)
    return set(session.scalars(stmt).all())


def list_overrides(session: Session, membership_id: uuid.UUID) -> list[MembershipPermissionOverride]:
    stmt = select(MembershipPermissionOverride).where(
        MembershipPermissionOverride.membership_id == membership_id
    )
    return list(session.scalars(stmt).all())


def get_permission(session: Session, key: str) -> Permission | None:
    return session.get(Permission, key)


def list_all_permissions(session: Session) -> list[Permission]:
    stmt = select(Permission).order_by(Permission.module, Permission.key)
    return list(session.scalars(stmt).all())
