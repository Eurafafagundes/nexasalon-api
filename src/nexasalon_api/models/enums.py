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


class OrderStatus(str, Enum):
    """Status da Comanda (`Order`). Fluxo Atendimento -> Comanda ->
    Pagamento -> Pago (Etapa "primeira versão funcional da Comanda"):
    uma comanda nasce OPEN (itens copiados do Appointment, preço
    editável por linha) e vira CLOSED quando o pagamento é registrado
    (`POST /orders/{id}/close`) — o que também promove o `Appointment`
    associado para `paid` automaticamente (ver `services/orders.py`).
    Sem estado de cancelamento próprio nesta primeira versão (não
    pedido, e cancelar comanda não é o mesmo domínio que cancelar
    agendamento)."""

    OPEN = "open"
    CLOSED = "closed"


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
