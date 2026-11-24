import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import PublicLinkKind
from nexasalon_api.models.organization_public_link import OrganizationPublicLink


def list_for_org(session: Session, organization_id: uuid.UUID) -> list[OrganizationPublicLink]:
    stmt = (
        select(OrganizationPublicLink)
        .where(OrganizationPublicLink.organization_id == organization_id)
        .order_by(OrganizationPublicLink.display_order)
    )
    return list(session.scalars(stmt).all())


def get(session: Session, organization_id: uuid.UUID, link_id: uuid.UUID) -> OrganizationPublicLink | None:
    stmt = select(OrganizationPublicLink).where(
        OrganizationPublicLink.id == link_id, OrganizationPublicLink.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def get_native(session: Session, organization_id: uuid.UUID, kind: PublicLinkKind) -> OrganizationPublicLink | None:
    """Os 4 nativos são únicos por `(organization_id, kind)` — nunca
    pode haver duas linhas `booking` pra mesma organização."""
    stmt = select(OrganizationPublicLink).where(
        OrganizationPublicLink.organization_id == organization_id, OrganizationPublicLink.kind == kind
    )
    return session.scalars(stmt).first()


def create(session: Session, link: OrganizationPublicLink) -> OrganizationPublicLink:
    session.add(link)
    session.flush()
    return link


def delete(session: Session, link: OrganizationPublicLink) -> None:
    """Só usada pra links PERSONALIZADOS removidos pelo usuário — nativos
    nunca passam por aqui (ver `services/organization_public_page.py`,
    que recusa remover `kind != CUSTOM`)."""
    session.delete(link)
    session.flush()
