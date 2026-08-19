"""Dashboard/BI — camada analítica central. Item explícito do pedido:
"UMA definição centralizada para cada métrica" (não duas contas de
faturamento diferentes entre Dashboard e Financeiro). Este módulo é a
ÚNICA fonte de verdade de cada KPI abaixo — o frontend só apresenta.

===========================================================================
FONTE DE VERDADE DE CADA MÉTRICA (documentado aqui porque é o único
lugar que efetivamente calcula cada uma)
===========================================================================

FATURAMENTO = soma de `OrderItem.price` de comandas (`Order`) com
`status=CLOSED` e `closed_at` dentro do período. Não é
`AppointmentItem.price` (valor no momento da RESERVA, pode nunca virar
venda) nem `Payment.amount` somado direto (existe só pra "Formas de
Pagamento", ver abaixo). Uma comanda só fica CLOSED depois que
`services/orders.py::close_order` confere que o total pago cobre o
total da comanda — ou seja, é dinheiro EFETIVAMENTE registrado, não
estimativa. Esta é EXATAMENTE a mesma fórmula que `services/extract.py`
(Financeiro > Extrato) já usa pra "Receitas" — escolhida de propósito
pra o Dashboard nunca discordar do Financeiro.

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
fechadas no período. Esta é a ÚNICA métrica que usa `Payment`
diretamente (é a única tabela com o método de pagamento). Na grande
maioria dos casos o total bate exatamente com FATURAMENTO acima
(`close_order` exige `paid_total >= total` da comanda); se algum dia
existir pagamento MAIOR que o total (troco não fica registrado no
domínio atual), os dois números podem divergir por essa diferença —
ver limitação no relatório final.

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.appointment import Appointment
from nexasalon_api.models.enums import AppointmentStatus, OrderStatus, PaymentMethod
from nexasalon_api.models.order import Order, OrderItem, Payment
from nexasalon_api.repositories import branch_repo, organization_repo
from nexasalon_api.schemas.dashboard import (
    DashboardKpiDetailResponse,
    DashboardKpis,
    DashboardOverviewResponse,
    HeatmapCell,
    KpiKind,
    KpiValue,
    NewVsRecurringPoint,
    PaymentMethodBucket,
    PaymentMethodRow,
    ProfessionalPerformanceRow,
    RetentionSummary,
    SeriesPoint,
    StatusDistributionRow,
    TopServiceRow,
)

_RETENTION_WINDOW_DAYS = 90
_TOP_SERVICES_LIMIT = 20
_PROFESSIONALS_LIMIT = 50

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

_KPI_KEYS = {"revenue", "ticket_average", "clients_served", "appointments_count", "no_show_rate", "new_clients"}


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
    total: Decimal


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
    order_stmt = (
        select(
            Order.id,
            Order.client_id,
            Order.closed_at,
            func.coalesce(func.sum(OrderItem.price), 0),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= date_from,
            Order.closed_at < date_to,
        )
        .group_by(Order.id, Order.client_id, Order.closed_at)
    )
    if filters.branch_id is not None:
        order_stmt = order_stmt.where(Order.branch_id == filters.branch_id)
    orders = [
        _OrderRow(order_id=row[0], client_id=row[1], closed_at=row[2], total=Decimal(row[3]))
        for row in session.execute(order_stmt).all()
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
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None, client_ids: set[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Primeira comanda fechada de cada cliente, olhando o HISTÓRICO
    COMPLETO (sem filtro de data) — precisa disso pra não classificar
    um cliente antigo como "novo" só porque a venda mais recente dele
    caiu dentro do período filtrado."""
    if not client_ids:
        return {}
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
    return dict(session.execute(stmt).all())


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


def _kpi_value(kind: KpiKind, current: Decimal, previous: Decimal | None) -> KpiValue:
    if previous is None:
        return KpiValue(kind=kind, value=current, has_comparison=False)
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
    )


# ---------------------------------------------------------------------------
# Métricas derivadas de `_PeriodData` — usadas tanto pelo overview quanto
# pelo drill-down por KPI, sempre a partir da MESMA função (consistência).
# ---------------------------------------------------------------------------


def _revenue(data: _PeriodData) -> Decimal:
    return sum((row.total for row in data.orders), Decimal("0"))


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
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None, data: _PeriodData, date_from: datetime, date_to: datetime
) -> int:
    client_ids = {row.client_id for row in data.orders}
    first_visits = _first_visit_by_client(session, organization_id, branch_id, client_ids)
    return sum(1 for fv in first_visits.values() if date_from <= fv < date_to)


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


def _status_distribution(data: _PeriodData) -> list[StatusDistributionRow]:
    counts: dict[AppointmentStatus, int] = defaultdict(int)
    for appt in data.appointments:
        counts[appt.status] += 1
    return [StatusDistributionRow(status=status, count=counts.get(status, 0)) for status in AppointmentStatus]


def _payment_methods(session: Session, filters: DashboardFilters) -> list[PaymentMethodRow]:
    stmt = (
        select(Payment.method, func.sum(Payment.amount))
        .join(Order, Order.id == Payment.order_id)
        .where(
            Order.organization_id == filters.organization_id,
            Order.status == OrderStatus.CLOSED,
            Order.closed_at >= filters.date_from,
            Order.closed_at < filters.date_to,
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
) -> list[NewVsRecurringPoint]:
    client_ids = {row.client_id for row in data.orders}
    first_visit = _first_visit_by_client(session, filters.organization_id, filters.branch_id, client_ids)

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

    revenue_current = _revenue(current)
    revenue_previous = _revenue(comparison) if comparison is not None else None
    ticket_previous = _ticket_average(comparison) if comparison is not None else None
    clients_previous = Decimal(_clients_served(comparison)) if comparison is not None else None
    appts_previous = Decimal(_appointments_count(comparison)) if comparison is not None else None
    no_show_previous = _no_show_rate(comparison) if comparison is not None else None
    new_clients_previous = (
        Decimal(_new_clients_count(session, filters.organization_id, filters.branch_id, comparison, filters.compare_from, filters.compare_to))
        if comparison is not None
        else None
    )

    kpis = DashboardKpis(
        revenue=_kpi_value(KpiKind.CURRENCY, revenue_current, revenue_previous),
        ticket_average=_kpi_value(KpiKind.CURRENCY, _ticket_average(current), ticket_previous),
        clients_served=_kpi_value(KpiKind.COUNT, Decimal(_clients_served(current)), clients_previous),
        appointments_count=_kpi_value(KpiKind.COUNT, Decimal(_appointments_count(current)), appts_previous),
        no_show_rate=_kpi_value(KpiKind.RATE, _no_show_rate(current), no_show_previous),
        new_clients=_kpi_value(
            KpiKind.COUNT,
            Decimal(_new_clients_count(session, filters.organization_id, filters.branch_id, current, filters.date_from, filters.date_to)),
            new_clients_previous,
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
        status_distribution=_status_distribution(current),
        payment_methods=_payment_methods(session, filters),
        new_vs_recurring=_new_vs_recurring_series(session, filters, buckets, current),
        retention=_retention_summary(session, filters),
        heatmap=_heatmap(session, filters, org_timezone),
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

    def _bucket_values(period_data: _PeriodData | None, bucket_list: list[tuple[datetime, datetime]] | None) -> list[Decimal] | None:
        if period_data is None or bucket_list is None:
            return None
        if key == "revenue":
            return _revenue_series_values(bucket_list, period_data)
        if key in ("clients_served", "appointments_count", "ticket_average", "no_show_rate"):
            return _generic_bucket_values(key, bucket_list, period_data)
        if key == "new_clients":
            return _new_clients_bucket_values(session, filters, bucket_list, period_data)
        raise AssertionError(key)  # pragma: no cover — `key` já validado contra `_KPI_KEYS`.

    current_values = _bucket_values(current, buckets) or [Decimal("0")] * len(buckets)
    comparison_values = _bucket_values(comparison, comparison_buckets)
    series = _align_series(buckets, current_values, comparison_buckets, comparison_values)

    kind, current_total, previous_total = _kpi_totals(session, filters, key, current, comparison)
    kpi = _kpi_value(kind, current_total, previous_total)

    return DashboardKpiDetailResponse(key=key, kpi=kpi, granularity=granularity, series=series, insights=[])


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
    session: Session, filters: DashboardFilters, buckets: list[tuple[datetime, datetime]], data: _PeriodData
) -> list[Decimal]:
    client_ids = {row.client_id for row in data.orders}
    first_visit = _first_visit_by_client(session, filters.organization_id, filters.branch_id, client_ids)
    counts = [0] * len(buckets)
    for fv in first_visit.values():
        idx = _bucket_index(buckets, fv)
        if idx is not None:
            counts[idx] += 1
    return [Decimal(c) for c in counts]


def _kpi_totals(
    session: Session, filters: DashboardFilters, key: str, current: _PeriodData, comparison: _PeriodData | None
) -> tuple[KpiKind, Decimal, Decimal | None]:
    if key == "revenue":
        return KpiKind.CURRENCY, _revenue(current), (_revenue(comparison) if comparison else None)
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
            _new_clients_count(session, filters.organization_id, filters.branch_id, current, filters.date_from, filters.date_to)
        )
        previous_total = (
            Decimal(
                _new_clients_count(
                    session, filters.organization_id, filters.branch_id, comparison, filters.compare_from, filters.compare_to
                )
            )
            if comparison is not None
            else None
        )
        return KpiKind.COUNT, current_total, previous_total
    raise AssertionError(key)  # pragma: no cover
