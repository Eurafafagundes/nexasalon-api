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
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from nexasalon_api.models.enums import AppointmentStatus, PaymentMethod
from nexasalon_api.schemas.fixed_expense import FixedExpenseProvisionRow


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
    # Etapa BI (redesign do Dashboard) — trilha do PERÍODO ATUAL apenas
    # (nunca do comparativo — a sparkline é só pra indicar tendência
    # recente, não pra sobrepor duas séries num espaço tão pequeno),
    # mesma granularidade/buckets de `DashboardOverviewResponse.
    # granularity`. `None` só quando o card não tem trilha definida
    # (não usado hoje — todo `KpiValue` da visão geral vem com uma
    # lista, possivelmente de zeros).
    sparkline: list[Decimal] | None = None


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


class ClientPerformanceRow(BaseModel):
    """Uma linha do "Ranking de Clientes" (rodada de interatividade
    analítica) — MESMA granularidade de `ProfessionalPerformanceRow`
    (faturamento de `OrderItem`, nunca `OrderProductItem`/produtos —
    consistente com Top Serviços/Profissionais, que também não incluem
    produto). `orders_count` é nº de comandas FECHADAS deste cliente no
    período (mesma definição de "Atendimentos" usada no resto do
    Dashboard). `last_visit` é o `Order.closed_at` mais recente do
    cliente DENTRO do período filtrado (não a última visita da vida
    inteira dele)."""

    client_id: uuid.UUID
    client_name: str
    orders_count: int
    revenue: Decimal
    ticket_average: Decimal | None
    last_visit: datetime


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


class PaymentMethodPaymentRow(BaseModel):
    """Um `Payment` individual do drill-down de uma fatia do donut —
    SNAPSHOT (nome do cliente no momento do fechamento), mesmo
    raciocínio de `DashboardOrderItemRow`. `method` é o método EXATO
    (não o bucket) — a fatia "Outros" agrupa vários métodos distintos,
    então o drill-down precisa distinguir qual foi usado em cada linha."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    order_number: int
    closed_at: datetime
    client_name: str
    method: PaymentMethod
    amount: Decimal


class PaymentMethodDetailResponse(BaseModel):
    """Drill-down de UMA fatia do donut "Forma de Pagamento" (rodada de
    interatividade analítica). `percent` é a MESMA base de
    `PaymentMethodRow.percent` (percentual sobre o total de pagamentos
    do período, nunca sobre o Faturamento — granularidades diferentes,
    ver docstring de `services/dashboard.py`)."""

    bucket: PaymentMethodBucket
    date_from: datetime
    date_to: datetime
    total_amount: Decimal
    payments_count: int
    percent: float
    payments: list[PaymentMethodPaymentRow]


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
    # --- Redesign BI (mockup "Visão Geral do Seu Salão") — os 6 cards
    # principais da NOVA visão geral passam a ser: revenue (Faturamento
    # Bruto), net_revenue, orders_count (Atendimentos), new_clients,
    # ticket_average, repeat_rate. Os 3 campos ACIMA (`clients_served`/
    # `appointments_count`/`no_show_rate`) são MANTIDOS no contrato —
    # nunca removidos — só saem da grade principal (`config/dashboard.ts`
    # decide o que renderiza); continuam calculáveis/consultáveis via
    # `GET /dashboard/kpi/{key}` pra nunca quebrar um consumidor futuro.
    #
    # `net_revenue` = `revenue_fee_summary.known_net_revenue` (o MESMO
    # número, aqui só embalado como `KpiValue` com comparação — nunca
    # uma segunda conta). `orders_count` = comandas FECHADAS no
    # período ("Atendimentos" no sentido do pedido: venda efetivamente
    # realizada — nunca `Appointment`, que inclui cancelado/faltou).
    # `repeat_rate` = ver docstring de `_repeat_rate` em
    # `services/dashboard.py` — métrica NOVA e DIFERENTE da retenção-90-
    # dias (`RetentionSummary`, mantida como está, disponível à parte).
    net_revenue: KpiValue
    orders_count: KpiValue
    repeat_rate: KpiValue


class RevenueFeeSummary(BaseModel):
    """Etapa N4 — Bruto/Taxa/Líquido do PERÍODO (não de uma comanda só,
    ver `ExtractPaymentBreakdownRow`/`ExtractSaleRow` pro equivalente por
    comanda no Extrato). Mesma semântica financeira dos dois lugares,
    calculada pela MESMA função (`services/payment_fees.py`) — nunca uma
    segunda interpretação de `fee_status` aqui.

    `gross_revenue` é EXATAMENTE `kpis.revenue.value` (soma de
    `OrderItem.price` de comandas fechadas no período) — nunca soma de
    `Payment.amount`; ver docstring "TRÊS CONCEITOS" em
    `services/dashboard.py`. As taxas, por sua vez, só existem nos
    `Payment` — por isso são calculadas a partir de uma população
    diferente (pagamentos, não itens), sem redefinir o que é Bruto.

    `known_net_revenue` = `gross_revenue - known_fee_total`, SEMPRE
    calculável (nunca `None`) — mas só pode ser apresentado como
    "Faturamento Líquido" definitivo quando `has_unconfigured_fee` é
    `False`. Quando `has_unconfigured_fee` é `True`, este mesmo número
    ainda é exibível, só que como "Líquido CONHECIDO" (rótulo
    diferente, nunca fingindo ser o líquido final) — exatamente porque
    ele deliberadamente NÃO desconta nenhuma taxa dos pagamentos em
    `unconfigured_card_amount` (a taxa real deles é desconhecida, nunca
    zero); o frontend é responsável por mostrar `unconfigured_card_amount`
    junto sempre que `has_unconfigured_fee` for `True`, pra nunca deixar
    a impressão de que R$X é definitivo. Mesmo raciocínio do item do
    pedido: "Líquido conhecido: R$9.800" + "R$2.000 aguardando
    configuração de taxa" (nunca só "Líquido: R$9.800")."""

    gross_revenue: Decimal
    known_fee_total: Decimal
    known_net_revenue: Decimal
    unconfigured_card_amount: Decimal
    has_unconfigured_fee: bool


class FinancialSummary(BaseModel):
    """"Resumo Financeiro" do redesign BI — quatro linhas SEPARADAS,
    cada uma já com fonte de verdade própria e reaproveitada de um
    módulo existente; DELIBERADAMENTE sem nenhum "Resultado
    operacional"/"Lucro Líquido" (item explícito do pedido: só compor
    esse número quando o sistema tiver base semântica completa pra ele
    — hoje não tem: falta ao menos impostos e demais custos fixos —
    nunca fingir um P&L completo com 4 componentes parciais).

    `received` = `RevenueReconciliation.received` (soma de
    `Payment.amount`, granularidade "Recebido" — ver "TRÊS CONCEITOS"
    em `services/dashboard.py`). `known_fee_total` = MESMO campo de
    `RevenueFeeSummary` (nunca uma segunda conta de taxa). `expenses` =
    soma de `CashMovement` tipo `withdrawal` no período — MESMA
    definição já usada pelo Extrato (`services/extract.py::
    ExtractSummary.expense_total`), reaproveitada aqui, nunca uma nova
    interpretação de "despesa".

    `commissions_calculated` é `None` quando o ator autenticado não tem
    `commissions.view_all`/`commissions.manage` — Dashboard nunca
    expõe um dado de outro módulo pra quem não tem escopo nele, mesmo
    que veja o Dashboard (`dashboard.view` é uma permission
    independente); o frontend deve mostrar essa linha como
    indisponível/oculta nesse caso, nunca como R$0,00 (que pareceria
    "sem comissão" em vez de "sem permissão para ver")."""

    received: Decimal
    known_fee_total: Decimal
    commissions_calculated: Decimal | None
    expenses: Decimal


class TaxCompetenceBreakdownRow(BaseModel):
    """Uma competência (mês) dentro do período do Dashboard, com o
    faturamento ATRIBUÍDO a ela e a alíquota que estava vigente NAQUELA
    competência — nunca a alíquota atual. Só inclui competências com
    faturamento > 0 (mês sem venda não aparece, mesmo que tenha uma
    linha de alíquota configurada)."""

    competence_month: date
    revenue: Decimal
    tax_rate: Decimal | None  # None = organização nunca configurou alíquota até esta competência.
    tax_amount: Decimal


class AvailableResultSummary(BaseModel):
    """Painel "Resultado disponível" — indicador GERENCIAL (não
    contábil, nunca chamado de "Lucro líquido"): Faturamento Bruto menos
    Impostos provisionados, Comissões, Taxas de pagamento, Custos
    variáveis e Despesas fixas do período selecionado no Dashboard.

    Aditivo ao contrato existente — nunca redefine `kpis.revenue`,
    `kpis.net_revenue`, `revenue_fee_summary` nem `financial_summary`
    (todos continuam com a MESMA semântica de sempre). Cada linha reusa
    uma fonte de verdade já existente:

      - `gross_revenue`: idêntico a `kpis.revenue.value` (nunca uma
        segunda soma de faturamento).
      - `taxes_provisioned`: PROVISÃO gerencial (faturamento aplicável
        × alíquota vigente EM CADA COMPETÊNCIA tocada pelo período —
        ver `tax_breakdown`) — nunca um lançamento de caixa/pagamento
        real. `has_unconfigured_tax_rate=True` quando alguma competência
        com faturamento não tinha nenhuma alíquota configurada até ela
        (nesse caso `unconfigured_tax_revenue` guarda o faturamento
        daquelas competências, nunca tratado como 0% de imposto).
      - `commissions`: idêntico a
        `financial_summary.commissions_calculated` — soma dos
        snapshots de comissão (`OrderItem.commission_amount_snapshot`)
        das comandas fechadas no período, nunca a comissão atual
        recalculada. `None` (o painel inteiro fica indisponível, ver
        `available_result`) quando o ator não tem
        `commissions.view_all`/`commissions.manage` — mostrar "Resultado
        disponível" com uma comissão ausente daria um total
        materialmente diferente pra usuários diferentes no MESMO
        período, o que quebraria a premissa de indicador auditável.
      - `payment_fees`: idêntico a `revenue_fee_summary.known_fee_total`.
      - `variable_costs`: soma de `CashMovement` tipo WITHDRAWAL no
        período cuja categoria tem natureza variável.
      - `fixed_costs`: compromissos de `FixedExpense` provisionados
        pelos vencimentos reais do período; nunca saídas de caixa.
      - `legacy_fixed_costs`: fallback de `CashMovement` FIXED histórico,
        sem vínculo, somente quando não existe provisão equivalente para
        a mesma categoria e unidade no período.
      - `linked_fixed_payments`: pagamentos realizados vinculados a uma
        `FixedExpense`; valor informativo, nunca deduzido novamente.
      - `unclassified_expenses`: WITHDRAWAL sem `financial_category_id`
        (todo o histórico anterior a esta feature, ou lançamento novo
        deixado sem categoria) — NUNCA somado a `variable_costs` nem
        `fixed_costs`; o frontend deve avisar quando > 0.

    `available_result = gross_revenue - taxes_provisioned - commissions
    - payment_fees - variable_costs - fixed_costs - legacy_fixed_costs`.
    `linked_fixed_payments` não participa da fórmula. `available_percent`
    é `None` quando `gross_revenue == 0` (nunca divisão por zero)."""

    available: bool  # False quando o ator não tem permissão de Comissões — todo o resto do payload é None.
    gross_revenue: Decimal | None
    taxes_provisioned: Decimal | None
    tax_breakdown: list[TaxCompetenceBreakdownRow]
    has_multiple_tax_rates: bool
    single_tax_rate: Decimal | None  # preenchido só quando UMA única alíquota se aplicou a todo o período.
    has_unconfigured_tax_rate: bool
    unconfigured_tax_revenue: Decimal
    commissions: Decimal | None
    payment_fees: Decimal | None
    variable_costs: Decimal | None
    fixed_costs: Decimal | None
    fixed_expense_breakdown: list[FixedExpenseProvisionRow]
    legacy_fixed_costs: Decimal
    linked_fixed_payments: Decimal
    unclassified_expenses: Decimal
    available_result: Decimal | None
    available_percent: Decimal | None


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
    # Ranking de Clientes (rodada de interatividade analítica) — MESMO
    # padrão de `professionals` (lista já ordenada por faturamento
    # desc.; o frontend decide quantos exibir na home).
    top_clients: list[ClientPerformanceRow]
    # Mantido no contrato (evita breaking change em consumidores
    # futuros) mesmo sem o card correspondente no frontend a partir da
    # Etapa N4 — ver `services/dashboard.py::_status_distribution`.
    status_distribution: list[StatusDistributionRow]
    payment_methods: list[PaymentMethodRow]
    new_vs_recurring: list[NewVsRecurringPoint]
    retention: RetentionSummary
    heatmap: list[HeatmapCell]
    revenue_fee_summary: RevenueFeeSummary
    financial_summary: FinancialSummary
    available_result: AvailableResultSummary


# ---------------------------------------------------------------------------
# Redesign BI — "Ver todos" (análise detalhada de Serviços/Profissionais).
# ---------------------------------------------------------------------------


class ServicePerformanceRow(BaseModel):
    """Uma linha da tabela "Dashboard > Análise de Serviços". `quantity`
    é "Atendimentos" no sentido do pedido (nº de vezes que ESTE serviço
    foi vendido no período — mesma contagem de `TopServiceRow.quantity`,
    nunca redefinida aqui). `revenue` já vem como `KpiValue` (com
    comparação/variação embutida) pra o frontend reaproveitar o MESMO
    componente de formatação/seta dos 6 cards principais, sem duplicar
    a lógica de "seta pra cima/baixo" numa tabela."""

    service_id: uuid.UUID
    service_name: str
    quantity: int
    revenue: KpiValue
    ticket_average: Decimal


class DashboardServicesResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    compare_from: datetime | None
    compare_to: datetime | None
    rows: list[ServicePerformanceRow]


class DashboardOrderItemRow(BaseModel):
    """Linha de drill-down (Serviço OU Profissional) — sempre o
    SNAPSHOT do `OrderItem` (`service_name`/`professional_name`/
    `price`), nunca uma leitura ao vivo de `Service`/`Professional`
    (mesmo raciocínio do Extrato/Comissões: histórico não muda se o
    cadastro mudar depois)."""

    order_item_id: uuid.UUID
    order_id: uuid.UUID
    order_number: int
    closed_at: datetime
    client_name: str
    service_name: str
    professional_name: str
    price: Decimal


class DashboardServiceDetailResponse(BaseModel):
    service_id: uuid.UUID
    service_name: str
    date_from: datetime
    date_to: datetime
    items: list[DashboardOrderItemRow]


class ProfessionalPerformanceDetailRow(BaseModel):
    """Uma linha de "Dashboard > Desempenho dos Profissionais".
    `commission_calculated` é `None` nas MESMAS condições de
    `FinancialSummary.commissions_calculated` (sem escopo de
    Comissões) — nunca R$0 fingido."""

    professional_id: uuid.UUID
    professional_name: str
    services_count: int
    revenue: KpiValue
    ticket_average: Decimal | None
    commission_calculated: Decimal | None


class DashboardProfessionalsResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    compare_from: datetime | None
    compare_to: datetime | None
    # `False` quando o ator não tem escopo de Comissões — o frontend usa
    # isto (não a nulidade de cada `commission_calculated`) pra decidir
    # se mostra a coluna inteira ou a omite, evitando uma coluna cheia
    # de "—" que pareceria um bug em vez de uma permissão ausente.
    commissions_available: bool
    rows: list[ProfessionalPerformanceDetailRow]


class ProfessionalTopServiceRow(BaseModel):
    service_name: str
    revenue: Decimal
    quantity: int


class DashboardProfessionalDetailResponse(BaseModel):
    professional_id: uuid.UUID
    professional_name: str
    date_from: datetime
    date_to: datetime
    revenue: Decimal
    services_count: int
    ticket_average: Decimal | None
    commission_calculated: Decimal | None
    commission_available: bool
    top_services: list[ProfessionalTopServiceRow]
    granularity: str
    revenue_series: list[SeriesPoint]
    items: list[DashboardOrderItemRow]


class RevenueReconciliation(BaseModel):
    """Só populada no drill-down do KPI `revenue` — compara o que foi
    VENDIDO (Faturamento, `OrderItem`) com o que foi EFETIVAMENTE
    RECEBIDO (`Payment.amount`). Ver docstring de
    `services/dashboard.py` (seção "TRÊS CONCEITOS") pro raciocínio
    completo — granularidades diferentes que nunca se misturam:
    Faturamento não aumenta por causa de pagamento em duplicidade/a
    mais, e uma comanda paga a mais nunca "compensa" outra paga a
    menos no total (`pending_amount`/`overpaid_amount` são somas de
    diferenças calculadas POR COMANDA, não a diferença agregada)."""

    revenue: Decimal
    received: Decimal
    # soma, por comanda, de (total - recebido) quando positivo — venda
    # ainda sem cobertura total de pagamento registrado.
    pending_amount: Decimal
    # soma, por comanda, de (recebido - total) quando positivo —
    # dinheiro registrado além do valor vendido daquela comanda (erro
    # de lançamento/troco não registrado). NUNCA contado como
    # faturamento adicional (Faturamento continua vindo só de
    # `OrderItem`, nunca de `Payment`).
    overpaid_amount: Decimal


class DashboardKpiDetailResponse(BaseModel):
    """Drill-down de UM card. `series` usa o MESMO shape de
    `revenue_series` pra qualquer KPI (não só faturamento) — cada
    métrica tem sua própria série por bucket alinhada atual×comparativo.
    `insights` é reservado pra explicações automáticas futuras (item
    16 — "arquitetura para futuro BI"): sempre `[]` nesta versão, mas
    já faz parte do contrato pra não exigir mudança de schema depois.
    `reconciliation` só vem preenchida quando `key == "revenue"` — ver
    `RevenueReconciliation`."""

    key: str
    kpi: KpiValue
    granularity: str
    series: list[SeriesPoint]
    insights: list[str] = Field(default_factory=list)
    reconciliation: RevenueReconciliation | None = None
