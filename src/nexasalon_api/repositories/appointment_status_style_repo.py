import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.appointment_status_style import AppointmentStatusStyle
from nexasalon_api.models.enums import AppointmentStatus


def list_for_org(session: Session, organization_id: uuid.UUID) -> list[AppointmentStatusStyle]:
    """SPARSE — só as combinações que a organização já personalizou (0 a
    8 linhas). Ver docstring do model: ausência de linha pra um status
    = quem chama cai pro padrão de fábrica."""
    stmt = select(AppointmentStatusStyle).where(AppointmentStatusStyle.organization_id == organization_id)
    return list(session.scalars(stmt).all())


def get(
    session: Session, organization_id: uuid.UUID, status_code: AppointmentStatus
) -> AppointmentStatusStyle | None:
    stmt = select(AppointmentStatusStyle).where(
        AppointmentStatusStyle.organization_id == organization_id,
        AppointmentStatusStyle.status_code == status_code,
    )
    return session.scalars(stmt).first()


def upsert(
    session: Session,
    organization_id: uuid.UUID,
    status_code: AppointmentStatus,
    *,
    label: str | None,
    color_hex: str | None,
    updated_by: uuid.UUID | None,
) -> AppointmentStatusStyle:
    style = get(session, organization_id, status_code)
    if style is None:
        style = AppointmentStatusStyle(organization_id=organization_id, status_code=status_code)
        session.add(style)
    style.label = label
    style.color_hex = color_hex
    style.updated_by = updated_by
    session.flush()
    return style


def delete(session: Session, organization_id: uuid.UUID, status_code: AppointmentStatus) -> None:
    """Reset pro padrão de fábrica — some a linha (idempotente: não
    existir já é o estado "sem personalização")."""
    stmt = sa_delete(AppointmentStatusStyle).where(
        AppointmentStatusStyle.organization_id == organization_id,
        AppointmentStatusStyle.status_code == status_code,
    )
    session.execute(stmt)
    session.flush()
