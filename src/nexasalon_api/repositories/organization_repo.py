import uuid

from sqlalchemy.orm import Session

from nexasalon_api.models.organization import Organization


def get(session: Session, organization_id: uuid.UUID) -> Organization | None:
    return session.get(Organization, organization_id)
