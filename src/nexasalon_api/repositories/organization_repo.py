import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.organization import Organization


def get(session: Session, organization_id: uuid.UUID) -> Organization | None:
    return session.get(Organization, organization_id)


def get_by_slug(session: Session, slug: str) -> Organization | None:
    """Busca por slug — Etapa K (Agendamento Online público). Sob RLS
    normal (autenticado, `app.current_org_id` já setado) só enxerga a
    PRÓPRIA organização, então isto só devolve uma organização de outro
    tenant quando o chamador ligou deliberadamente o flag de sessão
    `app.public_booking_lookup` (ver `api/deps.py::get_public_context` e
    `services/organizations.py`, migration 0028) — nunca por acidente."""
    stmt = select(Organization).where(Organization.slug == slug)
    return session.scalars(stmt).first()


def create(session: Session, **fields) -> Organization:
    """Sem rota HTTP hoje (não existe fluxo de signup) — usado só pelo
    CLI de bootstrap (`cli/bootstrap_owner.py`, Etapa 3C)."""
    organization = Organization(**fields)
    session.add(organization)
    session.flush()
    return organization


def save(session: Session, organization: Organization) -> Organization:
    """`organization` já deve ter os atributos alterados via `setattr`
    (ver `services/organizations.py::update_organization`) — mesmo
    padrão de `client_repo.save`."""
    session.flush()
    return organization
