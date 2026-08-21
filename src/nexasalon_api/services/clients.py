import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.appointment import Appointment
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import appointment_repo, client_repo, order_repo
from nexasalon_api.schemas.client import ClientCreate, ClientUpdate

# Agendamentos ainda "em aberto" (nem concluídos nem descartados) — usado
# tanto pra achar o "próximo agendamento" (Etapa J) quanto, por exclusão,
# pra contar faltas/cancelamentos. Deliberadamente diferente de
# `appointment_state_machine.OPERATIONAL_STATUSES` (que inclui
# FINISHED/NO_SHOW — serve outro propósito, mudança de status em lote).
_UPCOMING_STATUSES = frozenset(
    {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.WAITING,
        AppointmentStatus.IN_PROGRESS,
    }
)


def _order_total(order: Order) -> Decimal:
    """Serviços + produtos — mesma fórmula de `OrderRead.from_order`
    (nunca duas contas de faturamento diferentes pro mesmo domínio)."""
    services_total = sum((item.price for item in order.items), Decimal("0"))
    products_total = sum((item.quantity * item.unit_price for item in order.product_items), Decimal("0"))
    return services_total + products_total


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
    # Etapa J: corrigido pra somar TAMBÉM produtos (`_order_total`) —
    # antes só somava `OrderItem.price` (serviço), subestimando "total
    # gasto" em qualquer comanda com produto vendido junto.
    total_spent = sum((_order_total(o) for o in orders), Decimal("0"))
    return ClientSummary(
        client_since=client.created_at, visits_count=len(orders), total_spent=total_spent, orders=orders
    )


@dataclass
class ClientListEntry:
    """Uma linha da lista de Clientes (Etapa J) — `client` + campos
    DERIVADOS de Appointment/Order, calculados em LOTE por
    `list_clients_with_summary` (nunca 1 query por cliente)."""

    client: Client
    last_visit_at: object  # datetime | None
    next_appointment_at: object  # datetime | None
    last_professional_name: str | None
    has_no_show: bool


def list_clients_with_summary(
    session: Session,
    organization_id: uuid.UUID,
    include_inactive: bool = False,
    search: str | None = None,
) -> list[ClientListEntry]:
    """Lista enriquecida (Etapa J, item "Lista de clientes") — nome e
    telefone já vêm do próprio `Client`; "último atendimento" e
    "profissional mais recente" vêm da comanda FECHADA mais recente
    (mesma fonte de `get_client_history`, item "não duplicar dados");
    "próximo agendamento" e o indicador de "faltas" vêm de Appointment.

    Sempre 3 queries no total (clientes + comandas em lote + agendamentos
    em lote), nunca 1 por cliente — importante porque esta lista carrega
    SEM paginação (mesmo comportamento de antes desta etapa)."""
    clients = client_repo.list_all(session, organization_id, include_inactive, search)
    if not clients:
        return []
    client_ids = [c.id for c in clients]

    last_closed_order: dict[uuid.UUID, Order] = {}
    for order in order_repo.list_closed_for_clients(session, organization_id, client_ids):
        # Já vem ordenado `closed_at desc` (ver `order_repo`) — a
        # primeira ocorrência por cliente é sempre a mais recente.
        last_closed_order.setdefault(order.client_id, order)

    next_appointment_at: dict[uuid.UUID, object] = {}
    has_no_show: dict[uuid.UUID, bool] = {}
    for appointment in appointment_repo.list_for_clients(session, organization_id, client_ids):
        if appointment.status == AppointmentStatus.NO_SHOW:
            has_no_show[appointment.client_id] = True
        if appointment.status in _UPCOMING_STATUSES and appointment.starts_at is not None:
            current = next_appointment_at.get(appointment.client_id)
            if current is None or appointment.starts_at < current:
                next_appointment_at[appointment.client_id] = appointment.starts_at

    entries = []
    for client in clients:
        order = last_closed_order.get(client.id)
        professional_name = None
        if order is not None:
            names = list(dict.fromkeys(item.professional_name for item in order.items))
            professional_name = " + ".join(names) if names else None
        entries.append(
            ClientListEntry(
                client=client,
                last_visit_at=order.closed_at if order is not None else None,
                next_appointment_at=next_appointment_at.get(client.id),
                last_professional_name=professional_name,
                has_no_show=has_no_show.get(client.id, False),
            )
        )
    return entries


@dataclass
class ClientProfile:
    """Ficha 360° completa (Etapa J) — tudo DERIVADO de Appointment/
    Order, nada gravado a mais no `Client` (ver docstring de
    `models/client.py::Client`). `can_view_finance`/`can_view_orders`
    refletem `actor.permissions` no momento da consulta — a rota
    (`api/v1/clients.py`) usa esses dois pra decidir o que serializar,
    nunca o frontend."""

    client: Client
    client_since: object  # datetime
    visits_count: int
    # Sempre calculado aqui (não sabe de RBAC) — quem decide se envia
    # `None` no lugar (sem `finance.view`) é a rota, ver `api/v1/clients.py`.
    total_spent: Decimal
    last_visit_at: object  # datetime | None
    next_appointment: Appointment | None
    no_show_count: int
    cancelled_count: int
    timeline: list[Appointment]
    orders: list[Order]
    can_view_finance: bool
    can_view_orders: bool


def get_client_profile(session: Session, actor: ActorContext, client_id: uuid.UUID) -> ClientProfile:
    """Resumo + Histórico + Comandas num único cálculo — reaproveita
    `get_client_history` (visitas/total gasto/comandas fechadas) e
    `order_repo.list_for_org` (já suporta `client_id` com QUALQUER
    status, item "Comandas: abertas; finalizadas; canceladas" —
    nenhuma query nova precisou ser criada pra isso)."""
    organization_id = actor.organization_id
    history = get_client_history(session, organization_id, client_id)

    timeline = appointment_repo.list_for_client(session, organization_id, client_id)
    no_show_count = sum(1 for a in timeline if a.status == AppointmentStatus.NO_SHOW)
    cancelled_count = sum(1 for a in timeline if a.status == AppointmentStatus.CANCELLED)
    upcoming = sorted(
        (a for a in timeline if a.status in _UPCOMING_STATUSES and a.starts_at is not None),
        key=lambda a: a.starts_at,
    )
    next_appointment = upcoming[0] if upcoming else None
    # `history.orders` já vem ordenado `closed_at desc` — a primeira é
    # sempre a comanda fechada mais recente.
    last_visit_at = history.orders[0].closed_at if history.orders else None

    can_view_finance = "finance.view" in actor.permissions
    can_view_orders = "orders.view" in actor.permissions
    orders = order_repo.list_for_org(session, organization_id, client_id=client_id) if can_view_orders else []

    client = get_client(session, organization_id, client_id)
    return ClientProfile(
        client=client,
        client_since=history.client_since,
        visits_count=history.visits_count,
        total_spent=history.total_spent,
        last_visit_at=last_visit_at,
        next_appointment=next_appointment,
        no_show_count=no_show_count,
        cancelled_count=cancelled_count,
        timeline=timeline,
        orders=orders,
        can_view_finance=can_view_finance,
        can_view_orders=can_view_orders,
    )
