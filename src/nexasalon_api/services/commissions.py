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
     "Produção" nem em "Comissão calculada".

===========================================================================
Etapa C4 — Fechamento/Pagamento de Comissão + Ajustes Auditáveis
===========================================================================

"A pagar"/"Pago" são DERIVADOS, nunca uma terceira coluna de status
redundante (mesmo raciocínio de `PaymentFeeStatus`/`CommissionStatus`
acima — nunca duplicar uma informação que já existe estruturada):

  - A pagar: `commission_status = calculated AND
    commission_settlement_id IS NULL`.
  - Pago:    `commission_status = calculated AND
    commission_settlement_id IS NOT NULL`.

`not_configured` e histórico (`commission_status IS NULL`) NUNCA
entram em "A pagar"/"Pago" — permanecem suas próprias populações
(reforçado também por CHECK no banco, ver migration 0038).

`create_settlement` é o único caminho que preenche
`OrderItem.commission_settlement_id` — sempre travando as linhas
candidatas (`order_item_repo.lock_pending_commission_items`) ANTES de
somar qualquer total, pra nunca pagar o mesmo item duas vezes sob
concorrência (ver docstring da função). Uma vez criado, um
`CommissionSettlement` é permanente: nenhuma rota de edição/exclusão
existe. Uma correção posterior é sempre um `CommissionAdjustment`
novo (`create_adjustment`), nunca uma edição do settlement nem do
snapshot original do `OrderItem`."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.models.client import Client
from nexasalon_api.models.commission import CommissionAdjustment
from nexasalon_api.models.enums import (
    AuditAction,
    CommissionStatus,
    CommissionType,
    OrderStatus,
)
from nexasalon_api.models.order import Order, OrderItem
from nexasalon_api.repositories import (
    audit_log_repo,
    commission_adjustment_repo,
    commission_settlement_repo,
    order_item_repo,
    professional_repo,
    professional_service_repo,
    user_repo,
)

_CENTS = Decimal("0.01")

VIEW_ALL_PERMISSION = "commissions.view_all"
VIEW_OWN_PERMISSION = "commissions.view_own"
MANAGE_PERMISSION = "commissions.manage"


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
    # Etapa C4 — soma de `commission_total` que já entrou (`paid_total`)
    # ou ainda não entrou (`pending_total`) em algum `CommissionSettlement`.
    # `paid_total + pending_total == commission_total`, sempre.
    paid_total: Decimal
    pending_total: Decimal
    unconfigured_count: int
    unconfigured_amount: Decimal
    historical_count: int


@dataclass(frozen=True)
class CommissionOverview:
    date_from: datetime
    date_to: datetime
    production: Decimal
    known_commission_total: Decimal
    paid_total: Decimal
    pending_total: Decimal
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
                known_commission_total=Decimal("0"), paid_total=Decimal("0"), pending_total=Decimal("0"),
                unconfigured_count=0, unconfigured_amount=Decimal("0"), historical_count=0, professionals=[],
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
    # Etapa C4 — "Pago" = calculada E já linkada a um settlement;
    # "A pagar" = calculada E ainda sem settlement. Nunca deriva de
    # `not_configured`/histórico (esses ficam de fora dos dois).
    paid_case = case(
        (
            and_(OrderItem.commission_status == CommissionStatus.CALCULATED, OrderItem.commission_settlement_id.isnot(None)),
            OrderItem.commission_amount_snapshot,
        ),
        else_=0,
    )
    pending_case = case(
        (
            and_(OrderItem.commission_status == CommissionStatus.CALCULATED, OrderItem.commission_settlement_id.is_(None)),
            OrderItem.commission_amount_snapshot,
        ),
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
            func.coalesce(func.sum(paid_case), 0),
            func.coalesce(func.sum(pending_case), 0),
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
            paid_total=Decimal(paid_total),
            pending_total=Decimal(pending_total),
            unconfigured_count=int(unconfigured_count),
            unconfigured_amount=Decimal(unconfigured_amount),
            historical_count=int(historical_count),
        )
        for (
            prof_id, name, production, commission_total, paid_total, pending_total,
            unconfigured_count, unconfigured_amount, historical_count,
        ) in session.execute(stmt).all()
    ]

    return CommissionOverview(
        date_from=date_from,
        date_to=date_to,
        production=sum((p.production for p in professionals), Decimal("0")),
        known_commission_total=sum((p.commission_total for p in professionals), Decimal("0")),
        paid_total=sum((p.paid_total for p in professionals), Decimal("0")),
        pending_total=sum((p.pending_total for p in professionals), Decimal("0")),
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
    # Etapa C4 — `None` = "A pagar" (quando `commission_status=calculated`)
    # ou irrelevante (`not_configured`/histórico); preenchido = "Pago",
    # aponta pro `CommissionSettlement` que liquidou este item.
    commission_settlement_id: uuid.UUID | None


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
    status: str | None = None,
) -> CommissionDetail:
    """Rastreabilidade por `OrderItem` — sempre os SNAPSHOTS da C2
    (`commission_type_snapshot`/`commission_value_snapshot`/
    `commission_amount_snapshot`), nunca a configuração ATUAL de
    `ProfessionalService` (que já pode ter mudado desde o fechamento —
    ver docstring de `resolve_commission`). `service_name`/`price` vêm
    do próprio `OrderItem` (snapshot da N2), nenhum join a `Service`.

    `status` (Etapa C4 — filtro Todos/A pagar/Pago/Não configurado da
    tela) filtra a LISTAGEM; os totais de `get_overview` continuam
    sempre sobre o período inteiro, independente do filtro (mesma
    filosofia do Extrato: filtro nunca muda o total exibido em outro
    lugar). `None`/valor desconhecido = "Todos", inclusive histórico.

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
    if status == "pending":
        stmt = stmt.where(
            OrderItem.commission_status == CommissionStatus.CALCULATED, OrderItem.commission_settlement_id.is_(None)
        )
    elif status == "paid":
        stmt = stmt.where(
            OrderItem.commission_status == CommissionStatus.CALCULATED,
            OrderItem.commission_settlement_id.isnot(None),
        )
    elif status == "unconfigured":
        stmt = stmt.where(OrderItem.commission_status == CommissionStatus.NOT_CONFIGURED)

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
            commission_settlement_id=item.commission_settlement_id,
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


# ---------------------------------------------------------------------------
# Etapa C4 — Ajustes auditáveis.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommissionAdjustmentResult:
    id: uuid.UUID
    professional_id: uuid.UUID
    order_item_id: uuid.UUID | None
    commission_settlement_id: uuid.UUID | None
    amount: Decimal
    reason: str
    created_by_name: str | None
    created_at: datetime


def create_adjustment(
    session: Session,
    actor: ActorContext,
    *,
    professional_id: uuid.UUID,
    amount: Decimal,
    reason: str,
    order_item_id: uuid.UUID | None = None,
) -> CommissionAdjustmentResult:
    """Correção manual auditável — NUNCA altera o snapshot original de
    `OrderItem` nem um settlement já criado (ver docstring do módulo).
    Exige `commissions.manage` (checado na rota, `api/v1/commissions.py`
    — `view_all`/`view_own` sozinhos nunca chegam aqui). Valor não-zero
    e motivo não-vazio são regra de NEGÓCIO (não só formato do schema
    Pydantic), por isso validados de novo aqui."""
    organization_id = actor.organization_id
    if amount == 0:
        raise ValidationDomainError("O valor do ajuste não pode ser zero.")
    reason = reason.strip()
    if not reason:
        raise ValidationDomainError("Informe o motivo do ajuste.")

    professional = professional_repo.get(session, organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")

    if order_item_id is not None:
        item = session.get(OrderItem, order_item_id)
        if item is None or item.organization_id != organization_id:
            raise NotFoundError("Item de comanda não encontrado.")
        if item.professional_id != professional_id:
            raise ValidationDomainError("O item de comanda não pertence a este profissional.")

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None

    adjustment = commission_adjustment_repo.create(
        session,
        organization_id,
        professional_id=professional_id,
        order_item_id=order_item_id,
        amount=amount.quantize(_CENTS),
        reason=reason,
        created_by=actor.user_id,
        created_by_name=actor_name,
    )
    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="commission_adjustment",
        entity_id=adjustment.id,
        action=AuditAction.CREATE,
        new_values={
            "professional_id": str(professional_id),
            "order_item_id": str(order_item_id) if order_item_id else None,
            "amount": str(adjustment.amount),
            "reason": reason,
        },
    )
    return CommissionAdjustmentResult(
        id=adjustment.id,
        professional_id=adjustment.professional_id,
        order_item_id=adjustment.order_item_id,
        commission_settlement_id=adjustment.commission_settlement_id,
        amount=adjustment.amount,
        reason=adjustment.reason,
        created_by_name=adjustment.created_by_name,
        created_at=adjustment.created_at,
    )


def get_pending_adjustments(
    session: Session, actor: ActorContext, professional_id: uuid.UUID
) -> list[CommissionAdjustmentResult]:
    """Ajustes ainda NÃO incluídos em nenhum settlement — candidatos a
    entrar na próxima liquidação (`create_settlement`,
    `adjustment_ids`). Decoupled de período: um ajuste pode corrigir
    algo de um período anterior e ser pago junto de uma liquidação
    futura, por isso nunca filtrado por `date_from`/`date_to` aqui."""
    if not can_view_professional_commissions(actor, professional_id):
        raise NotFoundError("Profissional não encontrado.")
    rows = commission_adjustment_repo.list_pending_for_professional(session, actor.organization_id, professional_id)
    return [
        CommissionAdjustmentResult(
            id=row.id,
            professional_id=row.professional_id,
            order_item_id=row.order_item_id,
            commission_settlement_id=row.commission_settlement_id,
            amount=row.amount,
            reason=row.reason,
            created_by_name=row.created_by_name,
            created_at=row.created_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Etapa C4 — Fechamento/Pagamento de Comissão (settlements).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommissionSettlementSummary:
    id: uuid.UUID
    professional_id: uuid.UUID
    professional_name: str
    period_start: datetime
    period_end: datetime
    production_total: Decimal
    commission_total: Decimal
    created_by_name: str | None
    paid_at: datetime


def create_settlement(
    session: Session,
    actor: ActorContext,
    *,
    professional_id: uuid.UUID,
    date_from: datetime,
    date_to: datetime,
    adjustment_ids: list[uuid.UUID] | None = None,
) -> CommissionSettlementSummary:
    """"Registrar pagamento" — o ato de pagamento em si (nunca um
    rascunho separado: criar o settlement JÁ é a confirmação). Exige
    `commissions.manage` (checado na rota).

    Concorrência (item mandatório da Etapa C4): trava as linhas de
    `OrderItem` candidatas com `SELECT ... FOR UPDATE OF order_items`
    (`order_item_repo.lock_pending_commission_items`) ANTES de
    calcular qualquer total — sob READ COMMITTED (padrão do Postgres),
    uma segunda requisição concorrente pro MESMO profissional+período
    bloqueia até a primeira commitar e, ao ser liberada, reavalia o
    `WHERE` (`commission_settlement_id IS NULL`) contra o dado JÁ
    atualizado pela primeira — os itens que a primeira já reivindicou
    somem naturalmente do resultado da segunda. Nunca um "reverificar
    depois de travar" separado: a trava E a leitura são o mesmo
    `SELECT`, então não há janela entre travar e ler pra explorar.
    Mesma trava (`FOR UPDATE`) e mesmo raciocínio pros ajustes
    escolhidos (`commission_adjustment_repo.lock_by_ids`).

    Nunca inclui `not_configured` nem histórico (`commission_status
    IS NULL`) — a query de `lock_pending_commission_items` já filtra
    `commission_status = calculated`, reforçado também por CHECK no
    banco (`ck_order_items_commission_settlement_id_requires_calculated`,
    migration 0038). Uma venda com competência DENTRO do período que só
    aparece DEPOIS de um settlement já criado nunca é absorvida
    retroativamente — fica pendente pra uma liquidação futura separada
    (o settlement já criado é permanente, nunca reaberto)."""
    organization_id = actor.organization_id
    professional = professional_repo.get(session, organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")

    items = order_item_repo.lock_pending_commission_items(
        session, organization_id, professional_id, date_from=date_from, date_to=date_to
    )
    if not items:
        raise ValidationDomainError("Não há comissão pendente para este profissional neste período.")

    adjustments: list[CommissionAdjustment] = []
    if adjustment_ids:
        adjustments = commission_adjustment_repo.lock_by_ids(
            session, organization_id, professional_id, adjustment_ids
        )
        missing = set(adjustment_ids) - {row.id for row in adjustments}
        if missing:
            raise NotFoundError("Um ou mais ajustes não foram encontrados.")
        already_settled = [row for row in adjustments if row.commission_settlement_id is not None]
        if already_settled:
            raise ConflictError("Um ou mais ajustes já foram incluídos em outro pagamento.")

    production_total = sum((item.price for item in items), Decimal("0")).quantize(_CENTS)
    items_commission_total = sum((item.commission_amount_snapshot or Decimal("0") for item in items), Decimal("0"))
    adjustments_total = sum((row.amount for row in adjustments), Decimal("0"))
    commission_total = (items_commission_total + adjustments_total).quantize(_CENTS)

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None
    paid_at = datetime.now(timezone.utc)

    settlement = commission_settlement_repo.create(
        session,
        organization_id,
        professional_id=professional_id,
        period_start=date_from,
        period_end=date_to,
        production_total=production_total,
        commission_total=commission_total,
        created_by=actor.user_id,
        created_by_name=actor_name,
        paid_at=paid_at,
    )
    for item in items:
        item.commission_settlement_id = settlement.id
    for row in adjustments:
        row.commission_settlement_id = settlement.id
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="commission_settlement",
        entity_id=settlement.id,
        action=AuditAction.CREATE,
        new_values={
            "professional_id": str(professional_id),
            "period_start": date_from.isoformat(),
            "period_end": date_to.isoformat(),
            "production_total": str(production_total),
            "commission_total": str(commission_total),
            "items_count": len(items),
            "adjustments_count": len(adjustments),
        },
    )

    return CommissionSettlementSummary(
        id=settlement.id,
        professional_id=professional_id,
        professional_name=professional.name,
        period_start=date_from,
        period_end=date_to,
        production_total=production_total,
        commission_total=commission_total,
        created_by_name=actor_name,
        paid_at=paid_at,
    )


def list_settlements(
    session: Session,
    actor: ActorContext,
    *,
    professional_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[CommissionSettlementSummary]:
    """Histórico de pagamentos. Sem `commissions.view_all`, o filtro é
    forçado pro próprio profissional (mesmo raciocínio de
    `get_overview`)."""
    organization_id = actor.organization_id
    if VIEW_ALL_PERMISSION not in actor.permissions:
        if actor.professional_id is None:
            return []
        professional_id = actor.professional_id

    rows = commission_settlement_repo.list_all(
        session, organization_id, professional_id=professional_id, date_from=date_from, date_to=date_to
    )
    return [
        CommissionSettlementSummary(
            id=row.id,
            professional_id=row.professional_id,
            professional_name=name,
            period_start=row.period_start,
            period_end=row.period_end,
            production_total=row.production_total,
            commission_total=row.commission_total,
            created_by_name=row.created_by_name,
            paid_at=row.paid_at,
        )
        for row, name in rows
    ]


@dataclass(frozen=True)
class CommissionSettlementItemDetail:
    order_item_id: uuid.UUID
    order_id: uuid.UUID
    order_number: int
    closed_at: datetime
    client_name: str
    service_name: str
    price: Decimal
    commission_amount_snapshot: Decimal


@dataclass(frozen=True)
class CommissionSettlementAdjustmentDetail:
    id: uuid.UUID
    order_item_id: uuid.UUID | None
    amount: Decimal
    reason: str
    created_by_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class CommissionSettlementDetail:
    settlement: CommissionSettlementSummary
    items: list[CommissionSettlementItemDetail]
    adjustments: list[CommissionSettlementAdjustmentDetail]


def get_settlement_detail(
    session: Session, actor: ActorContext, settlement_id: uuid.UUID
) -> CommissionSettlementDetail:
    """Ao abrir um pagamento no histórico, mostra EXATAMENTE os
    `OrderItem`s e ajustes incluídos naquela liquidação (nunca uma
    reconstrução por `profissional + período` — ver docstring do
    módulo/`models/commission.py`)."""
    organization_id = actor.organization_id
    settlement = commission_settlement_repo.get(session, organization_id, settlement_id)
    if settlement is None:
        raise NotFoundError("Pagamento não encontrado.")
    if not can_view_professional_commissions(actor, settlement.professional_id):
        raise NotFoundError("Pagamento não encontrado.")

    professional = professional_repo.get(session, organization_id, settlement.professional_id)
    summary = CommissionSettlementSummary(
        id=settlement.id,
        professional_id=settlement.professional_id,
        professional_name=professional.name if professional is not None else "",
        period_start=settlement.period_start,
        period_end=settlement.period_end,
        production_total=settlement.production_total,
        commission_total=settlement.commission_total,
        created_by_name=settlement.created_by_name,
        paid_at=settlement.paid_at,
    )

    item_stmt = (
        select(OrderItem, Order.id, Order.order_number, Order.closed_at, Client.name)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Client, Client.id == Order.client_id)
        .where(OrderItem.commission_settlement_id == settlement_id)
        .order_by(Order.closed_at)
    )
    items = [
        CommissionSettlementItemDetail(
            order_item_id=item.id,
            order_id=order_id,
            order_number=order_number,
            closed_at=closed_at,
            client_name=client_name,
            service_name=item.service_name,
            price=item.price,
            commission_amount_snapshot=item.commission_amount_snapshot or Decimal("0"),
        )
        for item, order_id, order_number, closed_at, client_name in session.execute(item_stmt).all()
    ]

    adjustments = [
        CommissionSettlementAdjustmentDetail(
            id=row.id,
            order_item_id=row.order_item_id,
            amount=row.amount,
            reason=row.reason,
            created_by_name=row.created_by_name,
            created_at=row.created_at,
        )
        for row in commission_adjustment_repo.list_for_settlement(session, settlement_id)
    ]

    return CommissionSettlementDetail(settlement=summary, items=items, adjustments=adjustments)
