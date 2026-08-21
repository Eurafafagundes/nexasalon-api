import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.enums import OrderStatus
from nexasalon_api.models.order import Order


def get(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order | None:
    # `populate_existing=True` — mesmo motivo de `appointment_repo.get`:
    # este repo é consultado de novo logo depois de editar preço/fechar
    # a comanda dentro da mesma transação/identity map.
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.product_items), selectinload(Order.payments))
        .where(Order.id == order_id, Order.organization_id == organization_id)
        .execution_options(populate_existing=True)
    )
    return session.scalars(stmt).first()


def get_for_update(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order | None:
    """Mesma consulta de `get`, mas com `SELECT ... FOR UPDATE` na linha
    da comanda — usada por toda operação que muda o conteúdo/status de
    uma comanda `OPEN` (adicionar/remover/editar item ou produto,
    fechar). Serializa duas requisições concorrentes na MESMA comanda:
    a segunda bloqueia até a primeira commitar, e então enxerga o
    estado JÁ atualizado (ex.: `status=closed`) em vez de decidir com
    base num valor "velho" — é isto que torna um retry de fechamento
    seguro (a segunda tentativa vê a comanda já fechada e recusa, nunca
    fecha/baixa estoque duas vezes) e uma edição concorrente durante o
    fechamento impossível de intercalar de forma inconsistente."""
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.product_items), selectinload(Order.payments))
        .where(Order.id == order_id, Order.organization_id == organization_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.scalars(stmt).first()


def get_by_appointment(session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID) -> Order | None:
    """A comanda ATIVA (OPEN ou CLOSED) deste agendamento — nunca uma
    CANCELLED (migration 0024, item "Cancelar Comanda"): depois de
    cancelar uma comanda criada por engano, o Appointment precisa
    voltar a "sem comanda" pra `create_order` permitir abrir uma nova, e
    `services/appointments.py::update_appointment_item` (edição de
    preço/duração pela Agenda) precisa enxergar "nenhuma comanda pra
    sincronizar/bloquear" — não a cancelada. O índice único parcial
    `uq_orders_appointment_id_active` garante o mesmo filtro no banco."""
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.product_items), selectinload(Order.payments))
        .where(
            Order.appointment_id == appointment_id,
            Order.organization_id == organization_id,
            Order.status != OrderStatus.CANCELLED,
        )
        .execution_options(populate_existing=True)
    )
    return session.scalars(stmt).first()


def list_active_for_client(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[Order]:
    """Comandas NÃO canceladas (OPEN ou CLOSED) da cliente, qualquer
    unidade/data — usada por `services/orders.py::get_related_orders`
    (Etapa I, "Comandas relacionadas") pra filtrar o dia operacional em
    memória (o agrupamento depende de `Appointment.starts_at`, que não
    dá pra comparar diretamente numa cláusula `WHERE` desta tabela)."""
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.product_items), selectinload(Order.payments))
        .where(
            Order.organization_id == organization_id,
            Order.client_id == client_id,
            Order.status != OrderStatus.CANCELLED,
        )
    )
    return list(session.scalars(stmt).all())


def list_closed_for_clients(session: Session, organization_id: uuid.UUID, client_ids: list[uuid.UUID]) -> list[Order]:
    """Comandas FECHADAS de vários clientes de uma vez, mais recente
    primeiro — Etapa J, "Lista de clientes" (calcula "último
    atendimento"/"profissional mais recente" em LOTE, evita 1 query por
    linha da lista). Mesmo filtro de `list_for_client` (só CLOSED)."""
    if not client_ids:
        return []
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(
            Order.organization_id == organization_id,
            Order.client_id.in_(client_ids),
            Order.status == OrderStatus.CLOSED,
        )
        .order_by(Order.closed_at.desc())
    )
    return list(session.scalars(stmt).all())


def _next_order_number(session: Session, organization_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(Order.order_number), 0) + 1).where(
        Order.organization_id == organization_id
    )
    return session.scalar(stmt) or 1


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    appointment_id: uuid.UUID,
    branch_id: uuid.UUID,
    client_id: uuid.UUID,
    created_by: uuid.UUID | None,
) -> Order:
    order = Order(
        organization_id=organization_id,
        order_number=_next_order_number(session, organization_id),
        appointment_id=appointment_id,
        branch_id=branch_id,
        client_id=client_id,
        created_by=created_by,
    )
    session.add(order)
    session.flush()
    return order


def list_for_client(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[Order]:
    """Histórico do cliente (item "universal, derivado de Comandas") —
    só comandas FECHADAS (venda concluída), mais recente primeiro."""
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.product_items), selectinload(Order.payments))
        .where(
            Order.organization_id == organization_id,
            Order.client_id == client_id,
            Order.status == OrderStatus.CLOSED,
        )
        .order_by(Order.closed_at.desc())
    )
    return list(session.scalars(stmt).all())


def list_for_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    status: OrderStatus | None = None,
    client_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
    order_number: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[Order]:
    """Comandas Abertas/Finalizadas (Financeiro > Comandas) com filtros
    simples — profissional filtra por qualquer item da comanda conter
    aquele profissional."""
    stmt = select(Order).options(selectinload(Order.items), selectinload(Order.product_items), selectinload(Order.payments)).where(
        Order.organization_id == organization_id
    )
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if client_id is not None:
        stmt = stmt.where(Order.client_id == client_id)
    if order_number is not None:
        stmt = stmt.where(Order.order_number == order_number)
    if date_from is not None:
        stmt = stmt.where(Order.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.created_at <= date_to)
    if professional_id is not None:
        stmt = stmt.where(Order.items.any(professional_id=professional_id))
    return list(session.scalars(stmt.order_by(Order.created_at.desc())).all())
