import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from nexasalon_api.models.organization import BusinessHours


def list_for_organization(session: Session, organization_id: uuid.UUID) -> list[BusinessHours]:
    stmt = (
        select(BusinessHours)
        .where(BusinessHours.organization_id == organization_id)
        .order_by(BusinessHours.weekday)
    )
    return list(session.scalars(stmt).all())


def replace_all(session: Session, organization_id: uuid.UUID, items: list[dict]) -> list[BusinessHours]:
    """Substitui o horário de funcionamento inteiro (as 7 linhas) pelos
    itens informados — semântica de "PUT idempotente", mesmo padrão de
    `working_hours_repo.replace_all`."""
    session.execute(delete(BusinessHours).where(BusinessHours.organization_id == organization_id))
    created = []
    for item in items:
        row = BusinessHours(organization_id=organization_id, **item)
        session.add(row)
        created.append(row)
    session.flush()
    return created
