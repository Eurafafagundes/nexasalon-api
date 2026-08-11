import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nexasalon_api.models.client import Client


def get(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> Client | None:
    stmt = select(Client).where(Client.id == client_id, Client.organization_id == organization_id)
    return session.scalars(stmt).first()


def list_all(
    session: Session,
    organization_id: uuid.UUID,
    include_inactive: bool = False,
    search: str | None = None,
) -> list[Client]:
    stmt = select(Client).where(Client.organization_id == organization_id)
    if not include_inactive:
        stmt = stmt.where(Client.is_active.is_(True))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(Client.name.ilike(pattern), Client.phone.ilike(pattern)))
    return list(session.scalars(stmt.order_by(Client.name)).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> Client:
    client = Client(organization_id=organization_id, **fields)
    session.add(client)
    session.flush()
    return client


def save(session: Session, client: Client) -> Client:
    session.flush()
    return client
