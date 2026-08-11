import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from nexasalon_api.models.professional import WorkingHours


def list_for_professional(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID
) -> list[WorkingHours]:
    stmt = (
        select(WorkingHours)
        .where(
            WorkingHours.organization_id == organization_id,
            WorkingHours.professional_id == professional_id,
        )
        .order_by(WorkingHours.weekday, WorkingHours.start_time)
    )
    return list(session.scalars(stmt).all())


def replace_all(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID, items: list[dict]
) -> list[WorkingHours]:
    """Substitui a jornada inteira do profissional pelos itens
    informados — semântica de "PUT idempotente", não de PATCH parcial."""
    session.execute(
        delete(WorkingHours).where(
            WorkingHours.organization_id == organization_id,
            WorkingHours.professional_id == professional_id,
        )
    )
    created = []
    for item in items:
        row = WorkingHours(organization_id=organization_id, professional_id=professional_id, **item)
        session.add(row)
        created.append(row)
    session.flush()
    return created
