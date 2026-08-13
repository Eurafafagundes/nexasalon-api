import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.service import ServiceCategory


def get(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> ServiceCategory | None:
    stmt = select(ServiceCategory).where(
        ServiceCategory.id == category_id, ServiceCategory.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[ServiceCategory]:
    stmt = (
        select(ServiceCategory)
        .where(ServiceCategory.organization_id == organization_id)
        .order_by(ServiceCategory.display_order, ServiceCategory.name)
    )
    if not include_inactive:
        stmt = stmt.where(ServiceCategory.is_active.is_(True))
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> ServiceCategory:
    category = ServiceCategory(organization_id=organization_id, **fields)
    session.add(category)
    session.flush()
    return category


def save(session: Session, category: ServiceCategory) -> ServiceCategory:
    session.flush()
    return category
