import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.organization import Branch


def get(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> Branch | None:
    # Filtro explícito por organization_id além do RLS — defesa em
    # profundidade (ver seção 7 do documento de modelagem da Etapa 2A).
    stmt = select(Branch).where(Branch.id == branch_id, Branch.organization_id == organization_id)
    return session.scalars(stmt).first()


def list_all(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[Branch]:
    stmt = select(Branch).where(Branch.organization_id == organization_id).order_by(Branch.name)
    if not include_inactive:
        stmt = stmt.where(Branch.is_active.is_(True))
    return list(session.scalars(stmt).all())


def exists(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> bool:
    return get(session, organization_id, branch_id) is not None


def create(session: Session, organization_id: uuid.UUID, **fields) -> Branch:
    branch = Branch(organization_id=organization_id, **fields)
    session.add(branch)
    session.flush()
    return branch


def save(session: Session, branch: Branch) -> Branch:
    session.flush()
    return branch
