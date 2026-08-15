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
    # Continua sendo um destino manual válido a partir de FINISHED (ver
    # `appointment_state_machine.py`, usado pelo PATCH de status
    # genérico) — MAS na prática, com a Comanda implementada
    # (`services/orders.py`), o caminho normal é essa transição
    # acontecer AUTOMATICAMENTE ao fechar a comanda com pagamento
    # registrado (`POST /orders/{id}/close`), reaproveitando a mesma
    # `next_status`. O usuário não deveria precisar marcar "Pago"
    # manualmente depois de finalizar a comanda.
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


class CardBrand(str, Enum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    ELO = "elo"
    AMEX = "amex"
    HIPERCARD = "hipercard"
    OTHER = "other"


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
