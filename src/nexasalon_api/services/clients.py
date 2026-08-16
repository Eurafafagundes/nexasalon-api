import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import client_repo, order_repo
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


@dataclass
class ClientSummary:
    """Tudo aqui é DERIVADO de `Order` — nunca um campo gravado no
    próprio `Client` (item "esses números devem ser derivados dos
    dados reais, nunca campos manuais"). `visits_count`/`total_spent`
    contam por COMANDA fechada, não por serviço/pagamento — uma comanda
    com 2 serviços é 1 atendimento, não 2 (item "definições
    analíticas")."""

    client_since: object  # datetime — ver Client.created_at
    visits_count: int
    total_spent: Decimal
    orders: list[Order]


def get_client_history(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> ClientSummary:
    client = get_client(session, organization_id, client_id)
    orders = order_repo.list_for_client(session, organization_id, client_id)
    total_spent = sum((sum((item.price for item in o.items), Decimal("0")) for o in orders), Decimal("0"))
    return ClientSummary(
        client_since=client.created_at, visits_count=len(orders), total_spent=total_spent, orders=orders
    )
