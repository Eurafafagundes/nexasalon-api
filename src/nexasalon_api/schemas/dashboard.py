"""Schemas do Dashboard/BI — ver `services/dashboard.py` para a
definição/fonte de verdade de cada métrica. Contrato deliberadamente
"denso" (um único `GET /dashboard/overview` devolve tudo que a tela
inicial precisa) porque as seções compartilham a mesma população de
dados (mesmo filtro de período/unidade) e recalcular cada uma em
requests separadas custaria N idas ao banco pra filtrar exatamente a
mesma coisa. O drill-down por KPI (`GET /dashboard/kpi/{key}`) é a
única rota granular — pensada pra crescer com "insights" no futuro sem
precisar mudar o contrato do overview.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from nexasalon_api.models.enums import AppointmentStatus


class KpiKind(str, Enum):
    """Como o frontend deve FORMATAR o valor — nunca inferido do nome
    da métrica no componente (isso é como "Dashboard mostra um número e
    Financeiro mostra outro" acontece: cada tela reinventando a regra).
    `rate` é a única categoria cuja comparação prioriza `delta_points`
    (pontos percentuais) em vez de `delta_percent`."""

    CURRENCY = "currency"
    COUNT = "count"
    RATE = "rate"


class KpiValue(BaseModel):
    """Um KPI com sua comparação já calculada NO BACKEND — o frontend só
    apresenta, nunca recalcula variação (item "cálculos importantes
    devem ficar no backend"). `delta_percent`/`delta_points` são `None`
    sempre que não há uma base de comparação válida (sem período
    comparativo selecionado OU período comparativo com valor-base 0) —
    o frontend deve mostrar "sem base de comparação" nesse caso, nunca
    Infinity/NaN/"—"."""

    kind: KpiKind
    value: Decimal
    comparison_value: Decimal | None = None
    delta_absolute: Decimal | None = None
    # variação percentual — só relevante/preenchida quando kind != RATE.
    delta_percent: float | None = None
    # diferença em PONTOS PERCENTUAIS — só relevante/preenchida quando kind == RATE.
    delta_points: float | None = None
    has_comparison: bool = False


class SeriesPoint(BaseModel):
    """Um ponto alinhado por POSIÇÃO ORDINAL dentro do período (bucket 0
    do período atual pareia com bucket 0 do comparativo, bucket 1 com
    bucket 1, ...) — não por data de calendário, pra que dois períodos
    de duração igual sempre alinhem 1:1 mesmo se um mês tiver mais dias
    que o outro. `bucket_start` é hora LOCAL da organização (sem
    tzinfo — só pra rotular o eixo X/tooltip, nunca reusar como
    timestamp UTC)."""

    index: int
    current_bucket_start: datetime
    current_value: Decimal
    comparison_bucket_start: datetime | None = None
    comparison_value: Decimal | None = None


class TopServiceRow(BaseModel):
    service_id: uuid.UUID
    service_name: str
    revenue: Decimal
    quantity: int
    comparison_revenue: Decimal | None = None


class ProfessionalPerformanceRow(BaseModel):
    professional_id: uuid.UUID
    professional_name: str
    clients_served: int
    services_count: int
    revenue: Decimal
    # `None` só quando `clients_served == 0` (não deveria acontecer numa
    # linha que já existe, mas o contrato fica explícito mesmo assim).
    ticket_average: Decimal | None


class StatusDistributionRow(BaseModel):
    """`status` é sempre o CÓDIGO interno oficial (`AppointmentStatus`)
    — o frontend resolve nome/cor exibidos via
    `useAppointmentStatusConfig()` (personalização por organização já
    implementada), nunca um rótulo vindo do backend."""

    status: AppointmentStatus
    count: int


class PaymentMethodBucket(str, Enum):
    """Agrupamento de exibição do donut "Formas de Pagamento" — os 9
    valores de `PaymentMethod` (`models/enums.py`) viram só 5 fatias
    (item "Crédito/Débito/Pix/Dinheiro/Outros"); `loyalty_card`,
    `voucher`, `barter`, `transfer` e `bank_slip` caem em `other`."""

    CREDIT = "credit"
    DEBIT = "debit"
    PIX = "pix"
    CASH = "cash"
    OTHER = "other"


class PaymentMethodRow(BaseModel):
    bucket: PaymentMethodBucket
    amount: Decimal
    percent: float


class NewVsRecurringPoint(BaseModel):
    index: int
    bucket_start: datetime
    new_clients: int
    recurring_clients: int


class RetentionSummary(BaseModel):
    """Taxa de Retorno em 90 dias — ver docstring de
    `services/dashboard.py::compute_retention` pro raciocínio completo
    de censura temporal. `eligible_clients` só conta quem JÁ tinha 90
    dias completos pra retornar até HOJE (não até `date_to`) — por
    isso pode ser menor que `clients_served` do período."""

    eligible_clients: int
    returned_clients: int
    rate_percent: float | None
    comparison_rate_percent: float | None = None
    delta_points: float | None = None
    has_comparison: bool = False
    note: str


class HeatmapCell(BaseModel):
    """`weekday`: 0=segunda .. 6=domingo (ISO). `hour`: hora cheia local
    (0-23) do início do agendamento. Métrica = contagem de
    `Appointment.starts_at` no bucket — ver docstring do módulo de
    serviço pra por que NÃO é chamado de "ocupação"."""

    weekday: int
    hour: int
    count: int


class DashboardKpis(BaseModel):
    revenue: KpiValue
    ticket_average: KpiValue
    clients_served: KpiValue
    appointments_count: KpiValue
    no_show_rate: KpiValue
    new_clients: KpiValue


class DashboardOverviewResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    compare_from: datetime | None
    compare_to: datetime | None
    branch_id: uuid.UUID | None
    granularity: str  # "day" | "week" | "month"
    kpis: DashboardKpis
    revenue_series: list[SeriesPoint]
    top_services: list[TopServiceRow]
    professionals: list[ProfessionalPerformanceRow]
    status_distribution: list[StatusDistributionRow]
    payment_methods: list[PaymentMethodRow]
    new_vs_recurring: list[NewVsRecurringPoint]
    retention: RetentionSummary
    heatmap: list[HeatmapCell]


class DashboardKpiDetailResponse(BaseModel):
    """Drill-down de UM card. `series` usa o MESMO shape de
    `revenue_series` pra qualquer KPI (não só faturamento) — cada
    métrica tem sua própria série por bucket alinhada atual×comparativo.
    `insights` é reservado pra explicações automáticas futuras (item
    16 — "arquitetura para futuro BI"): sempre `[]` nesta versão, mas
    já faz parte do contrato pra não exigir mudança de schema depois."""

    key: str
    kpi: KpiValue
    granularity: str
    series: list[SeriesPoint]
    insights: list[str] = Field(default_factory=list)
