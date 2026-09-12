"""Dashboard/BI — camada analítica central. Item explícito do pedido:
"UMA definição centralizada para cada métrica" (não duas contas de
faturamento diferentes entre Dashboard e Financeiro). Este módulo é a
ÚNICA fonte de verdade de cada KPI abaixo — o frontend só apresenta.

===========================================================================
TRÊS CONCEITOS — NUNCA MISTURAR AS GRANULARIDADES (item explícito de
uma correção posterior ao desenho original deste módulo)
===========================================================================

VENDAS/FATURAMENTO = valor efetivamente VENDIDO em comandas fechadas
válidas (`OrderItem`, ver definição abaixo). Mede o que foi vendido,
independente de como/quando o dinheiro entrou.

RECEBIDO = soma dos pagamentos efetivamente REGISTRADOS (`Payment.amount`)
das mesmas comandas fechadas. Mede o que de fato entrou via `Payment`,
independente de bater exatamente com o total vendido daquela comanda
(pode ser menor — pagamento parcial registrado incorretamente — ou
maior — pagamento em duplicidade/troco não registrado no domínio
atual).

MOVIMENTO DE CAIXA = movimentações monetárias reais do caixa
(`CashRegister`/`CashMovement` — abertura, sangria, suprimento,
fechamento). Granularidade de TESOURARIA, não de venda: um Recebido
pode entrar em um caixa e nunca ter uma contrapartida de Movimento (ex.:
pagamento fora do caixa) ou vice-versa. Este módulo NÃO calcula
Movimento de Caixa — quem precisar dele usa `services/cash_register.py`
diretamente; não duplicamos essa conta aqui.

Faturamento e Recebido são as DUAS únicas granularidades calculadas por
este módulo, e nunca se misturam: nenhuma função abaixo soma
`Payment.amount` dentro do cálculo de Faturamento, nem soma
`OrderItem.price` dentro do cálculo de Recebido. O KPI card principal
("Faturamento") sempre reflete o que foi VENDIDO — é a métrica correta
pra "como o salão está performando" (item 1 do pedido original). Um
pagamento em duplicidade/a mais numa comanda NUNCA infla o Faturamento
(estruturalmente impossível, já que Faturamento nem lê `Payment`); essa
diferença aparece só como `overpaid_amount` no drill-down de
Faturamento (`RevenueReconciliation`, `schemas/dashboard.py`), nunca
como faturamento adicional.

===========================================================================
FONTE DE VERDADE DE CADA MÉTRICA (documentado aqui porque é o único
lugar que efetivamente calcula cada uma)
===========================================================================

FATURAMENTO = soma de `OrderItem.price` (serviço) + `OrderProductItem.
quantity * unit_price` (produto) de comandas (`Order`) com
`status=CLOSED` e `closed_at` dentro do período — a MESMA fórmula
canônica de `services/order_totals.py::order_total`, já usada por
`close_order`/`OrderRead`/histórico de compras do cliente. Não é
`AppointmentItem.price` (valor no momento da RESERVA, pode nunca virar
venda) nem `Payment.amount` somado direto (existe só pra "Formas de
Pagamento" e pra "Recebido", ver abaixo/acima). Uma comanda só fica
CLOSED depois que `services/orders.py::close_order` confere que o total
pago cobre o total da comanda — ou seja, é dinheiro EFETIVAMENTE
registrado, não estimativa. Esta é EXATAMENTE a mesma fórmula que
`services/extract.py` (Financeiro > Extrato) já usa pra "Receitas" —
escolhida de propósito pra o Dashboard nunca discordar do Financeiro.

Etapa N4.1 — produto vendido (`OrderProductItem`) SEMPRE entra aqui:
é venda real (baixa de estoque de verdade acontece no fechamento, ver
`close_order`), nunca deveria ter ficado fora do Faturamento. Uma
comanda sem NENHUM `OrderItem` (só produto) também entra — a query
nunca faz INNER JOIN direto com `OrderItem`/`OrderProductItem` (ver
`_fetch_period_data`), exatamente pra não sumir comandas assim.

RECEBIDO = soma de `Payment.amount` das mesmas comandas fechadas no
período (mesmo filtro de organização/unidade/data que o Faturamento,
calculado sobre a MESMA população de `_PeriodData`, nunca uma query
separada que poderia divergir de quais comandas entram). Exposto como
KPI de drill-down (`GET /dashboard/kpi/received`) e, dentro do
drill-down de Faturamento, como parte de `RevenueReconciliation` —
nunca como um 7º card na visão geral (mantém os 6 KPIs principais como
definidos, ver item 2 do pedido original).

TICKET MÉDIO = Faturamento / quantidade de comandas fechadas no
período (uma comanda com 2 serviços continua sendo 1 no denominador —
nunca `AppointmentItem`/`OrderItem` isolado, que duplicaria).

CLIENTES ATENDIDOS = clientes únicos (`client_id` distinto) com
alguma comanda FECHADA no período — não clientes com agendamento
(que pode não ter virado venda) nem clientes cadastrados.

AGENDAMENTOS = contagem de `Appointment` cujo `starts_at` cai no
período (qualquer status — inclui cancelado/faltou: é volume de
AGENDA, não de venda).

TAXA DE FALTAS = `NO_SHOW` / agendamentos ELEGÍVEIS, onde elegível =
`Appointment` no período com status != `CANCELLED` (um agendamento
cancelado foi removido ativamente, não é risco de falta).

NOVOS CLIENTES = clientes cuja PRIMEIRA VISITA REAL (`MIN(closed_at)`
entre todas as comandas fechadas do cliente — histórico completo, não
só o período filtrado) cai dentro do período. Nunca
`Client.created_at` (cadastro pode acontecer dias antes do primeiro
atendimento, ou o cliente pode ser cadastrado por outro motivo sem
nunca ter sido atendido).

SERVIÇOS VENDIDOS (Top Serviços) = `OrderItem` de comandas fechadas no
período, agrupado por `service_id` (nome = snapshot mais recente do
próprio `OrderItem.service_name`, mesmo padrão do Extrato — nunca lê
`Service.name` ao vivo, que mudaria histórico se o serviço for
renomeado).

PROFISSIONAL RESPONSÁVEL = `OrderItem.professional_id`/
`professional_name` (quem de fato prestou aquele serviço vendido,
snapshot da comanda) — não `AppointmentItem.professional_id` (poderia
divergir se a comanda for reatribuída, embora hoje não haja UI pra
isso; o dado correto pra "quem gerou a venda" é sempre o da comanda).

FORMAS DE PAGAMENTO = `Payment.amount` agrupado por `Payment.method`
(bucketizado em 5 categorias — ver `PaymentMethodBucket`), das comandas
fechadas no período. Junto com RECEBIDO (acima), são as duas únicas
métricas que usam `Payment` diretamente (é a única tabela com o método
de pagamento e o valor efetivamente registrado como entrada). Na
grande maioria dos casos o total bate exatamente com FATURAMENTO acima
(`close_order` exige `paid_total >= total` da comanda); quando não bate
— pagamento MAIOR que o total (troco não fica registrado no domínio
atual) ou, numa comanda construída fora do fluxo normal, pagamento
MENOR — a diferença fica explícita em `RevenueReconciliation`
(`pending_amount`/`overpaid_amount`), nunca escondida nem absorvida
silenciosamente pelo Faturamento.

FATURAMENTO LÍQUIDO (Etapa N4, `RevenueFeeSummary`) = FATURAMENTO
(acima, `OrderItem`, nunca `Payment`) MENOS a soma de
`fee_amount_snapshot` dos `Payment` em estado `calculated` das mesmas
comandas fechadas do período (mesma leitura de `fee_status` do
Extrato, via `services/payment_fees.py::breakdown_for_display` — nunca
uma segunda interpretação). Pagamentos de cartão em estado
`unconfigured` (sem regra configurada, incluindo pagamentos
HISTÓRICOS anteriores à migration 0034, `fee_status IS NULL`) NUNCA
são tratados como taxa zero: seu valor bruto fica de fora do desconto
(não vira 0% escondido) e é exposto à parte em
`unconfigured_card_amount`; `has_unconfigured_fee` sinaliza quando
isso acontece no período. `known_net_revenue` é sempre calculável (não
é `None`), mas só pode ser apresentado como Líquido definitivo quando
`has_unconfigured_fee` é `False` — com pagamento não configurado no
período, é "Líquido CONHECIDO" (rótulo explícito, nunca um número final
fingido), sempre acompanhado de `unconfigured_card_amount` na
apresentação. Como a taxa só existe no `Payment` (não no `OrderItem`),
este cálculo lê uma população diferente da do Faturamento — mas o
Bruto usado aqui é sempre o MESMO `_revenue(data)` de cima, nunca
recalculado a partir da soma de `Payment.amount`.

RETENÇÃO 90 DIAS: ver docstring de `_compute_retention_rate` — cuidado
extra com censura temporal (não pode confundir "cliente não voltou"
com "cliente ainda não teve tempo de voltar").

HEATMAP: `Appointment.starts_at`, convertido pro fuso da organização
(`Organization.timezone`), agrupado por dia da semana ISO (0=segunda)
e hora cheia. É contagem de AGENDAMENTOS (não "ocupação" — não há
denominador de capacidade/vagas disponíveis implementado ainda).
"""
import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.appointment import Appointment
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CashMovementType,
    OrderStatus,
    PaymentFeeStatus,
    PaymentMethod,
)
from nexasalon_api.models.order import Order, OrderItem, OrderProductItem, Payment
from nexasalon_api.repositories import (
    branch_repo,
    cash_movement_repo,
    organization_repo,
    professional_repo,
    service_repo,
)
from nexasalon_api.schemas.dashboard import (
    AvailableResultSummary,
    ClientPerformanceRow,
    DashboardKpiDetailResponse,
    DashboardKpis,
    DashboardOrderItemRow,
    DashboardOverviewResponse,
    DashboardProfessionalDetailResponse,
    DashboardProfessionalsResponse,
    DashboardServiceDetailResponse,
    DashboardServicesResponse,
    FinancialSummary,
    HeatmapCell,
    KpiKind,
    KpiValue,
    NewVsRecurringPoint,
    PaymentMethodBucket,
    PaymentMethodDetailResponse,
    PaymentMethodPaymentRow,
    PaymentMethodRow,
    ProfessionalPerformanceDetailRow,
    ProfessionalPerformanceRow,
    ProfessionalTopServiceRow,
    RetentionSummary,
    RevenueFeeSummary,
    RevenueReconciliation,
    SeriesPoint,
    ServicePerformanceRow,
    StatusDistributionRow,
    TaxCompetenceBreakdownRow,
    TopServiceRow,
)
from nexasalon_api.services import commissions as commissions_service
from nexasalon_api.services import payment_fees as payment_fees_service
from nexasalon_api.services import tax_rates as tax_rates_service
from nexasalon_api.services import fixed_expenses as fixed_expenses_service

_CENTS = Decimal("0.01")

_RETENTION_WINDOW_DAYS = 90
_TOP_SERVICES_LIMIT = 20
_PROFESSIONALS_LIMIT = 50
# Rodada de interatividade analítica — Ranking de Clientes. A home só
# mostra os 5 primeiros, mas a query já limita um pouco acima (mesmo
# raciocínio de `_PROFESSIONALS_LIMIT`: nunca trazer a organização
# inteira pra depois cortar em memória).
_CLIENTS_LIMIT = 20

_PAYMENT_METHOD_BUCKET: dict[PaymentMethod, PaymentMethodBucket] = {
    PaymentMethod.CREDIT: PaymentMethodBucket.CREDIT,
    PaymentMethod.DEBIT: PaymentMethodBucket.DEBIT,
    PaymentMethod.PIX: PaymentMethodBucket.PIX,
    PaymentMethod.CASH: PaymentMethodBucket.CASH,
    PaymentMethod.LOYALTY_CARD: PaymentMethodBucket.OTHER,
    PaymentMethod.VOUCHER: PaymentMethodBucket.OTHER,
    PaymentMethod.BARTER: PaymentMethodBucket.OTHER,
    PaymentMethod.TRANSFER: PaymentMethodBucket.OTHER,
    PaymentMethod.BANK_SLIP: PaymentMethodBucket.OTHER,
}

# Inverso de `_PAYMENT_METHOD_BUCKET` — quais `PaymentMethod` compõem
# cada fatia do donut (ex.: "other" agrupa 5 métodos distintos). Usado
# pelo drill-down de uma fatia (`get_payment_method_detail`) pra nunca
# duplicar esse mapeamento numa segunda lista que poderia divergir.
_METHODS_BY_BUCKET: dict[PaymentMethodBucket, list[PaymentMethod]] = defaultdict(list)
for _method, _bucket in _PAYMENT_METHOD_BUCKET.items():
    _METHODS_BY_BUCKET[_bucket].append(_method)

_KPI_KEYS = {
    "revenue", "ticket_average", "clients_served", "appointments_count", "no_show_rate", "new_clients",
    # "received" não é um dos cards da visão geral — só é acessível via
    # drill-down próprio (`GET /dashboard/kpi/received`) e embutido no
    # drill-down de `revenue` via `RevenueReconciliation`.
    "received",
    # Redesign BI — os 3 novos cards principais (ver docstring de
    # `DashboardKpis` em `schemas/dashboard.py`).
    "net_revenue", "orders_count", "repeat_rate",
}

# Os 6 cards da NOVA visão geral (mockup "Visão Geral do Seu Salão"),
# nesta ORDEM exata — usado só pra montar `DashboardKpis`/sparklines
# num laço, nunca redefine o que cada chave significa (isso é
# `_bucket_values_for_key`/`_kpi_totals`).
_OVERVIEW_KPI_KEYS = ("revenue", "net_revenue", "orders_count", "new_clients", "ticket_average", "repeat_rate")


@dataclass(frozen=True)
class DashboardFilters:
    organization_id: uuid.UUID
    branch_id: uuid.UUID | None
    date_from: datetime
    date_to: datetime
    compare_from: datetime | None
    compare_to: datetime | None


@dataclass
class _OrderRow:
    order_id: uuid.UUID
    client_id: uuid.UUID
    closed_at: datetime
    total: Decimal  # FATURAMENTO desta comanda — soma de `OrderItem.price`, nunca `Payment`.
    received: Decimal  # RECEBIDO desta comanda — soma de `Payment.amount`, granularidade separada (ver docstring do módulo).
    # BENEFÍCIOS CONCEDIDOS desta comanda — soma de `OrderItem.
    # benefit_amount` (Cartão Fidelidade + Cortesia; Voucher/Permuta NÃO
    # entram aqui, continuam Payment real). Sempre <= `total` por
    # construção (CheckConstraint no banco). Granularidade separada de
    # `received` — nunca a mesma soma: um item pode ter benefício SEM
    # nenhum Payment relacionado (a diferença é dinheiro que nunca
    # existiu, não dinheiro que entrou e saiu).
    benefit: Decimal


@dataclass
class _AppointmentRow:
    starts_at: datetime
    status: AppointmentStatus


@dataclass
class _PeriodData:
    """Linhas cruas de UM período (atual OU comparativo), já filtradas
    por organização/unidade/data no banco — tudo que os KPIs e séries
    precisam calcular vem só daqui, nunca de uma query nova por
    métrica (evita N+1 e garante que todo mundo lê a MESMA população)."""

    orders: list[_OrderRow]
    appointments: list[_AppointmentRow]


def _validate_range(date_from: datetime, date_to: datetime, label: str) -> None:
    if date_to <= date_from:
        raise ValidationDomainError(f"Período {label} inválido: data final deve ser depois da inicial.")


def _resolve_branch(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None) -> None:
    if branch_id is None:
        return
    if branch_repo.get(session, organization_id, branch_id) is None:
        raise NotFoundError("Unidade não encontrada.")


def _fetch_period_data(session: Session, filters: DashboardFilters, date_from: datetime, date_to: datetime) -> _PeriodData:
    # Comandas fechadas no período — SEM join a `OrderItem` (Etapa
    # N4.1: uma comanda só-produto, sem NENHUM `OrderItem`, não pode
    # sumir do Faturamento por causa de um INNER JOIN vazio).
    base_order_stmt = select(Order.id, Order.client_id, Order.closed_at).where(
        Order.organization_id == filters.organization_id,
        Order.status == OrderStatus.CLOSED,
        Order.closed_at >= date_from,
        Order.closed_at < date_to,
    )
    if filters.branch_id is not None:
        base_order_stmt = base_order_stmt.where(Order.branch_id == filters.branch_id)

    # Serviço e produto somados em queries SEPARADAS — mesmo raciocínio
    # de `received_stmt` logo abaixo: nunca um join único entre
    # OrderItem/OrderProductItem/Payment na mesma consulta, que
    # duplicaria por produto cartesiano (ver
    # `test_top_servicos_agrega_por_servico_sem_duplicar_faturamento`).
    # Etapa N4.1 — fórmula CANÔNICA (mesma de
    # `order_totals.py::order_total`/`close_order`): FATURAMENTO =
    # serviços (`OrderItem.price`) + produtos (`OrderProductItem.
    # quantity * unit_price`). Produto vendido é venda real — baixa de
    # estoque de verdade no fechamento — e nunca deveria ter ficado de
    # fora daqui (bug corrigido nesta etapa).
    services_stmt = (
        select(OrderItem.order_id, func.coalesce(func.sum(OrderItem.price), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
        )
        .group_by(OrderItem.order_id)
    )
    products_stmt = (
        select(
            OrderProductItem.order_id,
            func.coalesce(func.sum(OrderProductItem.quantity * OrderProductItem.unit_price), 0),
        )
        .join(Order, Order.id == OrderProductItem.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
        )
        .group_by(OrderProductItem.order_id)
    )
    # BENEFÍCIOS CONCEDIDOS (Etapa "Resultado Disponível — Benefícios") —
    # MESMOS filtros de organização/unidade/status/data de `services_stmt`
    # acima (mesma população de comandas válidas, nunca uma query
    # solta) — soma `OrderItem.benefit_amount` (histórico já
    # snapshotado no fechamento, nunca recalculado a partir do preço
    # atual do catálogo). `SUM` ignora NULL nativamente (itens sem
    # benefício não contribuem, sem precisar de `WHERE` extra);
    # `coalesce` só cobre o caso de a comanda não ter NENHUM item com
    # benefício (grupo vazio).
    benefits_stmt = (
        select(OrderItem.order_id, func.coalesce(func.sum(OrderItem.benefit_amount), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
        )
        .group_by(OrderItem.order_id)
    )
    if filters.branch_id is not None:
        services_stmt = services_stmt.where(Order.branch_id == filters.branch_id)
        products_stmt = products_stmt.where(Order.branch_id == filters.branch_id)
        benefits_stmt = benefits_stmt.where(Order.branch_id == filters.branch_id)
    services_by_order: dict[uuid.UUID, Decimal] = {
        row[0]: Decimal(row[1]) for row in session.execute(services_stmt).all()
    }
    products_by_order: dict[uuid.UUID, Decimal] = {
        row[0]: Decimal(row[1]) for row in session.execute(products_stmt).all()
    }
    benefits_by_order: dict[uuid.UUID, Decimal] = {
        row[0]: Decimal(row[1]) for row in session.execute(benefits_stmt).all()
    }

    # RECEBIDO por comanda — query SEPARADA (mesmo raciocínio acima).
    # Mesmo filtro de organização/unidade/status/data que as demais,
    # pra somar Payment só das comandas que também entram no Faturamento.
    # `reversed_at IS NULL` exclui pagamento estornado por
    # `reopen_order` (comanda reaberta e refechada com outra forma de
    # pagamento) — sem este filtro, o Payment antigo revertido voltaria
    # a somar junto com o novo assim que a Order virasse `CLOSED` de
    # novo (mesmo raciocínio já aplicado em
    # `cash_register.py::build_summary`).
    received_stmt = (
        select(Payment.order_id, func.coalesce(func.sum(Payment.amount), 0))
        .join(Order, Order.id == Payment.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
            Payment.reversed_at.is_(None),
        )
        .group_by(Payment.order_id)
    )
    if filters.branch_id is not None:
        received_stmt = received_stmt.where(Order.branch_id == filters.branch_id)
    received_by_order: dict[uuid.UUID, Decimal] = {
        row[0]: Decimal(row[1]) for row in session.execute(received_stmt).all()
    }

    orders = [
        _OrderRow(
            order_id=row[0], client_id=row[1], closed_at=row[2],
            total=services_by_order.get(row[0], Decimal("0")) + products_by_order.get(row[0], Decimal("0")),
            received=received_by_order.get(row[0], Decimal("0")),
            benefit=benefits_by_order.get(row[0], Decimal("0")),
        )
        for row in session.execute(base_order_stmt).all()
    ]

    appt_stmt = select(Appointment.starts_at, Appointment.status).where(
        Appointment.organization_id == filters.organization_id,
        Appointment.starts_at >= date_from,
        Appointment.starts_at < date_to,
    )
    if filters.branch_id is not None:
        appt_stmt = appt_stmt.where(Appointment.branch_id == filters.branch_id)
    appointments = [_AppointmentRow(starts_at=row[0], status=row[1]) for row in session.execute(appt_stmt).all()]

    return _PeriodData(orders=orders, appointments=appointments)


def _first_visit_by_client(
    session: Session,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    client_ids: set[uuid.UUID],
    cache: dict[tuple[uuid.UUID, uuid.UUID | None, frozenset[uuid.UUID]], dict[uuid.UUID, datetime]] | None = None,
) -> dict[uuid.UUID, datetime]:
    """Primeira comanda fechada de cada cliente, olhando o HISTÓRICO
    COMPLETO (sem filtro de data) — precisa disso pra não classificar
    um cliente antigo como "novo" só porque a venda mais recente dele
    caiu dentro do período filtrado.

    `cache` (item de performance, "quick win 2"): opcional, um dict
    simples criado e descartado por `get_overview`/`get_kpi_detail` a
    cada chamada — NUNCA global/módulo/Redis, só dura o tempo de UMA
    requisição. `_new_clients_count`/`_repeat_client_count`/
    `_repeat_rate`/`_repeat_rate_bucket_values`/`_new_vs_recurring_series`/
    `_new_clients_bucket_values` chamam esta função com o MESMO
    (organization_id, branch_id, client_ids) várias vezes dentro de uma
    única resposta (uma vez por card/sparkline que depende de "primeira
    visita") — sem cache, a mesma query `GROUP BY Order.client_id` roda
    de 5 a 7 vezes por request. Com `cache=None` (comportamento
    default, usado por qualquer chamador que não passe o parâmetro —
    inclusive testes existentes) o resultado é IDÊNTICO a antes: calcula
    direto, sem memoizar nada."""
    if not client_ids:
        return {}
    key = (organization_id, branch_id, frozenset(client_ids))
    if cache is not None and key in cache:
        return cache[key]
    stmt = (
        select(Order.client_id, func.min(Order.closed_at))
        .where(
            Order.organization_id == organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.client_id.in_(client_ids),
        )
        .group_by(Order.client_id)
    )
    if branch_id is not None:
        stmt = stmt.where(Order.branch_id == branch_id)
    result: dict[uuid.UUID, datetime] = dict(session.execute(stmt).all())
    if cache is not None:
        cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Granularidade / buckets — ver docstring de `SeriesPoint` (schemas/dashboard.py)
# pro raciocínio de alinhamento ordinal atual×comparativo.
# ---------------------------------------------------------------------------


def _choose_granularity(date_from: datetime, date_to: datetime) -> str:
    days = (date_to - date_from).total_seconds() / 86400
    if days <= 31:
        return "day"
    if days <= 92:
        return "week"
    return "month"


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_one_month(dt: datetime) -> datetime:
    month_index = dt.month  # 1..12, queremos o PRÓXIMO mês
    year = dt.year + (1 if month_index == 12 else 0)
    month = 1 if month_index == 12 else month_index + 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _generate_buckets(date_from: datetime, date_to: datetime, granularity: str) -> list[tuple[datetime, datetime]]:
    buckets: list[tuple[datetime, datetime]] = []
    cur = date_from
    if granularity == "day":
        step = timedelta(days=1)
        while cur < date_to:
            nxt = min(cur + step, date_to)
            buckets.append((cur, nxt))
            cur = nxt
    elif granularity == "week":
        step = timedelta(days=7)
        while cur < date_to:
            nxt = min(cur + step, date_to)
            buckets.append((cur, nxt))
            cur = nxt
    elif granularity == "month":
        while cur < date_to:
            nxt = min(_add_one_month(cur), date_to)
            buckets.append((cur, nxt))
            cur = nxt
    else:  # pragma: no cover — defensivo, nunca alcançado via `_choose_granularity`.
        raise ValueError(f"granularidade desconhecida: {granularity}")
    return buckets


def _bucket_index(buckets: list[tuple[datetime, datetime]], ts: datetime) -> int | None:
    for i, (start, end) in enumerate(buckets):
        if start <= ts < end:
            return i
    return None


def _align_series(
    current_buckets: list[tuple[datetime, datetime]],
    current_values: list[Decimal],
    comparison_buckets: list[tuple[datetime, datetime]] | None,
    comparison_values: list[Decimal] | None,
) -> list[SeriesPoint]:
    length = len(current_buckets)
    points: list[SeriesPoint] = []
    for i in range(length):
        comp_start = None
        comp_value = None
        if comparison_buckets is not None and i < len(comparison_buckets):
            comp_start = comparison_buckets[i][0]
            comp_value = comparison_values[i] if comparison_values is not None else None
        points.append(
            SeriesPoint(
                index=i,
                current_bucket_start=current_buckets[i][0],
                current_value=current_values[i],
                comparison_bucket_start=comp_start,
                comparison_value=comp_value,
            )
        )
    return points


# ---------------------------------------------------------------------------
# Deltas — comparação estatística (item 14 do pedido).
# ---------------------------------------------------------------------------


def _delta_absolute_percent(current: Decimal, previous: Decimal) -> tuple[Decimal, float | None]:
    absolute = current - previous
    if previous == 0:
        # "sem base de comparação" pro percentual — mas a diferença
        # ABSOLUTA continua semanticamente válida (ex.: 0 -> 5 clientes
        # é "+5", não uma divisão por zero disfarçada de Infinity/NaN).
        return absolute, None
    return absolute, float(absolute / previous * Decimal(100))


def _kpi_value(
    kind: KpiKind, current: Decimal, previous: Decimal | None, *, sparkline: list[Decimal] | None = None
) -> KpiValue:
    if previous is None:
        return KpiValue(kind=kind, value=current, has_comparison=False, sparkline=sparkline)
    absolute, percent = _delta_absolute_percent(current, previous)
    points = None
    if kind == KpiKind.RATE:
        # `current`/`previous` já são percentuais (0-100) — a diferença
        # em PONTOS PERCENTUAIS é uma subtração direta, nunca "variação
        # percentual da taxa" (item explícito: nunca "-33%" pra uma taxa).
        points = float(current) - float(previous)
        percent = None
    return KpiValue(
        kind=kind,
        value=current,
        comparison_value=previous,
        delta_absolute=absolute,
        delta_percent=percent,
        delta_points=points,
        has_comparison=True,
        sparkline=sparkline,
    )


# ---------------------------------------------------------------------------
# Métricas derivadas de `_PeriodData` — usadas tanto pelo overview quanto
# pelo drill-down por KPI, sempre a partir da MESMA função (consistência).
# ---------------------------------------------------------------------------


def _revenue(data: _PeriodData) -> Decimal:
    return sum((row.total for row in data.orders), Decimal("0"))


def _received(data: _PeriodData) -> Decimal:
    """RECEBIDO — soma de `Payment.amount` das mesmas comandas fechadas
    (granularidade separada de Faturamento, ver docstring do módulo).
    Número CRU (transparente) — não capado por comanda; inconsistências
    por comanda aparecem em `_revenue_reconciliation_totals`, nunca
    escondidas aqui."""
    return sum((row.received for row in data.orders), Decimal("0"))


def _benefits_granted(data: _PeriodData) -> Decimal:
    """BENEFÍCIOS CONCEDIDOS — soma de `OrderItem.benefit_amount`
    (Cartão Fidelidade + Cortesia) das mesmas comandas fechadas do
    período (MESMA população de `_revenue`/`_received`, populada em
    `_fetch_period_data`). Usado SÓ pelo painel "Resultado disponível"
    (`_available_result`) — nunca redefine `_revenue`
    (Faturamento Bruto continua o valor econômico cheio, intocado)."""
    return sum((row.benefit for row in data.orders), Decimal("0"))


def _revenue_reconciliation_totals(data: _PeriodData) -> tuple[Decimal, Decimal]:
    """`(pending_total, overpaid_total)` — calculado POR COMANDA, nunca
    a partir da diferença agregada (`_revenue(data) - _received(data)`).
    Uma comanda paga R$50 a mais e outra paga R$50 a menos não podem se
    cancelar num único "diferença = 0": são duas inconsistências
    diferentes, cada uma pertence à sua própria comanda (item explícito
    do pedido: "não misture essas granularidades"). `overpaid_total`
    nunca é somado ao Faturamento em nenhum lugar deste módulo."""
    pending_total = Decimal("0")
    overpaid_total = Decimal("0")
    for row in data.orders:
        diff = row.total - row.received
        if diff > 0:
            pending_total += diff
        elif diff < 0:
            overpaid_total += -diff
    return pending_total, overpaid_total


def _orders_count(data: _PeriodData) -> int:
    return len(data.orders)


def _ticket_average(data: _PeriodData) -> Decimal:
    count = _orders_count(data)
    if count == 0:
        return Decimal("0")
    return (_revenue(data) / count).quantize(Decimal("0.01"))


def _clients_served(data: _PeriodData) -> int:
    return len({row.client_id for row in data.orders})


def _appointments_count(data: _PeriodData) -> int:
    return len(data.appointments)


def _no_show_eligible_and_count(data: _PeriodData) -> tuple[int, int]:
    eligible = [a for a in data.appointments if a.status != AppointmentStatus.CANCELLED]
    no_show = [a for a in eligible if a.status == AppointmentStatus.NO_SHOW]
    return len(eligible), len(no_show)


def _no_show_rate(data: _PeriodData) -> Decimal:
    eligible, no_show = _no_show_eligible_and_count(data)
    if eligible == 0:
        return Decimal("0")
    return (Decimal(no_show) / Decimal(eligible) * Decimal(100)).quantize(Decimal("0.01"))


def _new_clients_count(
    session: Session,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    data: _PeriodData,
    date_from: datetime,
    date_to: datetime,
    first_visit_cache: dict | None = None,
) -> int:
    client_ids = {row.client_id for row in data.orders}
    first_visits = _first_visit_by_client(session, organization_id, branch_id, client_ids, first_visit_cache)
    return sum(1 for fv in first_visits.values() if date_from <= fv < date_to)


# ---------------------------------------------------------------------------
# Redesign BI — Taxa de Retorno (NOVA definição, "olhando pra trás").
#
# Conceitualmente DIFERENTE de `RetentionSummary`/`_compute_retention_rate`
# (que olha PRA FRENTE, a partir da primeira visita, com censura de 90
# dias): aqui a pergunta é "dos clientes atendidos NESTE período,
# quantos JÁ eram clientes antes dele começar" — item explícito do
# pedido, mais intuitivo pra leitura mês-a-mês de um card único, sem
# censura temporal pra explicar. `RetentionSummary` continua existindo
# tal como está (não removida), disponível à parte no overview.
# ---------------------------------------------------------------------------


def _repeat_client_count(
    session: Session,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    data: _PeriodData,
    date_from: datetime,
    first_visit_cache: dict | None = None,
) -> int:
    client_ids = {row.client_id for row in data.orders}
    first_visits = _first_visit_by_client(session, organization_id, branch_id, client_ids, first_visit_cache)
    return sum(1 for cid in client_ids if first_visits.get(cid) is not None and first_visits[cid] < date_from)


def _repeat_rate(
    session: Session,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    data: _PeriodData,
    date_from: datetime,
    first_visit_cache: dict | None = None,
) -> Decimal:
    served = _clients_served(data)
    if served == 0:
        return Decimal("0")
    repeat = _repeat_client_count(session, organization_id, branch_id, data, date_from, first_visit_cache)
    return (Decimal(repeat) / Decimal(served) * Decimal(100)).quantize(Decimal("0.01"))


def _repeat_rate_bucket_values(
    session: Session,
    filters: DashboardFilters,
    buckets: list[tuple[datetime, datetime]],
    data: _PeriodData,
    first_visit_cache: dict | None = None,
) -> list[Decimal]:
    """Trilha (sparkline) da Taxa de Retorno — referência de "já era
    cliente" é o INÍCIO DE CADA BUCKET (não o início do período inteiro,
    diferente do card principal acima): mostra o crescimento orgânico
    de clientes recorrentes ao longo do próprio período, não só um
    número estático repetido. Decorativo (tendência), não recalcula o
    card principal — o valor oficial de "Taxa de Retorno" continua
    vindo só de `_repeat_rate`."""
    client_ids = {row.client_id for row in data.orders}
    first_visit = _first_visit_by_client(session, filters.organization_id, filters.branch_id, client_ids, first_visit_cache)
    served_by_bucket: dict[int, set[uuid.UUID]] = defaultdict(set)
    repeat_by_bucket: dict[int, set[uuid.UUID]] = defaultdict(set)
    for row in data.orders:
        idx = _bucket_index(buckets, row.closed_at)
        if idx is None:
            continue
        served_by_bucket[idx].add(row.client_id)
        fv = first_visit.get(row.client_id)
        if fv is not None and fv < buckets[idx][0]:
            repeat_by_bucket[idx].add(row.client_id)
    return [
        (
            (Decimal(len(repeat_by_bucket.get(i, ()))) / Decimal(len(served_by_bucket[i])) * Decimal(100)).quantize(
                Decimal("0.01")
            )
            if served_by_bucket.get(i)
            else Decimal("0")
        )
        for i in range(len(buckets))
    ]


def _orders_count_series_values(buckets: list[tuple[datetime, datetime]], data: _PeriodData) -> list[Decimal]:
    """"Atendimentos" por bucket — contagem de comandas FECHADAS (nunca
    `Appointment`, ver docstring do módulo: "Atendimentos" no redesign
    BI significa venda efetivamente realizada, não volume de agenda)."""
    totals = [Decimal("0")] * len(buckets)
    for row in data.orders:
        idx = _bucket_index(buckets, row.closed_at)
        if idx is not None:
            totals[idx] += 1
    return totals


def _net_revenue_series_values(
    session: Session, filters: DashboardFilters, buckets: list[tuple[datetime, datetime]], data: _PeriodData
) -> list[Decimal]:
    """Trilha do Faturamento Líquido — Bruto por bucket (reaproveita
    `_revenue_series_values`) menos BENEFÍCIO por bucket (reaproveita
    `_benefit_series_values` — mesmos dados de `_PeriodData` já
    buscados por `_fetch_period_data`, nenhuma query nova) menos taxa
    CONHECIDA por bucket, somada numa ÚNICA passada sobre os `Payment`
    do intervalo inteiro (mesmo raciocínio de custo de
    `_revenue_fee_summary`, nunca uma query por bucket). Pagamento com
    taxa `unconfigured` nunca desconta 0 nem inventa um valor —
    simplesmente não entra na subtração (mesma semântica do card
    principal `known_net_revenue`).

    Benefício de uma comanda entra SÓ no bucket do seu próprio
    `closed_at` (correção de bug confirmado em produção) — nunca o
    total do PERÍODO INTEIRO subtraído de cada bucket, o que inflaria
    artificialmente todos os outros buckets além do que a comanda
    realmente pertence."""
    revenue_totals = _revenue_series_values(buckets, data)
    benefit_totals = _benefit_series_values(buckets, data)
    if not buckets:
        return revenue_totals
    stmt = (
        select(Payment, Order.closed_at)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= buckets[0][0],
            Order.closed_at < buckets[-1][1],
        )
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    fee_totals = [Decimal("0")] * len(buckets)
    for payment, closed_at in session.execute(stmt).all():
        idx = _bucket_index(buckets, closed_at)
        if idx is None:
            continue
        breakdown = payment_fees_service.breakdown_for_display(payment)
        if breakdown.fee_status == PaymentFeeStatus.CALCULATED:
            fee_totals[idx] += breakdown.fee_amount or Decimal("0")
    return [revenue_totals[i] - benefit_totals[i] - fee_totals[i] for i in range(len(buckets))]


# ---------------------------------------------------------------------------
# Retenção 90 dias.
# ---------------------------------------------------------------------------


def _compute_retention_rate(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None, date_from: datetime, date_to: datetime
) -> tuple[float | None, int, int]:
    """Dos clientes cuja PRIMEIRA visita caiu no período, quantos
    tiveram uma segunda comanda fechada dentro de 90 dias seguidos.

    Censura temporal (item explícito do pedido): um cliente cuja
    primeira visita foi há menos de 90 dias da data ATUAL (o momento
    real em que o Dashboard está sendo consultado — nunca `date_to`,
    que pode ser uma data futura dentro do período escolhido) ainda não
    teve a chance completa de voltar. Esse cliente fica de FORA do
    denominador (não conta como "não retornou") até completar a
    janela — ver `RetentionSummary.note`, sempre devolvida junto pra
    documentar a limitação em vez de inventar o número."""
    first_visit_stmt = (
        select(Order.client_id, func.min(Order.closed_at))
        .where(Order.organization_id == organization_id, Order.status == OrderStatus.CLOSED)
    )
    if branch_id is not None:
        first_visit_stmt = first_visit_stmt.where(Order.branch_id == branch_id)
    first_visit_stmt = first_visit_stmt.group_by(Order.client_id)

    now = datetime.now(timezone.utc)
    censor_boundary = now - timedelta(days=_RETENTION_WINDOW_DAYS)

    eligible: dict[uuid.UUID, datetime] = {
        client_id: first_visit
        for client_id, first_visit in session.execute(first_visit_stmt).all()
        if date_from <= first_visit < date_to and first_visit <= censor_boundary
    }
    if not eligible:
        return None, 0, 0

    visits_stmt = select(Order.client_id, Order.closed_at).where(
        Order.organization_id == organization_id,
        Order.status == OrderStatus.CLOSED,
        Order.client_id.in_(eligible.keys()),
    )
    if branch_id is not None:
        visits_stmt = visits_stmt.where(Order.branch_id == branch_id)
    visits_by_client: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    for client_id, closed_at in session.execute(visits_stmt).all():
        visits_by_client[client_id].append(closed_at)

    returned = 0
    for client_id, first_visit in eligible.items():
        window_end = first_visit + timedelta(days=_RETENTION_WINDOW_DAYS)
        if any(first_visit < v <= window_end for v in visits_by_client.get(client_id, [])):
            returned += 1

    rate = round(returned / len(eligible) * 100, 2)
    return rate, len(eligible), returned


def _retention_summary(
    session: Session, filters: DashboardFilters
) -> RetentionSummary:
    rate, eligible, returned = _compute_retention_rate(
        session, filters.organization_id, filters.branch_id, filters.date_from, filters.date_to
    )
    comparison_rate = None
    has_comparison = filters.compare_from is not None and filters.compare_to is not None
    if has_comparison:
        comparison_rate, _, _ = _compute_retention_rate(
            session, filters.organization_id, filters.branch_id, filters.compare_from, filters.compare_to
        )

    if eligible == 0:
        note = (
            "Nenhum cliente elegível: ou não houve primeira visita real nesse período, ou os clientes que "
            "visitaram pela primeira vez ainda não completaram 90 dias até hoje para provar se retornam."
        )
    else:
        note = (
            "Considera só clientes cuja primeira visita já completou 90 dias até hoje — quem visitou "
            "recentemente e ainda não teve a janela completa não entra no denominador nem é contado como "
            "'não retornou'."
        )

    delta_points = None
    if rate is not None and comparison_rate is not None:
        delta_points = round(rate - comparison_rate, 2)

    return RetentionSummary(
        eligible_clients=eligible,
        returned_clients=returned,
        rate_percent=rate,
        comparison_rate_percent=comparison_rate,
        delta_points=delta_points,
        has_comparison=has_comparison,
        note=note,
    )


# ---------------------------------------------------------------------------
# Top serviços / profissionais / status / formas de pagamento / heatmap —
# cada um é uma agregação SQL própria (não dá pra derivar de `_PeriodData`
# sem perder a granularidade de item/pagamento).
# ---------------------------------------------------------------------------


def _top_services(session: Session, filters: DashboardFilters, date_from: datetime, date_to: datetime) -> dict[uuid.UUID, tuple[str, Decimal, int]]:
    stmt = (
        select(
            OrderItem.service_id,
            func.max(OrderItem.service_name),
            func.sum(OrderItem.price),
            func.count(OrderItem.id),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
        )
        .group_by(OrderItem.service_id)
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    return {row[0]: (row[1], Decimal(row[2]), row[3]) for row in session.execute(stmt).all()}


def _professionals(session: Session, filters: DashboardFilters) -> list[ProfessionalPerformanceRow]:
    stmt = (
        select(
            OrderItem.professional_id,
            func.max(OrderItem.professional_name),
            func.count(OrderItem.id),
            func.sum(OrderItem.price),
            func.count(func.distinct(Order.client_id)),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
        )
        .group_by(OrderItem.professional_id)
        .order_by(func.sum(OrderItem.price).desc())
        .limit(_PROFESSIONALS_LIMIT)
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    rows = []
    for professional_id, name, services_count, revenue, clients_served in session.execute(stmt).all():
        revenue = Decimal(revenue)
        ticket = (revenue / clients_served).quantize(Decimal("0.01")) if clients_served else None
        rows.append(
            ProfessionalPerformanceRow(
                professional_id=professional_id,
                professional_name=name,
                clients_served=clients_served,
                services_count=services_count,
                revenue=revenue,
                ticket_average=ticket,
            )
        )
    return rows


def _top_clients(session: Session, filters: DashboardFilters) -> list[ClientPerformanceRow]:
    """Ranking de Clientes por faturamento — MESMO padrão de
    `_professionals` (agregação SQL própria, `OrderItem.price` de
    comandas fechadas, nunca `OrderProductItem`/produto — consistente
    com Top Serviços/Profissionais). `orders_count` conta comandas
    DISTINTAS (não itens) — um cliente com 1 comanda de 3 serviços
    ainda é "1 atendimento", mesma semântica do KPI "Atendimentos"."""
    stmt = (
        select(
            Order.client_id,
            func.max(Client.name),
            func.sum(OrderItem.price),
            func.count(func.distinct(Order.id)),
            func.max(Order.closed_at),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Client, Client.id == Order.client_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
        )
        .group_by(Order.client_id)
        .order_by(func.sum(OrderItem.price).desc())
        .limit(_CLIENTS_LIMIT)
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    rows = []
    for client_id, name, revenue, orders_count, last_visit in session.execute(stmt).all():
        revenue = Decimal(revenue)
        ticket = (revenue / orders_count).quantize(Decimal("0.01")) if orders_count else None
        rows.append(
            ClientPerformanceRow(
                client_id=client_id,
                client_name=name,
                orders_count=orders_count,
                revenue=revenue,
                ticket_average=ticket,
                last_visit=last_visit,
            )
        )
    return rows


def _status_distribution(data: _PeriodData) -> list[StatusDistributionRow]:
    counts: dict[AppointmentStatus, int] = defaultdict(int)
    for appt in data.appointments:
        counts[appt.status] += 1
    return [StatusDistributionRow(status=status, count=counts.get(status, 0)) for status in AppointmentStatus]


def _payment_methods(session: Session, filters: DashboardFilters) -> list[PaymentMethodRow]:
    # `reversed_at IS NULL` — mesmo raciocínio de `received_stmt` em
    # `_fetch_period_data`: exclui pagamento estornado por
    # `reopen_order`, nunca somado de novo se a comanda for refechada
    # com outra forma de pagamento.
    stmt = (
        select(Payment.method, func.sum(Payment.amount))
        .join(Order, Order.id == Payment.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
            Payment.reversed_at.is_(None),
        )
        .group_by(Payment.method)
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)

    bucket_totals: dict[PaymentMethodBucket, Decimal] = defaultdict(lambda: Decimal("0"))
    for method, amount in session.execute(stmt).all():
        bucket_totals[_PAYMENT_METHOD_BUCKET[method]] += Decimal(amount)

    grand_total = sum(bucket_totals.values(), Decimal("0"))
    rows = []
    for bucket in PaymentMethodBucket:
        amount = bucket_totals.get(bucket, Decimal("0"))
        if amount == 0 and bucket not in bucket_totals:
            continue
        percent = float(amount / grand_total * 100) if grand_total > 0 else 0.0
        rows.append(PaymentMethodRow(bucket=bucket, amount=amount, percent=round(percent, 2)))
    return rows


def _revenue_fee_summary(
    session: Session,
    filters: DashboardFilters,
    gross_revenue: Decimal,
    date_from: datetime,
    date_to: datetime,
    *,
    benefits_granted: Decimal,
) -> RevenueFeeSummary:
    """Etapa N4 — Bruto/Taxa/Líquido do período. `gross_revenue` é
    SEMPRE recebido de fora (= `_revenue(data)`, o mesmo Faturamento de
    sempre) — esta função nunca soma `Payment.amount` pra formar o
    Bruto, só usa `Payment` pra achar a taxa (item explícito do pedido:
    "não calcular bruto somando Payments"). `date_from`/`date_to`
    explícitos (mesmo padrão de `_top_services`) — Etapa BI passou a
    chamar isto também pro período COMPARATIVO (`net_revenue`, o novo
    card de Faturamento Líquido), nunca só o período atual de `filters`.

    Reaproveita `services/payment_fees.py::breakdown_for_display` —
    MESMA função usada pelo Extrato — pra nunca duplicar a
    interpretação de `fee_status` (incl. o caso histórico
    `fee_status IS NULL`, ver `derive_fee_status`).

    `reversed_at IS NULL` — mesmo raciocínio de `received_stmt`/
    `_payment_methods`: pagamento estornado por `reopen_order` nunca
    entra na taxa, mesmo se a comanda for refechada depois.

    `benefits_granted` (Etapa "Benefício por Item" — correção de bug
    confirmado em produção) — SEMPRE recebido de fora (= `_benefits_granted(data)`,
    a MESMA soma usada por `_available_result`, nunca uma segunda
    conta) e subtraído como termo INDEPENDENTE de `known_fee_total`/
    `unconfigured_card_amount`/`has_unconfigured_fee` (esses 3 continuam
    calculados SÓ a partir de `Payment` reais, sem misturar benefício
    com taxa). Sem isso, uma comanda 100% coberta por Fidelidade/
    Cortesia (`payments=[]`) mostrava `known_net_revenue == gross_revenue`
    — Faturamento Líquido idêntico ao Bruto, como se o dinheiro tivesse
    sido recebido, quando na verdade nenhum `Payment` existe."""
    stmt = (
        select(Payment)
        .join(Order, Order.id == Payment.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
            Payment.reversed_at.is_(None),
        )
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    payments = session.execute(stmt).scalars().all()

    known_fee_total = Decimal("0")
    unconfigured_card_amount = Decimal("0")
    has_unconfigured_fee = False
    for payment in payments:
        breakdown = payment_fees_service.breakdown_for_display(payment)
        if breakdown.fee_status == PaymentFeeStatus.CALCULATED:
            known_fee_total += breakdown.fee_amount or Decimal("0")
        elif breakdown.fee_status == PaymentFeeStatus.UNCONFIGURED:
            unconfigured_card_amount += payment.amount
            has_unconfigured_fee = True

    return RevenueFeeSummary(
        gross_revenue=gross_revenue,
        known_fee_total=known_fee_total,
        known_net_revenue=gross_revenue - benefits_granted - known_fee_total,
        unconfigured_card_amount=unconfigured_card_amount,
        has_unconfigured_fee=has_unconfigured_fee,
    )


def _financial_summary(
    session: Session, actor: ActorContext, filters: DashboardFilters, *, received: Decimal, known_fee_total: Decimal
) -> FinancialSummary:
    """"Resumo Financeiro" (Etapa BI) — ver docstring de `FinancialSummary`
    (schemas/dashboard.py) pro raciocínio completo de cada linha.
    `received`/`known_fee_total` são recebidos de fora (já calculados
    por `get_overview`) — esta função nunca soma `Payment`/`OrderItem`
    de novo, só resolve as duas linhas que faltam (comissão, despesa).

    `commissions_calculated` fica `None` quando o ator não tem
    `commissions.view_all`/`commissions.manage` — nunca expõe dado de
    um módulo pro qual o ator não tem escopo, mesmo enxergando o
    Dashboard (permissions independentes)."""
    commissions_calculated = None
    if "commissions.view_all" in actor.permissions or "commissions.manage" in actor.permissions:
        commission_overview = commissions_service.get_overview(
            session, actor, date_from=filters.date_from, date_to=filters.date_to
        )
        commissions_calculated = commission_overview.known_commission_total

    # Mesma definição de "Despesa" já usada no Extrato
    # (`services/extract.py::ExtractSummary.expense_total`) — soma de
    # `CashMovement` tipo WITHDRAWAL (sangria) no período, nunca uma
    # segunda interpretação. `cash_movement_repo.list_for_org` não
    # filtra por unidade (mesma limitação já existente no Extrato hoje
    # — não é uma limitação nova introduzida aqui).
    movements = cash_movement_repo.list_for_org(
        session, filters.organization_id, type=CashMovementType.WITHDRAWAL,
        date_from=filters.date_from, date_to=filters.date_to,
    )
    expenses = sum((m.amount for m in movements), Decimal("0"))

    return FinancialSummary(
        received=received, known_fee_total=known_fee_total, commissions_calculated=commissions_calculated,
        expenses=expenses,
    )


def _empty_available_result() -> AvailableResultSummary:
    return AvailableResultSummary(
        available=False,
        gross_revenue=None,
        benefits_granted=None,
        taxes_provisioned=None,
        tax_breakdown=[],
        has_multiple_tax_rates=False,
        single_tax_rate=None,
        has_unconfigured_tax_rate=False,
        unconfigured_tax_revenue=Decimal("0"),
        commissions=None,
        payment_fees=None,
        variable_costs=None,
        fixed_costs=None,
        fixed_expense_breakdown=[],
        legacy_fixed_costs=Decimal("0"),
        linked_fixed_payments=Decimal("0"),
        unclassified_expenses=Decimal("0"),
        available_result=None,
        available_percent=None,
    )


def _available_result(
    session: Session,
    actor: ActorContext,
    filters: DashboardFilters,
    data: _PeriodData,
    *,
    gross_revenue: Decimal,
    known_fee_total: Decimal,
) -> AvailableResultSummary:
    """Painel "Resultado disponível" — ver docstring completa em
    `schemas/dashboard.py::AvailableResultSummary`. Reaproveita
    `data`/`gross_revenue`/`known_fee_total` já calculados por
    `get_overview` (nenhuma query nova de Faturamento/Taxa aqui).

    Indisponível por inteiro (`available=False`, todo o resto `None`)
    quando o ator não tem `commissions.view_all`/`commissions.manage` —
    ver justificativa na docstring do schema: um total que muda de
    valor dependendo de QUEM está olhando quebraria a premissa de
    indicador auditável."""
    if "commissions.view_all" not in actor.permissions and "commissions.manage" not in actor.permissions:
        return _empty_available_result()

    commission_overview = commissions_service.get_overview(
        session, actor, date_from=filters.date_from, date_to=filters.date_to
    )
    commissions = commission_overview.known_commission_total

    # --- Impostos provisionados: revenue por competência (mês
    # calendário) × alíquota vigente NAQUELA competência ---------------
    months = tax_rates_service.months_between(filters.date_from, filters.date_to)
    revenue_by_month: dict = {m: Decimal("0") for m in months}
    for row in data.orders:
        month_key = row.closed_at.date().replace(day=1)
        if month_key in revenue_by_month:
            revenue_by_month[month_key] += row.total

    resolutions = tax_rates_service.resolve_rates_for_months(session, filters.organization_id, months)

    tax_breakdown: list[TaxCompetenceBreakdownRow] = []
    taxes_provisioned = Decimal("0")
    unconfigured_tax_revenue = Decimal("0")
    has_unconfigured_tax_rate = False
    seen_rates: set[Decimal] = set()
    for month in months:
        revenue = revenue_by_month[month]
        if revenue <= 0:
            continue
        resolution = resolutions[month]
        if resolution.tax_rate is None:
            has_unconfigured_tax_rate = True
            unconfigured_tax_revenue += revenue
            tax_breakdown.append(
                TaxCompetenceBreakdownRow(competence_month=month, revenue=revenue, tax_rate=None, tax_amount=Decimal("0"))
            )
            continue
        tax_amount = (revenue * resolution.tax_rate / Decimal("100")).quantize(_CENTS)
        taxes_provisioned += tax_amount
        seen_rates.add(resolution.tax_rate)
        tax_breakdown.append(
            TaxCompetenceBreakdownRow(
                competence_month=month, revenue=revenue, tax_rate=resolution.tax_rate, tax_amount=tax_amount
            )
        )

    has_multiple_tax_rates = len(seen_rates) > 1
    single_tax_rate = next(iter(seen_rates)) if len(seen_rates) == 1 else None

    # --- Custos variáveis / Despesas fixas / não classificados --------
    # `provisions()` (valor contábil por vencimento real) continua INTACTO
    # e segue sendo a fonte da tela Despesas Fixas/Financeiro — aqui ele só
    # é usado, como já era, pra saber quais (categoria, unidade) já têm
    # provisão no período e não devem cair no fallback `legacy_fixed`.
    provisioned_rows = fixed_expenses_service.provisions(
        session, filters.organization_id, branch_id=filters.branch_id,
        date_from=filters.date_from, date_to=filters.date_to,
    )
    # Visão GERENCIAL (rateio por dias operacionais) — é o que o painel
    # "Resultado disponível" EXIBE e DEDUZ; nunca os dois valores (contábil
    # x rateado) ao mesmo tempo no card, pra não expor dois números
    # conflitantes pro mesmo indicador.
    managerial_rows = fixed_expenses_service.managerial_fixed_costs(
        session, filters.organization_id, branch_id=filters.branch_id,
        date_from=filters.date_from, date_to=filters.date_to,
    )
    fixed_expenses_managerial = sum((row.amount for row in managerial_rows), Decimal("0"))
    nature_totals = cash_movement_repo.sum_withdrawals_by_nature(
        session, filters.organization_id, date_from=filters.date_from, date_to=filters.date_to,
        branch_id=filters.branch_id,
        provisioned_fixed_category_branches={
            (row.financial_category_id, row.branch_id) for row in provisioned_rows
        },
    )

    # Etapa "Resultado Disponível — Benefícios": Cartão Fidelidade e
    # Cortesia reduzem o valor DISPONÍVEL (esse dinheiro não entrou),
    # mesmo o Faturamento Bruto (`gross_revenue`, acima) continuando
    # intocado — nunca redefine `kpis.revenue`/`_revenue()`. Mesma
    # população de comandas de `data` (já filtrada por organização/
    # unidade/período/status=CLOSED em `_fetch_period_data`), snapshot
    # histórico (`OrderItem.benefit_amount`), nunca preço de catálogo.
    benefits_granted = _benefits_granted(data)

    available_result = (
        gross_revenue
        - benefits_granted
        - taxes_provisioned
        - commissions
        - known_fee_total
        - nature_totals["variable"]
        - fixed_expenses_managerial
        - nature_totals["legacy_fixed"]
    )
    available_percent = (
        (available_result / gross_revenue * Decimal("100")).quantize(_CENTS) if gross_revenue > 0 else None
    )

    return AvailableResultSummary(
        available=True,
        gross_revenue=gross_revenue,
        benefits_granted=benefits_granted,
        taxes_provisioned=taxes_provisioned,
        tax_breakdown=tax_breakdown,
        has_multiple_tax_rates=has_multiple_tax_rates,
        single_tax_rate=single_tax_rate,
        has_unconfigured_tax_rate=has_unconfigured_tax_rate,
        unconfigured_tax_revenue=unconfigured_tax_revenue,
        commissions=commissions,
        payment_fees=known_fee_total,
        variable_costs=nature_totals["variable"],
        fixed_costs=fixed_expenses_managerial,
        fixed_expense_breakdown=managerial_rows,
        legacy_fixed_costs=nature_totals["legacy_fixed"],
        linked_fixed_payments=nature_totals["linked_fixed_payments"],
        unclassified_expenses=nature_totals["unclassified"],
        available_result=available_result,
        available_percent=available_percent,
    )


def _heatmap(session: Session, filters: DashboardFilters, org_timezone: str) -> list[HeatmapCell]:
    local_start = func.timezone(org_timezone, Appointment.starts_at)
    weekday_expr = func.extract("isodow", local_start) - 1  # 1..7 (seg..dom) -> 0..6
    hour_expr = func.extract("hour", local_start)
    stmt = (
        select(weekday_expr.label("weekday"), hour_expr.label("hour"), func.count(Appointment.id))
        .where(
            Appointment.organization_id == filters.organization_id,
            Appointment.starts_at >= filters.date_from,
            Appointment.starts_at < filters.date_to,
        )
        .group_by("weekday", "hour")
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Appointment.branch_id == filters.branch_id)
    return [
        HeatmapCell(weekday=int(weekday), hour=int(hour), count=count)
        for weekday, hour, count in session.execute(stmt).all()
    ]


def _new_vs_recurring_series(
    session: Session,
    filters: DashboardFilters,
    buckets: list[tuple[datetime, datetime]],
    data: _PeriodData,
    first_visit_cache: dict | None = None,
) -> list[NewVsRecurringPoint]:
    client_ids = {row.client_id for row in data.orders}
    first_visit = _first_visit_by_client(session, filters.organization_id, filters.branch_id, client_ids, first_visit_cache)

    new_by_bucket: dict[int, set[uuid.UUID]] = defaultdict(set)
    recurring_by_bucket: dict[int, set[uuid.UUID]] = defaultdict(set)
    touched: dict[int, set[uuid.UUID]] = defaultdict(set)

    for row in data.orders:
        idx = _bucket_index(buckets, row.closed_at)
        if idx is None or row.client_id in touched[idx]:
            continue
        touched[idx].add(row.client_id)
        fv = first_visit.get(row.client_id)
        fv_idx = _bucket_index(buckets, fv) if fv is not None else None
        if fv_idx == idx:
            new_by_bucket[idx].add(row.client_id)
        else:
            recurring_by_bucket[idx].add(row.client_id)

    return [
        NewVsRecurringPoint(
            index=i,
            bucket_start=start,
            new_clients=len(new_by_bucket.get(i, ())),
            recurring_clients=len(recurring_by_bucket.get(i, ())),
        )
        for i, (start, _end) in enumerate(buckets)
    ]


def _revenue_series_values(buckets: list[tuple[datetime, datetime]], data: _PeriodData) -> list[Decimal]:
    totals = [Decimal("0")] * len(buckets)
    for row in data.orders:
        idx = _bucket_index(buckets, row.closed_at)
        if idx is not None:
            totals[idx] += row.total
    return totals


def _received_series_values(buckets: list[tuple[datetime, datetime]], data: _PeriodData) -> list[Decimal]:
    """RECEBIDO por bucket — mesmo bucketing por `row.closed_at` que
    `_revenue_series_values` (a comanda é a mesma, o que muda é qual
    campo somamos: `received` em vez de `total`)."""
    totals = [Decimal("0")] * len(buckets)
    for row in data.orders:
        idx = _bucket_index(buckets, row.closed_at)
        if idx is not None:
            totals[idx] += row.received
    return totals


def _benefit_series_values(buckets: list[tuple[datetime, datetime]], data: _PeriodData) -> list[Decimal]:
    """BENEFÍCIOS CONCEDIDOS por bucket — mesmo bucketing por
    `row.closed_at` que `_revenue_series_values`/`_received_series_values`
    (a MESMA comanda, o que muda é qual campo somamos: `benefit` em vez
    de `total`/`received`). `row.benefit` já vem populado por
    `_fetch_period_data` (`benefits_stmt`) — nenhuma query nova aqui,
    só reagrupa o que já foi buscado por comanda em buckets de data.
    Usado por `_net_revenue_series_values` pra nunca "vazar" o
    benefício de uma comanda pro bucket errado (cada comanda entra
    SÓ no bucket do seu próprio `closed_at`, nunca distribuído/rateado
    entre buckets vizinhos)."""
    totals = [Decimal(0)] * len(buckets)
    for row in data.orders:
        idx = _bucket_index(buckets, row.closed_at)
        if idx is not None:
            totals[idx] += row.benefit
    return totals


# ---------------------------------------------------------------------------
# Orquestração pública.
# ---------------------------------------------------------------------------


def _build_filters(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
    compare_from: datetime | None,
    compare_to: datetime | None,
) -> DashboardFilters:
    _validate_range(date_from, date_to, "analisado")
    if (compare_from is None) != (compare_to is None):
        raise ValidationDomainError("Informe compare_from e compare_to juntos, ou nenhum dos dois.")
    if compare_from is not None and compare_to is not None:
        _validate_range(compare_from, compare_to, "comparativo")
    _resolve_branch(session, actor.organization_id, branch_id)
    return DashboardFilters(
        organization_id=actor.organization_id,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        compare_from=compare_from,
        compare_to=compare_to,
    )


def _bucket_values_for_key(
    session: Session,
    filters: DashboardFilters,
    key: str,
    period_data: _PeriodData | None,
    bucket_list: list[tuple[datetime, datetime]] | None,
    first_visit_cache: dict | None = None,
) -> list[Decimal] | None:
    """Dispatcher ÚNICO "chave -> série por bucket", reaproveitado tanto
    por `get_overview` (sparkline de cada card, só período ATUAL) quanto
    por `get_kpi_detail` (série completa do drill-down, atual+
    comparativo) — nunca duas implementações da mesma lógica de bucket
    por chave."""
    if period_data is None or bucket_list is None:
        return None
    if key == "revenue":
        return _revenue_series_values(bucket_list, period_data)
    if key == "received":
        return _received_series_values(bucket_list, period_data)
    if key in ("clients_served", "appointments_count", "ticket_average", "no_show_rate"):
        return _generic_bucket_values(key, bucket_list, period_data)
    if key == "new_clients":
        return _new_clients_bucket_values(session, filters, bucket_list, period_data, first_visit_cache)
    if key == "orders_count":
        return _orders_count_series_values(bucket_list, period_data)
    if key == "net_revenue":
        return _net_revenue_series_values(session, filters, bucket_list, period_data)
    if key == "repeat_rate":
        return _repeat_rate_bucket_values(session, filters, bucket_list, period_data, first_visit_cache)
    raise AssertionError(key)  # pragma: no cover — `key` já validado contra `_KPI_KEYS`.


def get_overview(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
    compare_from: datetime | None,
    compare_to: datetime | None,
) -> DashboardOverviewResponse:
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=compare_from, compare_to=compare_to,
    )
    org = organization_repo.get(session, actor.organization_id)
    org_timezone = org.timezone if org is not None else "America/Sao_Paulo"

    current = _fetch_period_data(session, filters, filters.date_from, filters.date_to)
    comparison = None
    if filters.compare_from is not None and filters.compare_to is not None:
        comparison = _fetch_period_data(session, filters, filters.compare_from, filters.compare_to)

    granularity = _choose_granularity(filters.date_from, filters.date_to)
    buckets = _generate_buckets(filters.date_from, filters.date_to, granularity)
    comparison_buckets = None
    if comparison is not None:
        comparison_buckets = _generate_buckets(filters.compare_from, filters.compare_to, granularity)

    # Item de performance ("quick win 2") — `_first_visit_by_client` é
    # consultado por até 7 caminhos diferentes abaixo (new_clients,
    # repeat_rate, seus sparklines, new_vs_recurring) para o MESMO
    # (organization_id, branch_id, client_ids) de cada período. Este
    # dict vive só durante esta chamada de `get_overview` — criado aqui,
    # descartado ao retornar, nunca persistido/compartilhado entre
    # requests (ver docstring de `_first_visit_by_client`).
    first_visit_cache: dict = {}

    revenue_current = _revenue(current)
    revenue_previous = _revenue(comparison) if comparison is not None else None
    ticket_previous = _ticket_average(comparison) if comparison is not None else None
    clients_previous = Decimal(_clients_served(comparison)) if comparison is not None else None
    appts_previous = Decimal(_appointments_count(comparison)) if comparison is not None else None
    no_show_previous = _no_show_rate(comparison) if comparison is not None else None
    new_clients_previous = (
        Decimal(
            _new_clients_count(
                session, filters.organization_id, filters.branch_id, comparison,
                filters.compare_from, filters.compare_to, first_visit_cache,
            )
        )
        if comparison is not None
        else None
    )

    # Redesign BI — Faturamento Líquido (`net_revenue`) precisa do
    # MESMO `RevenueFeeSummary` pro período comparativo também (não só
    # o atual, que já era calculado antes) — `_revenue_fee_summary`
    # agora recebe `date_from`/`date_to` explícitos por isso.
    #
    # `benefits_granted` calculado independentemente pra cada período
    # (`_benefits_granted(current)`/`_benefits_granted(comparison)`) —
    # nunca a mesma base pros dois, senão o comparativo ficaria
    # descontando o benefício do período ERRADO.
    fee_summary_current = _revenue_fee_summary(
        session, filters, revenue_current, filters.date_from, filters.date_to,
        benefits_granted=_benefits_granted(current),
    )
    fee_summary_previous = (
        _revenue_fee_summary(
            session, filters, revenue_previous, filters.compare_from, filters.compare_to,
            benefits_granted=_benefits_granted(comparison),
        )
        if comparison is not None and revenue_previous is not None
        else None
    )

    orders_count_current = Decimal(_orders_count(current))
    orders_count_previous = Decimal(_orders_count(comparison)) if comparison is not None else None

    repeat_rate_current = _repeat_rate(
        session, filters.organization_id, filters.branch_id, current, filters.date_from, first_visit_cache
    )
    repeat_rate_previous = (
        _repeat_rate(
            session, filters.organization_id, filters.branch_id, comparison, filters.compare_from, first_visit_cache
        )
        if comparison is not None
        else None
    )

    def _sparkline(key: str) -> list[Decimal] | None:
        return _bucket_values_for_key(session, filters, key, current, buckets, first_visit_cache)

    kpis = DashboardKpis(
        revenue=_kpi_value(KpiKind.CURRENCY, revenue_current, revenue_previous, sparkline=_sparkline("revenue")),
        ticket_average=_kpi_value(
            KpiKind.CURRENCY, _ticket_average(current), ticket_previous, sparkline=_sparkline("ticket_average")
        ),
        clients_served=_kpi_value(KpiKind.COUNT, Decimal(_clients_served(current)), clients_previous),
        appointments_count=_kpi_value(KpiKind.COUNT, Decimal(_appointments_count(current)), appts_previous),
        no_show_rate=_kpi_value(KpiKind.RATE, _no_show_rate(current), no_show_previous),
        new_clients=_kpi_value(
            KpiKind.COUNT,
            Decimal(
                _new_clients_count(
                    session, filters.organization_id, filters.branch_id, current,
                    filters.date_from, filters.date_to, first_visit_cache,
                )
            ),
            new_clients_previous,
            sparkline=_sparkline("new_clients"),
        ),
        net_revenue=_kpi_value(
            KpiKind.CURRENCY,
            fee_summary_current.known_net_revenue,
            fee_summary_previous.known_net_revenue if fee_summary_previous is not None else None,
            sparkline=_sparkline("net_revenue"),
        ),
        orders_count=_kpi_value(
            KpiKind.COUNT, orders_count_current, orders_count_previous, sparkline=_sparkline("orders_count")
        ),
        repeat_rate=_kpi_value(
            KpiKind.RATE, repeat_rate_current, repeat_rate_previous, sparkline=_sparkline("repeat_rate")
        ),
    )

    revenue_series = _align_series(
        buckets,
        _revenue_series_values(buckets, current),
        comparison_buckets,
        _revenue_series_values(comparison_buckets, comparison) if comparison is not None else None,
    )

    top_services_current = _top_services(session, filters, filters.date_from, filters.date_to)
    top_services_previous = (
        _top_services(session, filters, filters.compare_from, filters.compare_to) if comparison is not None else {}
    )
    top_services = sorted(
        (
            TopServiceRow(
                service_id=service_id,
                service_name=name,
                revenue=revenue,
                quantity=quantity,
                comparison_revenue=top_services_previous.get(service_id, (None, None, None))[1]
                if comparison is not None
                else None,
            )
            for service_id, (name, revenue, quantity) in top_services_current.items()
        ),
        key=lambda r: r.revenue,
        reverse=True,
    )[:_TOP_SERVICES_LIMIT]

    return DashboardOverviewResponse(
        date_from=filters.date_from,
        date_to=filters.date_to,
        compare_from=filters.compare_from,
        compare_to=filters.compare_to,
        branch_id=filters.branch_id,
        granularity=granularity,
        kpis=kpis,
        revenue_series=revenue_series,
        top_services=top_services,
        professionals=_professionals(session, filters),
        top_clients=_top_clients(session, filters),
        status_distribution=_status_distribution(current),
        payment_methods=_payment_methods(session, filters),
        new_vs_recurring=_new_vs_recurring_series(session, filters, buckets, current, first_visit_cache),
        retention=_retention_summary(session, filters),
        heatmap=_heatmap(session, filters, org_timezone),
        revenue_fee_summary=fee_summary_current,
        financial_summary=_financial_summary(
            session, actor, filters, received=_received(current), known_fee_total=fee_summary_current.known_fee_total
        ),
        available_result=_available_result(
            session, actor, filters, current,
            gross_revenue=revenue_current, known_fee_total=fee_summary_current.known_fee_total,
        ),
    )


def get_kpi_detail(
    session: Session,
    actor: ActorContext,
    *,
    key: str,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
    compare_from: datetime | None,
    compare_to: datetime | None,
) -> DashboardKpiDetailResponse:
    if key not in _KPI_KEYS:
        raise NotFoundError(f"KPI '{key}' não existe.")
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=compare_from, compare_to=compare_to,
    )
    current = _fetch_period_data(session, filters, filters.date_from, filters.date_to)
    comparison = None
    if filters.compare_from is not None and filters.compare_to is not None:
        comparison = _fetch_period_data(session, filters, filters.compare_from, filters.compare_to)

    granularity = _choose_granularity(filters.date_from, filters.date_to)
    buckets = _generate_buckets(filters.date_from, filters.date_to, granularity)
    comparison_buckets = _generate_buckets(filters.compare_from, filters.compare_to, granularity) if comparison is not None else None

    # Mesmo item de performance de `get_overview` ("quick win 2") — este
    # dict vive só durante esta chamada de `get_kpi_detail`.
    first_visit_cache: dict = {}

    current_values = _bucket_values_for_key(
        session, filters, key, current, buckets, first_visit_cache
    ) or [Decimal("0")] * len(buckets)
    comparison_values = _bucket_values_for_key(session, filters, key, comparison, comparison_buckets, first_visit_cache)
    series = _align_series(buckets, current_values, comparison_buckets, comparison_values)

    kind, current_total, previous_total = _kpi_totals(session, filters, key, current, comparison, first_visit_cache)
    kpi = _kpi_value(kind, current_total, previous_total)

    # Reconciliação Faturamento×Recebido — só no drill-down de `revenue`
    # (item explícito do pedido, granularidade do PERÍODO ATUAL; não faz
    # sentido "comparar" pendência/excedente entre dois períodos, é um
    # instantâneo de conciliação, não uma métrica de tendência).
    reconciliation = None
    if key == "revenue":
        pending_total, overpaid_total = _revenue_reconciliation_totals(current)
        reconciliation = RevenueReconciliation(
            revenue=_revenue(current),
            received=_received(current),
            pending_amount=pending_total,
            overpaid_amount=overpaid_total,
        )

    return DashboardKpiDetailResponse(
        key=key, kpi=kpi, granularity=granularity, series=series, insights=[], reconciliation=reconciliation,
    )


def _generic_bucket_values(key: str, buckets: list[tuple[datetime, datetime]], data: _PeriodData) -> list[Decimal]:
    if key == "clients_served":
        clients_by_bucket: dict[int, set[uuid.UUID]] = defaultdict(set)
        for row in data.orders:
            idx = _bucket_index(buckets, row.closed_at)
            if idx is not None:
                clients_by_bucket[idx].add(row.client_id)
        return [Decimal(len(clients_by_bucket.get(i, ()))) for i in range(len(buckets))]

    if key == "appointments_count":
        counts = [0] * len(buckets)
        for appt in data.appointments:
            idx = _bucket_index(buckets, appt.starts_at)
            if idx is not None:
                counts[idx] += 1
        return [Decimal(c) for c in counts]

    if key == "ticket_average":
        revenue_totals = _revenue_series_values(buckets, data)
        order_counts = [0] * len(buckets)
        for row in data.orders:
            idx = _bucket_index(buckets, row.closed_at)
            if idx is not None:
                order_counts[idx] += 1
        return [
            (revenue_totals[i] / order_counts[i]).quantize(Decimal("0.01")) if order_counts[i] else Decimal("0")
            for i in range(len(buckets))
        ]

    if key == "no_show_rate":
        eligible = [0] * len(buckets)
        no_show = [0] * len(buckets)
        for appt in data.appointments:
            idx = _bucket_index(buckets, appt.starts_at)
            if idx is None or appt.status == AppointmentStatus.CANCELLED:
                continue
            eligible[idx] += 1
            if appt.status == AppointmentStatus.NO_SHOW:
                no_show[idx] += 1
        return [
            (Decimal(no_show[i]) / Decimal(eligible[i]) * Decimal(100)).quantize(Decimal("0.01")) if eligible[i] else Decimal("0")
            for i in range(len(buckets))
        ]

    raise AssertionError(key)  # pragma: no cover


def _new_clients_bucket_values(
    session: Session,
    filters: DashboardFilters,
    buckets: list[tuple[datetime, datetime]],
    data: _PeriodData,
    first_visit_cache: dict | None = None,
) -> list[Decimal]:
    client_ids = {row.client_id for row in data.orders}
    first_visit = _first_visit_by_client(session, filters.organization_id, filters.branch_id, client_ids, first_visit_cache)
    counts = [0] * len(buckets)
    for fv in first_visit.values():
        idx = _bucket_index(buckets, fv)
        if idx is not None:
            counts[idx] += 1
    return [Decimal(c) for c in counts]


def _kpi_totals(
    session: Session,
    filters: DashboardFilters,
    key: str,
    current: _PeriodData,
    comparison: _PeriodData | None,
    first_visit_cache: dict | None = None,
) -> tuple[KpiKind, Decimal, Decimal | None]:
    if key == "revenue":
        return KpiKind.CURRENCY, _revenue(current), (_revenue(comparison) if comparison else None)
    if key == "received":
        return KpiKind.CURRENCY, _received(current), (_received(comparison) if comparison else None)
    if key == "ticket_average":
        return KpiKind.CURRENCY, _ticket_average(current), (_ticket_average(comparison) if comparison else None)
    if key == "clients_served":
        return (
            KpiKind.COUNT,
            Decimal(_clients_served(current)),
            Decimal(_clients_served(comparison)) if comparison else None,
        )
    if key == "appointments_count":
        return (
            KpiKind.COUNT,
            Decimal(_appointments_count(current)),
            Decimal(_appointments_count(comparison)) if comparison else None,
        )
    if key == "no_show_rate":
        return KpiKind.RATE, _no_show_rate(current), (_no_show_rate(comparison) if comparison else None)
    if key == "new_clients":
        current_total = Decimal(
            _new_clients_count(
                session, filters.organization_id, filters.branch_id, current,
                filters.date_from, filters.date_to, first_visit_cache,
            )
        )
        previous_total = (
            Decimal(
                _new_clients_count(
                    session, filters.organization_id, filters.branch_id, comparison,
                    filters.compare_from, filters.compare_to, first_visit_cache,
                )
            )
            if comparison is not None
            else None
        )
        return KpiKind.COUNT, current_total, previous_total
    if key == "orders_count":
        return (
            KpiKind.COUNT,
            Decimal(_orders_count(current)),
            Decimal(_orders_count(comparison)) if comparison is not None else None,
        )
    if key == "repeat_rate":
        current_total = _repeat_rate(
            session, filters.organization_id, filters.branch_id, current, filters.date_from, first_visit_cache
        )
        previous_total = (
            _repeat_rate(
                session, filters.organization_id, filters.branch_id, comparison, filters.compare_from, first_visit_cache
            )
            if comparison is not None
            else None
        )
        return KpiKind.RATE, current_total, previous_total
    if key == "net_revenue":
        current_summary = _revenue_fee_summary(
            session, filters, _revenue(current), filters.date_from, filters.date_to,
            benefits_granted=_benefits_granted(current),
        )
        previous_summary = (
            _revenue_fee_summary(
                session, filters, _revenue(comparison), filters.compare_from, filters.compare_to,
                benefits_granted=_benefits_granted(comparison),
            )
            if comparison is not None
            else None
        )
        return (
            KpiKind.CURRENCY,
            current_summary.known_net_revenue,
            previous_summary.known_net_revenue if previous_summary is not None else None,
        )
    raise AssertionError(key)  # pragma: no cover


# ---------------------------------------------------------------------------
# Redesign BI — "Ver todos" (Dashboard > Análise de Serviços / Desempenho
# dos Profissionais). Reaproveitam as MESMAS agregações do overview
# (`_top_services`/`_professionals`) — nunca uma segunda fórmula de
# faturamento/produção — só sem o limite de linhas do card resumido e
# com a comparação/comissão embutidas por linha.
# ---------------------------------------------------------------------------


def _has_commissions_scope(actor: ActorContext) -> bool:
    """Dashboard nunca expõe um dado de Comissões pra quem não tem
    escopo NAQUELE módulo, mesmo enxergando o Dashboard (`dashboard.view`
    é uma permission independente) — mesmo raciocínio de
    `_financial_summary`, reaproveitado aqui pros dois drill-downs de
    profissional."""
    return "commissions.view_all" in actor.permissions or "commissions.manage" in actor.permissions


def get_services(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
    compare_from: datetime | None,
    compare_to: datetime | None,
) -> DashboardServicesResponse:
    """Dashboard > Análise de Serviços ("Ver todos" de Top Serviços) —
    MESMA fonte (`_top_services`) da visão geral, sem o corte de
    `_TOP_SERVICES_LIMIT` (a visão geral só mostra os 20 principais; aqui
    é a lista completa do período)."""
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=compare_from, compare_to=compare_to,
    )
    current = _top_services(session, filters, filters.date_from, filters.date_to)
    has_comparison = filters.compare_from is not None and filters.compare_to is not None
    previous = _top_services(session, filters, filters.compare_from, filters.compare_to) if has_comparison else {}

    rows = sorted(
        (
            ServicePerformanceRow(
                service_id=service_id,
                service_name=name,
                quantity=quantity,
                revenue=_kpi_value(KpiKind.CURRENCY, revenue, previous.get(service_id, (None, None, None))[1]),
                ticket_average=(revenue / quantity).quantize(Decimal("0.01")) if quantity else Decimal("0"),
            )
            for service_id, (name, revenue, quantity) in current.items()
        ),
        key=lambda r: r.revenue.value,
        reverse=True,
    )
    return DashboardServicesResponse(
        date_from=filters.date_from, date_to=filters.date_to,
        compare_from=filters.compare_from, compare_to=filters.compare_to,
        rows=rows,
    )


def get_service_detail(
    session: Session,
    actor: ActorContext,
    service_id: uuid.UUID,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
) -> DashboardServiceDetailResponse:
    """Drill-down de UMA linha de "Análise de Serviços" — os `OrderItem`
    exatos que compuseram o faturamento daquele serviço no período,
    mais recentes primeiro. `service_name` do CABEÇALHO usa o snapshot
    do item mais recente (mesmo nome que a listagem de serviços já
    mostrou) — nunca o nome ao vivo de `Service`, que poderia divergir
    se o serviço foi renomeado depois de alguma dessas vendas."""
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=None, compare_to=None,
    )
    service = service_repo.get(session, filters.organization_id, service_id)
    if service is None:
        raise NotFoundError("Serviço não encontrado.")

    stmt = (
        select(OrderItem, Order.id, Order.order_number, Order.closed_at, Client.name)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Client, Client.id == Order.client_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            OrderItem.service_id == service_id,
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
        )
        .order_by(Order.closed_at.desc())
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    rows = session.execute(stmt).all()
    items = [
        DashboardOrderItemRow(
            order_item_id=item.id, order_id=order_id, order_number=order_number, closed_at=closed_at,
            client_name=client_name, service_name=item.service_name, professional_name=item.professional_name,
            price=item.price,
        )
        for item, order_id, order_number, closed_at, client_name in rows
    ]
    service_name = items[0].service_name if items else service.name

    return DashboardServiceDetailResponse(
        service_id=service_id, service_name=service_name,
        date_from=filters.date_from, date_to=filters.date_to, items=items,
    )


def get_payment_method_detail(
    session: Session,
    actor: ActorContext,
    bucket: PaymentMethodBucket,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
) -> PaymentMethodDetailResponse:
    """Drill-down de UMA fatia do donut "Forma de Pagamento" (rodada de
    interatividade analítica) — os `Payment` exatos que compuseram
    aquele bucket no período, mais recentes primeiro. `percent` reusa
    `_payment_methods` (MESMA base de cálculo do card, nunca uma
    segunda fórmula)."""
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=None, compare_to=None,
    )
    methods = _METHODS_BY_BUCKET.get(bucket, [])

    stmt = (
        select(Payment, Order.id, Order.order_number, Order.closed_at, Client.name)
        .join(Order, Order.id == Payment.order_id)
        .join(Client, Client.id == Order.client_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Payment.method.in_(methods),
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
        )
        .order_by(Order.closed_at.desc())
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    rows = session.execute(stmt).all()

    payments = [
        PaymentMethodPaymentRow(
            payment_id=payment.id, order_id=order_id, order_number=order_number, closed_at=closed_at,
            client_name=client_name, method=payment.method, amount=payment.amount,
        )
        for payment, order_id, order_number, closed_at, client_name in rows
    ]
    total_amount = sum((p.amount for p in payments), Decimal("0"))

    all_bucket_rows = _payment_methods(session, filters)
    grand_total = sum((Decimal(r.amount) for r in all_bucket_rows), Decimal("0"))
    percent = float(total_amount / grand_total * 100) if grand_total > 0 else 0.0

    return PaymentMethodDetailResponse(
        bucket=bucket, date_from=filters.date_from, date_to=filters.date_to,
        total_amount=total_amount, payments_count=len(payments),
        percent=round(percent, 2), payments=payments,
    )


def get_professionals_detail(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
    compare_from: datetime | None,
    compare_to: datetime | None,
) -> DashboardProfessionalsResponse:
    """Dashboard > Desempenho dos Profissionais ("Ver todos" do
    ranking) — MESMA fonte (`_professionals`) da visão geral, com
    comparação por linha e Comissão Calculada (reaproveitando
    `services/commissions.py::get_overview`, nunca recalculada pela
    regra atual — ver `_has_commissions_scope`/`FinancialSummary` pro
    raciocínio de permissão)."""
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=compare_from, compare_to=compare_to,
    )
    current_rows = _professionals(session, filters)

    previous_by_id: dict[uuid.UUID, Decimal] = {}
    if filters.compare_from is not None and filters.compare_to is not None:
        comparison_filters = replace(filters, date_from=filters.compare_from, date_to=filters.compare_to)
        previous_by_id = {r.professional_id: r.revenue for r in _professionals(session, comparison_filters)}

    commissions_available = _has_commissions_scope(actor)
    commission_by_professional: dict[uuid.UUID, Decimal] = {}
    if commissions_available:
        commission_overview = commissions_service.get_overview(
            session, actor, date_from=filters.date_from, date_to=filters.date_to
        )
        commission_by_professional = {p.professional_id: p.commission_total for p in commission_overview.professionals}

    rows = [
        ProfessionalPerformanceDetailRow(
            professional_id=r.professional_id,
            professional_name=r.professional_name,
            services_count=r.services_count,
            revenue=_kpi_value(KpiKind.CURRENCY, r.revenue, previous_by_id.get(r.professional_id)),
            ticket_average=r.ticket_average,
            commission_calculated=commission_by_professional.get(r.professional_id) if commissions_available else None,
        )
        for r in current_rows
    ]
    return DashboardProfessionalsResponse(
        date_from=filters.date_from, date_to=filters.date_to,
        compare_from=filters.compare_from, compare_to=filters.compare_to,
        commissions_available=commissions_available, rows=rows,
    )


def get_professional_detail(
    session: Session,
    actor: ActorContext,
    professional_id: uuid.UUID,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
) -> DashboardProfessionalDetailResponse:
    """Drill-down de UM profissional — produção, atendimentos, ticket
    médio, comissão calculada, principais serviços, evolução no
    período e os `OrderItem` exatos (item explícito do pedido)."""
    filters = _build_filters(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=None, compare_to=None,
    )
    professional = professional_repo.get(session, filters.organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")

    stmt = (
        select(OrderItem, Order.id, Order.order_number, Order.closed_at, Client.name)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Client, Client.id == Order.client_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            OrderItem.professional_id == professional_id,
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
        )
        .order_by(Order.closed_at.desc())
    )
    if filters.branch_id is not None:
        stmt = stmt.where(Order.branch_id == filters.branch_id)
    rows = session.execute(stmt).all()

    items = [
        DashboardOrderItemRow(
            order_item_id=item.id, order_id=order_id, order_number=order_number, closed_at=closed_at,
            client_name=client_name, service_name=item.service_name, professional_name=item.professional_name,
            price=item.price,
        )
        for item, order_id, order_number, closed_at, client_name in rows
    ]

    revenue = sum((item.price for item, *_rest in rows), Decimal("0"))
    services_count = len(rows)
    ticket_average = (revenue / services_count).quantize(Decimal("0.01")) if services_count else None

    # Top serviços deste profissional — agregado a partir dos MESMOS
    # itens já buscados acima, nunca uma 2ª query.
    revenue_by_service: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    quantity_by_service: dict[str, int] = defaultdict(int)
    for item, *_rest in rows:
        revenue_by_service[item.service_name] += item.price
        quantity_by_service[item.service_name] += 1
    top_services = sorted(
        (
            ProfessionalTopServiceRow(service_name=name, revenue=rev, quantity=quantity_by_service[name])
            for name, rev in revenue_by_service.items()
        ),
        key=lambda r: r.revenue,
        reverse=True,
    )[:10]

    granularity = _choose_granularity(filters.date_from, filters.date_to)
    buckets = _generate_buckets(filters.date_from, filters.date_to, granularity)
    revenue_by_bucket = [Decimal("0")] * len(buckets)
    for item, _order_id, _order_number, closed_at, _client_name in rows:
        idx = _bucket_index(buckets, closed_at)
        if idx is not None:
            revenue_by_bucket[idx] += item.price
    revenue_series = _align_series(buckets, revenue_by_bucket, None, None)

    # Correção de auditoria: `_has_commissions_scope` (só view_all/manage)
    # negava a comissão até pra um profissional com `commissions.view_own`
    # vendo A PRÓPRIA produção — regra mais restritiva que o módulo de
    # Comissões, que já reconhece view_own. Aqui reaproveitamos a MESMA
    # identidade de autorização de `services/commissions.py::
    # can_view_professional_commissions` (view_all/manage veem qualquer
    # profissional da org; view_own só autoriza o PRÓPRIO
    # `actor.professional_id`, nunca outro) — nunca uma segunda regra.
    commission_available = commissions_service.can_view_professional_commissions(actor, professional_id)
    commission_calculated = None
    if commission_available:
        commission_overview = commissions_service.get_overview(
            session, actor, date_from=filters.date_from, date_to=filters.date_to, professional_id=professional_id
        )
        matching = [p for p in commission_overview.professionals if p.professional_id == professional_id]
        commission_calculated = matching[0].commission_total if matching else Decimal("0")

    return DashboardProfessionalDetailResponse(
        professional_id=professional_id, professional_name=professional.name,
        date_from=filters.date_from, date_to=filters.date_to,
        revenue=revenue, services_count=services_count, ticket_average=ticket_average,
        commission_calculated=commission_calculated, commission_available=commission_available,
        top_services=top_services, granularity=granularity, revenue_series=revenue_series, items=items,
    )
