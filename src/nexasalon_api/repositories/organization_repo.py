import uuid

from sqlalchemy.orm import Session

from nexasalon_api.models.organization import Organization


def get(session: Session, organization_id: uuid.UUID) -> Organization | None:
    return session.get(Organization, organization_id)


def create(session: Session, **fields) -> Organization:
    """Sem rota HTTP hoje (não existe fluxo de signup) — usado só pelo
    CLI de bootstrap (`cli/bootstrap_owner.py`, Etapa 3C)."""
    organization = Organization(**fields)
    session.add(organization)
    session.flush()
    return organization
