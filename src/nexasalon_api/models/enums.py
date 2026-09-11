"""
Enums Python espelhando os tipos ENUM nativos do Postgres (criados na
migration 0001). Cada classe usa `str, Enum` para serializar como texto
puro (compatível com Pydantic/JSON) e o `name` do Postgres correspondente
fica documentado no comentário — os tipos são criados via `op.execute`
na migration, não pelo `create_all`, então as colunas do model usam
`postgresql.ENUM(..., create_type=False)`.
"""
from enum import Enum


class OrganizationStatus(str, Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class MembershipStatus(str, Enum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class CommissionType(str, Enum):
    PERCENTAGE = "percentage"
    FIXED = "fixed"


class CommissionStatus(str, Enum):
    """Etapa C2 — Comissão por serviço vendido. Estado ESTRUTURADO
    gravado no `OrderItem` no momento do FECHAMENTO da comanda (nunca
    `NULL` por omissão pra itens fechados depois desta coluna existir)
    pra nunca depender só da nulidade dos snapshots — "sem regra
    configurada NÃO significa comissão zero", mesmo raciocínio de
    `PaymentFeeStatus` (Etapa N3):

      - CALCULATED: existia uma `ProfessionalService` ativa com
        `commission_type`/`commission_value` configurados pra este par
        (profissional, serviço) no momento do fechamento — snapshots
        preenchidos com o valor REAL aplicado, que nunca muda se a
        regra for editada depois.
      - NOT_CONFIGURED: não existia regra (vínculo ausente, inativo, ou
        sem comissão configurada) — os 3 snapshots ficam `NULL` de
        propósito (nunca inventa 0%); o item continua válido, só a
        comissão fica "não configurada".

    `OrderItem` fechados ANTES desta coluna existir (migration 0036)
    têm `commission_status IS NULL` — nunca são migrados/backfilled com
    um valor inventado; quem lê precisa tratar `NULL` como "dado
    histórico sem informação de comissão", nunca fingindo um cálculo."""

    CALCULATED = "calculated"
    NOT_CONFIGURED = "not_configured"


class ScheduleBlockScope(str, Enum):
    PROFESSIONAL = "professional"
    BRANCH = "branch"
    ORGANIZATION = "organization"


class ScheduleBlockType(str, Enum):
    LUNCH = "lunch"
    MEETING = "meeting"
    DAY_OFF = "day_off"
    VACATION = "vacation"
    UNAVAILABLE = "unavailable"
    MAINTENANCE = "maintenance"
    OTHER = "other"


class AppointmentStatus(str, Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    WAITING = "waiting"
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"
    # NÃO é um destino do PATCH genérico de status (ver
    # `appointment_state_machine.py`) — só é atingido AUTOMATICAMENTE ao
    # fechar a Comanda com pagamento registrado (`POST
    # /orders/{id}/close`, `services/orders.py::close_order` ->
    # `appointments_service.mark_paid`). Item "status financeiro não se
    # mistura com status operacional": o usuário nunca marca "Pago" na
    # Agenda à mão, isso reflete um pagamento de fato registrado.
    PAID = "paid"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class OrderProductItemKind(str, Enum):
    """Distingue produto VENDIDO à cliente (`SALE`, comportamento
    original, entra no valor da comanda como venda) de produto
    CONSUMIDO internamente durante o serviço (`CONSUMPTION` — ex.:
    cabelo usado numa progressiva; `unit_price` pode ser `0` quando não
    há cobrança separada, ou um valor explícito quando há — ver
    docstring de `models/order.py::OrderProductItem`). Nunca muda a
    baixa de estoque em si (sempre passa por `stock_movements`, ledger
    append-only) — só o `StockMovementReason` usado no fechamento
    (`SALE` vs `INTERNAL_USE`, ver `services/orders.py::close_order`)."""

    SALE = "sale"
    CONSUMPTION = "consumption"


class OrderStatus(str, Enum):
    """Status da Comanda (`Order`). Fluxo Atendimento -> Comanda ->
    Pagamento -> Pago (Etapa "primeira versão funcional da Comanda"):
    uma comanda nasce OPEN (itens copiados do Appointment, preço
    editável por linha) e vira CLOSED quando o pagamento é registrado
    (`POST /orders/{id}/close`) — o que também promove o `Appointment`
    associado para `paid` automaticamente (ver `services/orders.py`).

    CANCELLED (migration 0024, Etapa F — "Cancelar/Excluir Comanda"):
    só alcançável a partir de OPEN, nunca de CLOSED (uma comanda paga
    exige um fluxo de estorno/reversão, não implementado nesta rodada
    — mesmo raciocínio de `AppointmentStatus.PAID` ser terminal pro
    grafo livre). Uma comanda cancelada sai das operações abertas mas
    nunca é apagada — continua no histórico (`GET /orders`), só some do
    "esta é a comanda ativa deste agendamento" (`get_by_appointment`
    filtra `!= cancelled`), permitindo abrir uma comanda nova pro mesmo
    Appointment depois de cancelar uma criada por engano."""

    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PaymentMethod(str, Enum):
    PIX = "pix"
    CASH = "cash"
    DEBIT = "debit"
    CREDIT = "credit"
    LOYALTY_CARD = "loyalty_card"
    VOUCHER = "voucher"
    BARTER = "barter"  # Permuta
    TRANSFER = "transfer"  # Transferência
    BANK_SLIP = "bank_slip"  # Boleto


class CardBrand(str, Enum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    ELO = "elo"
    AMEX = "amex"
    HIPERCARD = "hipercard"
    OTHER = "other"
    # Sentinela interno de `PaymentFeeRule.card_brand` pra linhas de Pix
    # (que não tem bandeira de verdade) — nunca aceito nem exposto pela
    # API como valor de bandeira; existe só pra manter a coluna `NOT
    # NULL` e a unicidade lógica `(organization, method, card_brand,
    # installments)` simples, sem reabrir a brecha de `NULL <> NULL` em
    # `UNIQUE` (ver docstring de `models/order.py::PaymentFeeRule`).
    # NUNCA usado em `Payment.card_brand` (esse continua `NULL` pra Pix).
    NOT_APPLICABLE = "not_applicable"


class PaymentFeeStatus(str, Enum):
    """Etapa N3 — Taxas de Pagamento. Estado ESTRUTURADO gravado no
    `Payment` no momento da criação (nunca `NULL` por omissão pra
    pagamentos novos) pra nunca depender só da nulidade dos snapshots —
    "cartão sem taxa NÃO significa taxa zero" é uma regra distinta de
    "Pix/Dinheiro não têm taxa", e as duas precisam ser diferenciáveis
    sem ambiguidade, inclusive numa query SQL direta:

      - NOT_APPLICABLE: método sem incidência de taxa nesta etapa
        (pix/dinheiro/outros não-cartão) — `fee_amount_snapshot=0`,
        `net_amount_snapshot=amount`, sempre, nunca ambíguo.
      - CALCULATED: débito/crédito com uma `PaymentFeeRule` encontrada
        no momento da venda — snapshots preenchidos com o valor REAL
        aplicado, que nunca muda se a regra for editada depois.
      - UNCONFIGURED: débito/crédito SEM regra correspondente — os 3
        snapshots ficam `NULL` de propósito (nunca inventa 0%); o
        pagamento continua válido, só o líquido fica "desconhecido".

    Pagamentos criados ANTES desta coluna existir (migration 0034) têm
    `fee_status IS NULL` — nunca são migrados/backfilled com um valor
    inventado; quem lê precisa tratar `NULL` como "dado histórico sem
    informação de taxa" (derivando de `method` só pra decidir "sem
    incidência" vs "desconhecida", nunca fingindo um cálculo)."""

    NOT_APPLICABLE = "not_applicable"
    CALCULATED = "calculated"
    UNCONFIGURED = "unconfigured"


class CashRegisterStatus(str, Enum):
    """Status do Caixa Diário (`CashRegister`). Um caixa ABERTO pode
    receber pagamentos de comanda e movimentações (sangria/suprimento);
    um caixa FECHADO nunca mais recebe nada — ver `services/cash_register.py`."""

    OPEN = "open"
    CLOSED = "closed"


class CashMovementType(str, Enum):
    """Tipos de lançamento manual dentro de um caixa aberto — NÃO inclui
    "payment": pagamentos de comanda já vivem em `payments`
    (`Payment.cash_register_id`), então o resumo/histórico do caixa lê
    `payments` diretamente em vez de duplicar cada pagamento aqui
    também (ver docstring de `services/cash_register.py`).

    `REVERSAL` existe no catálogo pra permitir uma futura tela de
    estorno auditável (item "não apagar sangria/suprimento
    silenciosamente — usar cancelamento/estorno") — nenhum endpoint
    cria este tipo ainda nesta primeira versão."""

    WITHDRAWAL = "withdrawal"  # sangria
    SUPPLY = "supply"  # suprimento
    REVERSAL = "reversal"  # estorno — reservado, sem fluxo implementado ainda


class BrazilianState(str, Enum):
    """UF controlada (item "Estado: prefira UF controlada em vez de
    texto totalmente livre") — 26 estados + DF, sem lógica de negócio
    associada, só evita erro de digitação livre."""

    AC = "AC"
    AL = "AL"
    AP = "AP"
    AM = "AM"
    BA = "BA"
    CE = "CE"
    DF = "DF"
    ES = "ES"
    GO = "GO"
    MA = "MA"
    MT = "MT"
    MS = "MS"
    MG = "MG"
    PA = "PA"
    PB = "PB"
    PR = "PR"
    PE = "PE"
    PI = "PI"
    RJ = "RJ"
    RN = "RN"
    RS = "RS"
    RO = "RO"
    RR = "RR"
    SC = "SC"
    SP = "SP"
    SE = "SE"
    TO = "TO"


class ClientGender(str, Enum):
    """Gênero do cliente — Etapa C.1 ("evolução do cadastro de
    clientes"), item explícito "opcional, opções claras, nunca
    obrigatório pra cadastro ou agendamento". Enum controlado (mesmo
    raciocínio de `BrazilianState`: evita texto livre), mas sem
    NENHUMA lógica de negócio associada — não afeta agendamento, preço,
    disponibilidade nem nenhum outro fluxo; é só um dado de perfil do
    cliente, exibido no cadastro."""

    FEMALE = "female"
    MALE = "male"
    NON_BINARY = "non_binary"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class AppointmentSource(str, Enum):
    INTERNAL = "internal"
    PUBLIC_BOOKING = "public_booking"


class RecurrenceFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"


class RecurrenceStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class AuditAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PermissionEffect(str, Enum):
    GRANT = "grant"
    DENY = "deny"


class AgendaAccessScope(str, Enum):
    """Escopo de VISUALIZAÇÃO/EDIÇÃO de agenda por profissional, além (não
    em vez) das permissions `agenda.view_own`/`agenda.view_all`/
    `agenda.edit` já existentes — ver `models/agenda_access.py`.

    ALL = todos os profissionais da organização, inclusive os criados
    DEPOIS desta configuração (nenhuma linha extra precisa ser inserida
    quando um novo Professional é cadastrado — é o valor padrão de toda
    membership, preservando o comportamento atual). É o que resolve, de
    forma estrutural, o item "aplicar acesso automaticamente a novas
    agendas": não existe lista para manter atualizada.

    SELECTED = só os profissionais com uma linha explícita em
    `membership_agenda_grants` (`can_view`/`can_edit`)."""

    ALL = "all"
    SELECTED = "selected"


class ProductUnit(str, Enum):
    """Unidade de medida do Produto — catálogo fechado (item "prefira
    enum controlado em vez de texto livre", mesma filosofia de
    `BrazilianState`) grande o bastante pra cobrir cosmético/insumo de
    salão sem precisar de tabela de unidades customizável nesta etapa."""

    UNIT = "unit"  # unidade (ex.: escova, esmalte)
    ML = "ml"
    LITER = "liter"
    GRAM = "gram"
    KG = "kg"
    BOX = "box"  # caixa
    PACK = "pack"  # pacote
    METER = "meter"
    DOSE = "dose"
    PAIR = "pair"  # par (ex.: luvas)


class StockMovementDirection(str, Enum):
    """ENTRADA/SAÍDA — a movimentação sempre aumenta (`IN`) ou diminui
    (`OUT`) o estoque de UM produto em UMA unidade; nunca as duas coisas
    na mesma linha (ver `models/stock.py`)."""

    IN = "in"
    OUT = "out"


class StockMovementReason(str, Enum):
    """Motivo da movimentação — catálogo fechado por direção (ver
    `STOCK_MOVEMENT_REASONS_BY_DIRECTION` abaixo). `ADJUSTMENT` existe
    nas duas direções (correção manual, pra mais ou pra menos);
    `INVENTORY_COUNT` também existe nas duas direções mas é reservado
    exclusivamente ao fechamento de um `InventoryCount` (nunca criado
    manualmente por uma rota de movimentação avulsa) — separar os dois
    motivos preserva a distinção "ajuste manual pontual" vs "resultado
    de uma contagem formal de inventário" no histórico."""

    PURCHASE = "purchase"  # compra (IN)
    RETURN = "return"  # devolução (IN)
    ADJUSTMENT = "adjustment"  # ajuste manual (IN ou OUT)
    INVENTORY_COUNT = "inventory_count"  # ajuste de inventário (IN ou OUT)
    TRANSFER_IN = "transfer_in"  # transferência recebida (IN)
    SALE = "sale"  # venda (OUT) — reservado para a integração da Etapa C
    INTERNAL_USE = "internal_use"  # uso interno (OUT)
    DAMAGE = "damage"  # perda/avaria (OUT)
    TRANSFER_OUT = "transfer_out"  # transferência enviada (OUT)


STOCK_MOVEMENT_REASONS_BY_DIRECTION: dict[StockMovementDirection, frozenset[StockMovementReason]] = {
    StockMovementDirection.IN: frozenset(
        {
            StockMovementReason.PURCHASE,
            StockMovementReason.RETURN,
            StockMovementReason.ADJUSTMENT,
            StockMovementReason.INVENTORY_COUNT,
            StockMovementReason.TRANSFER_IN,
        }
    ),
    StockMovementDirection.OUT: frozenset(
        {
            StockMovementReason.SALE,
            StockMovementReason.INTERNAL_USE,
            StockMovementReason.DAMAGE,
            StockMovementReason.ADJUSTMENT,
            StockMovementReason.INVENTORY_COUNT,
            StockMovementReason.TRANSFER_OUT,
        }
    ),
}


class ExpenseNature(str, Enum):
    """Natureza de uma `FinancialCategory` — usada pelo painel "Resultado
    disponível" pra separar (-) Custos variáveis de (-) Despesas fixas.
    A classificação vive na CATEGORIA (não em cada `CashMovement`
    individual, ver `models/finance.py`) — reclassificar uma categoria
    já reclassifica toda movimentação futura vinculada a ela, sem
    precisar editar lançamento a lençamento.

    Categorias antigas (criadas antes desta feature, se algum dia
    migradas de `CashMovement.category` texto livre) e qualquer
    `CashMovement` sem `financial_category_id` (todo o histórico
    anterior a esta feature, ver migration correspondente) NÃO contam
    como FIXED nem VARIABLE — ficam fora do cálculo, expostas
    separadamente como "não classificadas" (nunca uma suposição
    silenciosa)."""

    FIXED = "fixed"
    VARIABLE = "variable"


class FixedExpenseRecurrence(str, Enum):
    """Periodicidade suportada pelos compromissos fixos provisionados."""

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"


class InventoryCountStatus(str, Enum):
    """Um inventário `OPEN` ainda recebe contagens (`counted_quantity`)
    linha a linha; ao fechar (`CLOSED`) vira somente-leitura — as
    movimentações de ajuste já foram geradas e o estoque já reflete a
    contagem, sem novas edições retroativas (mesma filosofia de
    `CashRegister`: fechado nunca recebe novo lançamento)."""

    OPEN = "open"
    CLOSED = "closed"


def pg_enum(enum_cls, name: str):
    """Enum nativo do Postgres, tipo já criado na migration 0001.

    Usa `postgresql.ENUM` (dialect-specific), não o `sa.Enum` genérico:
    o `create_type=False` do `sa.Enum` genérico não é respeitado de forma
    confiável em `Table.create()`/`op.create_table()` — na prática ele
    tenta recriar o tipo e quebra com "already exists". `postgresql.ENUM`
    direto não tem esse problema. `values_callable` garante que o valor
    gravado é `.value` (minúsculo) e não `.name` (maiúsculo) do Enum
    Python.
    """
    from sqlalchemy.dialects.postgresql import ENUM as PgEnum

    return PgEnum(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda x: [e.value for e in x],
    )
