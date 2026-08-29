import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.appointment_custom_status import AppointmentCustomStatus


def get(session: Session, organization_id: uuid.UUID, status_id: uuid.UUID) -> AppointmentCustomStatus | None:
    stmt = select(AppointmentCustomStatus).where(
        AppointmentCustomStatus.id == status_id, AppointmentCustomStatus.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[AppointmentCustomStatus]:
    stmt = (
        select(AppointmentCustomStatus)
        .where(AppointmentCustomStatus.organization_id == organization_id)
        .order_by(AppointmentCustomStatus.sort_order, AppointmentCustomStatus.name)
    )
    if not include_inactive:
        stmt = stmt.where(AppointmentCustomStatus.is_active.is_(True))
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> AppointmentCustomStatus:
    custom_status = AppointmentCustomStatus(organization_id=organization_id, **fields)
    session.add(custom_status)
    session.flush()
    return custom_status


def save(session: Session, custom_status: AppointmentCustomStatus) -> AppointmentCustomStatus:
    session.flush()
    return custom_status
