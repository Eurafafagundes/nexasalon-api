import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.service import Service


def get(session: Session, organization_id: uuid.UUID, service_id: uuid.UUID) -> Service | None:
    stmt = select(Service).where(Service.id == service_id, Service.organization_id == organization_id)
    return session.scalars(stmt).first()


def list_all(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[Service]:
    stmt = select(Service).where(Service.organization_id == organization_id).order_by(Service.name)
    if not include_inactive:
        stmt = stmt.where(Service.is_active.is_(True))
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> Service:
    service = Service(organization_id=organization_id, **fields)
    session.add(service)
    session.flush()
    return service


def save(session: Session, service: Service) -> Service:
    session.flush()
    return service
