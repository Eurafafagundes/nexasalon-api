"""Etapa C2 — Comissão por serviço vendido. Fonte ÚNICA da fórmula de
comissão, usada tanto por `close_order` quanto por
`close_orders_consolidated` (`services/orders.py`) — nunca duas
implementações da mesma conta.

Regra central (mesmo raciocínio de `services/payment_fees.py`, Etapa
N3): "sem regra configurada NÃO significa comissão zero". Dois estados
possíveis, sempre gravados de forma EXPLÍCITA em
`OrderItem.commission_status` no momento do fechamento — nunca
inferido só pela nulidade dos snapshots (ver
`models/enums.py::CommissionStatus` pro raciocínio completo):

  1. CALCULATED — existe uma `ProfessionalService` ATIVA com
     `commission_type`/`commission_value` configurados pra este par
     (profissional, serviço): percentual/valor/comissão REAIS,
     congelados como snapshot.
  2. NOT_CONFIGURED — vínculo ausente, inativo, ou sem comissão
     configurada: os 3 snapshots ficam `None` de propósito — o item
     continua válido, só a comissão fica desconhecida (nunca R$0,00,
     nunca inventada).

A base do percentual é `OrderItem.price` — o valor efetivamente
vendido daquele item (já reflete qualquer edição manual de preço feita
enquanto a comanda estava aberta; não existe conceito de desconto
separado no domínio, ver `services/order_totals.py`).

===========================================================================
Etapa C3 — leitura (visão gerencial: `get_overview`/`get_detail`)
===========================================================================

Competência da comissão = `Order.closed_at` (nunca `OrderItem.created_at`
— o instante em que a comanda é CRIADA pode ser dias antes do
fechamento; a comissão só se torna definitiva no fechamento, junto do
snapshot em si, ver `services/orders.py::close_order`). Mesma coluna
que `services/dashboard.py` já usa pra bucketizar Faturamento por
período — Extrato é a exceção (usa `Order.created_at`, granularidade
diferente, decisão de outra etapa), não o padrão a seguir aqui.

Três populações SEMPRE distintas nas agregações abaixo, nunca
misturadas:
  1. CALCULATED — entra em "Comissão calculada".
  2. NOT_CONFIGURED — entra em "sem configuração" (nunca em "Comissão
     calculada", nunca fingido R$0).
  3. `commission_status IS NULL` — `OrderItem` fechado ANTES da
     migration 0036 existir. Nunca aparece como CALCULATED nem como
     NOT_CONFIGURED (os dois são estados que só existem a partir de
     quando o snapshot passou a ser gravado) — conta à parte como
     "histórico anterior ao controle de comissões", nunca soma nem em
     "Produção" nem em "Comissão calculada"."""
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import CommissionStatus, CommissionType, OrderStatus
from nexasalon_api.models.order import Order, OrderItem
from nexasalon_api.repositories import professional_repo, professional_service_repo

_CENTS = Decimal("0.01")

VIEW_ALL_PERMISSION = "commissions.view_all"
VIEW_OWN_PERMISSION = "commissions.view_own"


@dataclass(frozen=True)
class CommissionResolution:
    commission_type_snapshot: CommissionType | None
    commission_value_snapshot: Decimal | None
    commission_amount_snapshot: Decimal | None
    commission_status: CommissionStatus


def resolve_commission(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    service_id: uuid.UUID,
    price: Decimal,
) -> CommissionResolution:
    """Resolvida uma vez, NO FECHAMENTO da comanda — nunca recalculada
    depois. `price` é sempre o valor DESTE `OrderItem` individual (item
    explícito "1 OrderItem = 1 serviço + 1 profissional + 1 valor" —
    nunca a soma da comanda inteira, o que duplicaria comissão entre
    profissionais diferentes na mesma comanda)."""
    link = professional_service_repo.get_for_pair(session, organization_id, professional_id, service_id)
    if link is None or not link.is_active or link.commission_type is None:
        return CommissionResolution(
            commission_type_snapshot=None,
            commission_value_snapshot=None,
            commission_amount_snapshot=None,
            commission_status=CommissionStatus.NOT_CONFIGURED,
        )

    # Decimal em toda a conta (nunca float) — mesma convenção do
    # projeto (`.quantize(Decimal("0.01"))`, contexto decimal padrão
    # do processo, ROUND_HALF_EVEN). `commission_value` vem tipado
    # `float | None` na ORM (coluna `Numeric`, mesmo padrão de
    # `ProfessionalService.price_override`), por isso o `Decimal(str(...))`
    # defensivo — ver `services/availability.py::effective_duration_and_price`.
    commission_value = Decimal(str(link.commission_value))
    if link.commission_type == CommissionType.PERCENTAGE:
        commission_amount = (price * commission_value / Decimal("100")).quantize(_CENTS)
    else:
        commission_amount = commission_value.quantize(_CENTS)

    return CommissionResolution(
        commission_type_snapshot=link.commission_type,
        commission_value_snapshot=commission_value,
        commission_amount_snapshot=commission_amount,
        commission_status=CommissionStatus.CALCULATED,
    )


# ---------------------------------------------------------------------------
# Etapa C3 — leitura (visão gerencial).
# ---------------------------------------------------------------------------


def can_view_professional_commissions(actor: ActorContext, professional_id: uuid.UUID) -> bool:
    """Verdade única de "este ator pode ver a comissão DESTE profissional
    específico" — mesmo raciocínio de `services/agenda_access.py::
    can_view_professional` (view_all vence sempre; view_own só autoriza
    o PRÓPRIO `professional_id` do ator), sem a camada extra de escopo
    granular por membership que a Agenda tem (não pedida aqui).
    Estruturada como função separada de propósito: a Etapa C5 ("Minha
    Comissão") só precisa REUSAR esta função pra decidir o que mostrar,
    nunca duplicar a regra de autorização."""
    if VIEW_ALL_PERMISSION in actor.permissions:
        return True
    return VIEW_OWN_PERMISSION in actor.permissions and actor.professional_id == professional_id


@dataclass(frozen=True)
class CommissionProfessionalSummary:
    professional_id: uuid.UUID
    professional_name: str
    production: Decimal
    commission_total: Decimal
    unconfigured_count: int
    unconfigured_amount: Decimal
    historical_count: int


@dataclass(frozen=True)
class CommissionOverview:
    date_from: datetime
    date_to: datetime
    production: Decimal
    known_commission_total: Decimal
    unconfigured_count: int
    unconfigured_amount: Decimal
    historical_count: int
    professionals: list[CommissionProfessionalSummary]


def get_overview(
    session: Session,
    actor: ActorContext,
    *,
    date_from: datetime,
    date_to: datetime,
    professional_id: uuid.UUID | None = None,
) -> CommissionOverview:
    """Visão gerencial agregada por profissional — UMA query (evita
    N+1: nunca uma query de comissão por profissional depois de listar
    os profissionais). Competência = `Order.closed_at` (ver docstring
    do módulo). `professional_id` (filtro opcional) só é respeitado
    quando o ator tem `commissions.view_all` — sem essa permission, o
    filtro é IGNORADO e forçado pro próprio `actor.professional_id`
    (nunca aceita ver outro profissional só porque pediu no parâmetro
    — a rota já garante que o ator tem pelo menos uma das duas
    permissions antes de chegar aqui)."""
    organization_id = actor.organization_id
    if VIEW_ALL_PERMISSION not in actor.permissions:
        if actor.professional_id is None:
            return CommissionOverview(
                date_from=date_from, date_to=date_to, production=Decimal("0"),
                known_commission_total=Decimal("0"), unconfigured_count=0,
                unconfigured_amount=Decimal("0"), historical_count=0, professionals=[],
            )
        professional_id = actor.professional_id

    # Três populações NUNCA misturadas (ver docstring do módulo):
    # CALCULATED entra em produção/comissão; NOT_CONFIGURED entra só em
    # produção (venda real, comissão desconhecida) + no contador "sem
    # configuração"; `commission_status IS NULL` (histórico anterior à
    # migration 0036) não entra em NADA além do próprio contador —
    # nunca inflar Produção com um item que este módulo não consegue
    # de fato rastrear a comissão.
    production_case = case((OrderItem.commission_status.isnot(None), OrderItem.price), else_=0)
    commission_case = case(
        (OrderItem.commission_status == CommissionStatus.CALCULATED, OrderItem.commission_amount_snapshot),
        else_=0,
    )
    unconfigured_flag = case((OrderItem.commission_status == CommissionStatus.NOT_CONFIGURED, 1), else_=0)
    unconfigured_amount_case = case(
        (OrderItem.commission_status == CommissionStatus.NOT_CONFIGURED, OrderItem.price), else_=0
    )
    historical_flag = case((OrderItem.commission_status.is_(None), 1), else_=0)

    stmt = (
        select(
            OrderItem.professional_id,
            func.max(OrderItem.professional_name),
            func.coalesce(func.sum(production_case), 0),
            func.coalesce(func.sum(commission_case), 0),
            func.coalesce(func.sum(unconfigured_flag), 0),
            func.coalesce(func.sum(unconfigured_amount_case), 0),
            func.coalesce(func.sum(historical_flag), 0),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.organization_id == organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at <= date_to,
        )
        .group_by(OrderItem.professional_id)
        .order_by(func.sum(production_case).desc())
    )
    if professional_id is not None:
        stmt = stmt.where(OrderItem.professional_id == professional_id)

    professionals = [
        CommissionProfessionalSummary(
            professional_id=prof_id,
            professional_name=name,
            production=Decimal(production),
            commission_total=Decimal(commission_total),
            unconfigured_count=int(unconfigured_count),
            unconfigured_amount=Decimal(unconfigured_amount),
            historical_count=int(historical_count),
        )
        for prof_id, name, production, commission_total, unconfigured_count, unconfigured_amount, historical_count
        in session.execute(stmt).all()
    ]

    return CommissionOverview(
        date_from=date_from,
        date_to=date_to,
        production=sum((p.production for p in professionals), Decimal("0")),
        known_commission_total=sum((p.commission_total for p in professionals), Decimal("0")),
        unconfigured_count=sum((p.unconfigured_count for p in professionals), 0),
        unconfigured_amount=sum((p.unconfigured_amount for p in professionals), Decimal("0")),
        historical_count=sum((p.historical_count for p in professionals), 0),
        professionals=professionals,
    )


@dataclass(frozen=True)
class CommissionItemDetail:
    order_item_id: uuid.UUID
    order_id: uuid.UUID
    order_number: int
    closed_at: datetime
    client_name: str
    service_name: str
    price: Decimal
    commission_status: CommissionStatus | None
    commission_type_snapshot: CommissionType | None
    commission_value_snapshot: Decimal | None
    commission_amount_snapshot: Decimal | None


@dataclass(frozen=True)
class CommissionDetail:
    professional_id: uuid.UUID
    professional_name: str
    date_from: datetime
    date_to: datetime
    items: list[CommissionItemDetail]


def get_detail(
    session: Session,
    actor: ActorContext,
    professional_id: uuid.UUID,
    *,
    date_from: datetime,
    date_to: datetime,
) -> CommissionDetail:
    """Rastreabilidade por `OrderItem` — sempre os SNAPSHOTS da C2
    (`commission_type_snapshot`/`commission_value_snapshot`/
    `commission_amount_snapshot`), nunca a configuração ATUAL de
    `ProfessionalService` (que já pode ter mudado desde o fechamento —
    ver docstring de `resolve_commission`). `service_name`/`price` vêm
    do próprio `OrderItem` (snapshot da N2), nenhum join a `Service`.

    404 (nunca 403) quando o ator não pode ver este profissional — mesmo
    padrão anti-leak de `services/appointments.py::get_appointment`:
    nunca confirma/nega a existência do profissional pra quem não tem
    escopo pra vê-lo."""
    if not can_view_professional_commissions(actor, professional_id):
        raise NotFoundError("Profissional não encontrado.")

    organization_id = actor.organization_id
    professional = professional_repo.get(session, organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")

    stmt = (
        select(OrderItem, Order.id, Order.order_number, Order.closed_at, Client.name)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Client, Client.id == Order.client_id)
        .where(
            Order.organization_id == organization_id,
            Order.status == OrderStatus.CLOSED,
            OrderItem.professional_id == professional_id,
            Order.closed_at >= date_from,
            Order.closed_at <= date_to,
        )
        .order_by(Order.closed_at)
    )
    items = [
        CommissionItemDetail(
            order_item_id=item.id,
            order_id=order_id,
            order_number=order_number,
            closed_at=closed_at,
            client_name=client_name,
            service_name=item.service_name,
            price=item.price,
            commission_status=item.commission_status,
            commission_type_snapshot=item.commission_type_snapshot,
            commission_value_snapshot=item.commission_value_snapshot,
            commission_amount_snapshot=item.commission_amount_snapshot,
        )
        for item, order_id, order_number, closed_at, client_name in session.execute(stmt).all()
    ]

    return CommissionDetail(
        professional_id=professional_id,
        professional_name=professional.name,
        date_from=date_from,
        date_to=date_to,
        items=items,
    )
