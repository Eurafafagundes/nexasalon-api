import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.professional import Professional


def get(session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID) -> Professional | None:
    stmt = select(Professional).where(
        Professional.id == professional_id, Professional.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[Professional]:
    stmt = select(Professional).where(Professional.organization_id == organization_id).order_by(Professional.name)
    if not include_inactive:
        stmt = stmt.where(Professional.is_active.is_(True))
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> Professional:
    professional = Professional(organization_id=organization_id, **fields)
    session.add(professional)
    session.flush()
    return professional


def save(session: Session, professional: Professional) -> Professional:
    session.flush()
    return professional
