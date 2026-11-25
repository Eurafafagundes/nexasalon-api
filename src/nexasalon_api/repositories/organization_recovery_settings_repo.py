import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.client_recovery import OrganizationRecoverySettings


def get(session: Session, organization_id: uuid.UUID) -> OrganizationRecoverySettings | None:
    stmt = select(OrganizationRecoverySettings).where(
        OrganizationRecoverySettings.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def create(session: Session, settings: OrganizationRecoverySettings) -> OrganizationRecoverySettings:
    session.add(settings)
    session.flush()
    return settings


def save(session: Session, settings: OrganizationRecoverySettings) -> OrganizationRecoverySettings:
    session.flush()
    return settings
