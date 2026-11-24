import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.organization_public_page import OrganizationPublicPage


def get(session: Session, organization_id: uuid.UUID) -> OrganizationPublicPage | None:
    stmt = select(OrganizationPublicPage).where(OrganizationPublicPage.organization_id == organization_id)
    return session.scalars(stmt).first()


def get_by_slug_for_public(session: Session, organization_id: uuid.UUID) -> OrganizationPublicPage | None:
    """Mesma query de `get` — nome próprio só pra deixar explícito, no
    call site público (`services/public_page.py`), que esta leitura
    acontece DEPOIS da organização já ter sido resolvida por slug sob o
    mecanismo de RLS de `get_public_page_context` (nunca antes)."""
    return get(session, organization_id)


def create(session: Session, page: OrganizationPublicPage) -> OrganizationPublicPage:
    session.add(page)
    session.flush()
    return page


def save(session: Session, page: OrganizationPublicPage) -> OrganizationPublicPage:
    session.flush()
    return page
