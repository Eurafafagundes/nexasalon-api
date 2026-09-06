import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.service import Service


def get(session: Session, organization_id: uuid.UUID, service_id: uuid.UUID) -> Service | None:
    stmt = select(Service).where(Service.id == service_id, Service.organization_id == organization_id)
    return session.scalars(stmt).first()


def list_by_ids(session: Session, organization_id: uuid.UUID, service_ids: set[uuid.UUID]) -> list[Service]:
    """Busca em LOTE (`WHERE id IN (...)`) — item de performance ("quick
    win 4", `services/orders.py::create_order`): resolve o nome de
    vários serviços de uma vez em vez de um `get` por item do
    agendamento. Mesmo filtro de `organization_id` de `get`."""
    if not service_ids:
        return []
    stmt = select(Service).where(Service.id.in_(service_ids), Service.organization_id == organization_id)
    return list(session.scalars(stmt).all())


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
