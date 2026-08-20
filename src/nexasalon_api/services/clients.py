import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import client_repo, order_repo
from nexasalon_api.schemas.client import ClientCreate, ClientUpdate


def _assert_cpf_not_duplicated(
    session: Session, organization_id: uuid.UUID, cpf: str | None, *, exclude_client_id: uuid.UUID | None = None
) -> None:
    """Unicidade de CPF SEMPRE escopada por `organization_id` — nunca
    global (a mesma pessoa pode legitimamente existir como cliente em
    duas organizações diferentes, ex.: ela frequenta dois salões que
    usam o NexaSalon; isso nunca pode ser tratado como conflito). Ver
    docstring de `models/client.py::Client` para o porquê disso viver
    aqui (service layer) em vez de virar uma constraint de banco nesta
    rodada — dado de staging já existente, sem garantia de estar limpo."""
    if cpf is None:
        return
    existing = client_repo.get_by_cpf(session, organization_id, cpf)
    if existing is not None and existing.id != exclude_client_id:
        raise ConflictError("Já existe um cliente com este CPF nesta organização.")


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
    _assert_cpf_not_duplicated(session, organization_id, data.cpf)
    return client_repo.create(session, organization_id, **data.model_dump())


def update_client(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID, data: ClientUpdate
) -> Client:
    client = get_client(session, organization_id, client_id)
    _assert_cpf_not_duplicated(session, organization_id, data.cpf, exclude_client_id=client.id)
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
