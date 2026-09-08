import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexasalon_api.models.service import Service


def count_by_category(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> int:
    """Conta TODOS os serviços da categoria (ativos e inativos) — usado
    pra decidir se `DELETE /service-categories/{id}` pode prosseguir.
    Um serviço desativado ainda "usa" a categoria (continua existindo,
    só não aparece pra novos agendamentos), então também bloqueia a
    exclusão — nunca `include_inactive=False` aqui."""
    stmt = select(func.count()).select_from(Service).where(
        Service.organization_id == organization_id, Service.category_id == category_id
    )
    return session.scalar(stmt) or 0


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
