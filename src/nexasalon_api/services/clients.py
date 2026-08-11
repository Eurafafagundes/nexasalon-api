import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.repositories import client_repo
from nexasalon_api.schemas.client import ClientCreate, ClientUpdate


def list_clients(
    session: Session,
    organization_id: uuid.UUID,
    include_inactive: bool = False,
    search: str | None = None,
) -> list[Client]:
    return client_repo.list_all(session, organization_id, include_inactive, search)


def get_client(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    client = client_repo.get(session, organization_id, client_id)
    if client is None:
        raise NotFoundError("Cliente não encontrado.")
    return client


def create_client(session: Session, organization_id: uuid.UUID, data: ClientCreate) -> Client:
    return client_repo.create(session, organization_id, **data.model_dump())


def update_client(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID, data: ClientUpdate
) -> Client:
    client = get_client(session, organization_id, client_id)
    for field, value in data.model_dump().items():
        setattr(client, field, value)
    return client_repo.save(session, client)


def set_client_active(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID, is_active: bool
) -> Client:
    """Desativar não apaga o cliente nem seu histórico de agendamentos
    (FK Appointment.client_id é RESTRICT)."""
    client = get_client(session, organization_id, client_id)
    client.is_active = is_active
    return client_repo.save(session, client)
