"""Caixa Diário — abertura, sangria/suprimento, resumo e fechamento.

Fluxo: Comanda -> Pagamento -> Caixa -> Fechamento diário. `Payment`
(`models/order.py`) já carrega `cash_register_id` obrigatório — é ele
quem alimenta o resumo por forma de pagamento e o faturamento total
(`build_summary`, chamado tanto pelo detalhe do caixa quanto pela
prévia de fechamento). `CashMovement` guarda só sangria/suprimento
(e, reservado pro futuro, estorno) — pagamento nunca é duplicado numa
segunda tabela.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationDomainError
from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.enums import AuditAction, CashMovementType, CashRegisterStatus, PaymentMethod
from nexasalon_api.repositories import audit_log_repo, cash_movement_repo, cash_register_repo, payment_repo, user_repo
from nexasalon_api.services import cash_register_config as cash_register_config_service
from nexasalon_api.services.availability import effective_timezone


def _resolve_user_name(session: Session, user_id: uuid.UUID) -> str:
    user = user_repo.get(session, user_id)
    return user.name if user is not None else "Usuário removido"


def _get_register_or_404(session: Session, organization_id: uuid.UUID, register_id: uuid.UUID) -> CashRegister:
    register = cash_register_repo.get(session, organization_id, register_id)
    if register is None:
        raise NotFoundError("Caixa não encontrado.")
    return register


def _reload(session: Session, organization_id: uuid.UUID, register_id: uuid.UUID) -> CashRegister:
    session.flush()
    register = cash_register_repo.get(session, organization_id, register_id)
    assert register is not None
    return register


def _operational_date(moment: datetime, tz: ZoneInfo):
    return moment.astimezone(tz).date()


def _stale_open_register(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID, tz: ZoneInfo
) -> CashRegister | None:
    """Caixa aberto desta unidade cuja ABERTURA aconteceu num dia
    operacional anterior a hoje (dia calculado no fuso efetivo da
    organização/unidade — `effective_timezone` — nunca em UTC puro).
    Usado tanto por `open_register` (item "exigir fechamento do caixa
    anterior antes de abrir o de hoje") quanto por
    `assert_operational_prerequisites` (item "bloquear operações se
    existir caixa aberto de dia anterior")."""
    today = _operational_date(datetime.now(timezone.utc), tz)
    for register in cash_register_repo.list_open(session, organization_id):
        if register.branch_id == branch_id and _operational_date(register.created_at, tz) < today:
            return register
    return None


def _previous_day_open_message(register: CashRegister, tz: ZoneInfo) -> str:
    opened_local = register.created_at.astimezone(tz)
    return (
        f"Existe um caixa de {opened_local.strftime('%d/%m/%Y')} ainda aberto. "
        "Feche o caixa anterior antes de iniciar as operações de hoje."
    )


def _assert_role_allowed_to_open_close(
    actor: ActorContext, config: cash_register_config_service.EffectiveConfig
) -> None:
    """Proprietário nunca é bloqueado por este toggle — só os dois
    perfis de sistema explicitamente configuráveis (Administrador/
    Recepção). Perfis Personalizados/Profissional não são afetados
    aqui (continuam regidos só por `finance.manage`, RBAC padrão)."""
    role = (actor.role_name or "").upper()
    if role == "ADMIN" and not config.allow_admin_open_close:
        raise ForbiddenError("As Configurações do Caixa não permitem que Administradores abram/fechem caixa.")
    if role == "RECEPTIONIST" and not config.allow_receptionist_open_close:
        raise ForbiddenError("As Configurações do Caixa não permitem que a Recepção abra/feche caixa.")


def _acquire_branch_open_lock(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> None:
    """Serializa aberturas CONCORRENTES da mesma unidade dentro da
    transação atual (`pg_advisory_xact_lock` — liberado sozinho no
    commit/rollback da request, nunca precisa de unlock explícito).
    Necessário porque, quando não existe NENHUM caixa aberto ainda,
    não há linha nenhuma pra um `SELECT ... FOR UPDATE` travar — sem
    este lock, duas requisições de abertura concorrentes fariam a
    MESMA leitura "nenhum caixa aberto" antes de qualquer uma
    commitar, e as duas abririam caixa (exatamente o cenário que a
    Etapa H pede pra impedir "quando a configuração proibir")."""
    key = f"cash_register_open:{organization_id}:{branch_id}"
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


def assert_operational_prerequisites(
    session: Session, actor: ActorContext, branch_id: uuid.UUID, *, purpose: str
) -> None:
    """Reaproveitado por `services/orders.py::create_order`/`close_order`
    e `services/appointments.py::create_appointment` (Etapa H,
    `Financeiro > Caixa > Configurações do Caixa`). Nunca fecha nada
    sozinho — só barra a operação com uma mensagem acionável."""
    config = cash_register_config_service.get_effective_config(session, actor.organization_id)
    tz = effective_timezone(session, actor.organization_id, branch_id)

    if config.block_if_previous_day_open:
        stale = _stale_open_register(session, actor.organization_id, branch_id, tz)
        if stale is not None:
            raise ConflictError(_previous_day_open_message(stale, tz))

    require_open, action_description = {
        "order": (config.require_open_register_for_order, "criar uma comanda"),
        "payment": (config.require_open_register_for_payment, "receber um pagamento"),
        "appointment": (config.require_open_register_for_appointment, "criar um agendamento"),
    }[purpose]
    if require_open and cash_register_repo.get_open_for_branch(session, actor.organization_id, branch_id) is None:
        raise ValidationDomainError(f"É necessário abrir o caixa desta unidade para {action_description}.")


def open_register(
    session: Session, actor: ActorContext, branch_id: uuid.UUID, initial_amount: Decimal, notes: str | None
) -> CashRegister:
    # Regra desta rodada ("evolução funcional" — Clientes/Financeiro/
    # Caixa), MUDOU da 0014: lá, um MESMO usuário não podia ter dois
    # caixas abertos ao mesmo tempo (mas vários responsáveis diferentes
    # podiam, um cada). O pedido explícito agora é "uma unidade pode
    # ter apenas um caixa aberto por vez" — trocamos o filtro de
    # usuário pra unidade (`branch_id`). Qualquer usuário com
    # `finance.manage` continua podendo abrir/fechar; só não é possível
    # abrir um SEGUNDO caixa na mesma unidade enquanto o primeiro
    # estiver aberto.
    #
    # Etapa H acrescenta, sem mudar o que já existia: (a) os dois
    # toggles acima também podem restringir POR PERFIL quem abre/fecha;
    # (b) "apenas um caixa aberto por unidade" virou configurável (era
    # sempre-ligado); (c) checagem de caixa de dia anterior ainda
    # aberto ANTES de abrir um novo; (d) lock de transação pra duas
    # aberturas concorrentes nunca passarem as duas quando a regra "um
    # por unidade" estiver ligada.
    config = cash_register_config_service.get_effective_config(session, actor.organization_id)
    _assert_role_allowed_to_open_close(actor, config)

    tz = effective_timezone(session, actor.organization_id, branch_id)
    if config.require_close_previous_before_opening_today:
        stale = _stale_open_register(session, actor.organization_id, branch_id, tz)
        if stale is not None:
            raise ConflictError(_previous_day_open_message(stale, tz))

    if config.single_open_register_per_branch:
        _acquire_branch_open_lock(session, actor.organization_id, branch_id)
        existing = cash_register_repo.get_open_for_branch(session, actor.organization_id, branch_id)
        if existing is not None:
            raise ConflictError("Esta unidade já tem um caixa aberto. Feche-o antes de abrir outro.")

    name = _resolve_user_name(session, actor.user_id)
    register = cash_register_repo.create(
        session,
        actor.organization_id,
        branch_id=branch_id,
        opened_by=actor.user_id,
        opened_by_name=name,
        initial_amount=initial_amount,
        opening_notes=notes,
    )
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="cash_register",
        entity_id=register.id,
        action=AuditAction.CREATE,
        new_values={"initial_amount": str(initial_amount), "opened_by": str(actor.user_id), "branch_id": str(branch_id)},
    )
    return _reload(session, actor.organization_id, register.id)


def list_open_registers(session: Session, actor: ActorContext) -> list[CashRegister]:
    return cash_register_repo.list_open(session, actor.organization_id)


def list_registers(
    session: Session,
    actor: ActorContext,
    *,
    status: CashRegisterStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    opened_by: uuid.UUID | None = None,
) -> list[CashRegister]:
    return cash_register_repo.list_for_org(
        session, actor.organization_id, status=status, date_from=date_from, date_to=date_to, opened_by=opened_by
    )


def get_register(session: Session, actor: ActorContext, register_id: uuid.UUID) -> CashRegister:
    return _get_register_or_404(session, actor.organization_id, register_id)


def assert_register_open_and_in_org(session: Session, organization_id: uuid.UUID, register_id: uuid.UUID) -> CashRegister:
    """Reaproveitado por `services/orders.py::close_order` — todo
    pagamento de comanda precisa de um caixa aberto, da mesma
    organização, pra ser concluído (item "pagamento obrigatoriamente
    vinculado ao caixa"). Nunca cria um caixa automaticamente."""
    register = cash_register_repo.get(session, organization_id, register_id)
    if register is None:
        raise NotFoundError("Caixa não encontrado.")
    if register.status != CashRegisterStatus.OPEN:
        raise ValidationDomainError("O caixa selecionado não está aberto.")
    return register


def register_movement(
    session: Session,
    actor: ActorContext,
    register_id: uuid.UUID,
    movement_type: CashMovementType,
    amount: Decimal,
    description: str,
    *,
    category: str | None = None,
    method: PaymentMethod = PaymentMethod.CASH,
) -> CashRegister:
    register = _get_register_or_404(session, actor.organization_id, register_id)
    if register.status != CashRegisterStatus.OPEN:
        raise ValidationDomainError("Só é possível registrar entrada/despesa em um caixa aberto.")

    name = _resolve_user_name(session, actor.user_id)
    movement = cash_movement_repo.create(
        session,
        actor.organization_id,
        cash_register_id=register.id,
        type=movement_type,
        amount=amount,
        description=description,
        category=category,
        method=method,
        created_by=actor.user_id,
        created_by_name=name,
    )
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="cash_movement",
        entity_id=movement.id,
        action=AuditAction.CREATE,
        new_values={
            "type": movement_type.value,
            "amount": str(amount),
            "description": description,
            "category": category,
            "method": method.value,
            "cash_register_id": str(register_id),
        },
    )
    return _reload(session, actor.organization_id, register_id)


@dataclass
class RegisterSummary:
    register: CashRegister
    totals_by_method: dict[PaymentMethod, tuple[Decimal, int]] = field(default_factory=dict)
    total_revenue: Decimal = Decimal("0")  # Faturamento — soma de Payment, qualquer método.
    cash_payments_total: Decimal = Decimal("0")
    # Entradas/Despesas manuais (`CashMovement`), TODOS os métodos —
    # usadas pelos cards "Entradas"/"Saídas" e pelo Extrato. Distintas
    # das versões "_cash_total" abaixo, que só entram no saldo físico.
    supplies_total: Decimal = Decimal("0")
    withdrawals_total: Decimal = Decimal("0")
    supplies_cash_total: Decimal = Decimal("0")
    withdrawals_cash_total: Decimal = Decimal("0")
    expected_cash_balance: Decimal = Decimal("0")
    orders_count: int = 0
    average_ticket: Decimal = Decimal("0")
    # "Entradas" do resumo do caixa (item 6): tudo que entrou, venda ou
    # não, qualquer método — `total_revenue + supplies_total`. "Saídas"
    # é só `withdrawals_total` (não precisa de campo próprio).
    total_entries: Decimal = Decimal("0")
    movements: list[CashMovement] = field(default_factory=list)
    payments: list = field(default_factory=list)


def build_summary(session: Session, organization_id: uuid.UUID, register: CashRegister) -> RegisterSummary:
    """Agregação NUNCA hardcoded a um subconjunto fixo de formas de
    pagamento (item "não deixar métodos de pagamento importantes
    hardcoded") — itera sobre TODO o catálogo `PaymentMethod`, então
    qualquer método novo adicionado ao enum aparece aqui automaticamente,
    mesmo com zero transações.

    Ticket médio (item "definições analíticas"): `total_revenue /
    comandas PAGAS`, nunca por quantidade de pagamentos ou de itens —
    uma comanda com pagamento misto (Pix + Crédito) é UMA venda, conta
    uma vez só (`orders_count` usa `order_id` distintos, não linhas de
    `Payment`)."""
    payments = payment_repo.list_for_register(session, organization_id, register.id)
    movements = cash_movement_repo.list_for_register(session, organization_id, register.id)

    totals_by_method: dict[PaymentMethod, tuple[Decimal, int]] = {m: (Decimal("0"), 0) for m in PaymentMethod}
    total_revenue = Decimal("0")
    cash_payments_total = Decimal("0")
    order_ids: set[uuid.UUID] = set()
    for p in payments:
        total, count = totals_by_method[p.method]
        totals_by_method[p.method] = (total + p.amount, count + 1)
        total_revenue += p.amount
        order_ids.add(p.order_id)
        if p.method == PaymentMethod.CASH:
            cash_payments_total += p.amount

    supplies_total = sum((m.amount for m in movements if m.type == CashMovementType.SUPPLY), Decimal("0"))
    withdrawals_total = sum((m.amount for m in movements if m.type == CashMovementType.WITHDRAWAL), Decimal("0"))
    supplies_cash_total = sum(
        (m.amount for m in movements if m.type == CashMovementType.SUPPLY and m.method == PaymentMethod.CASH),
        Decimal("0"),
    )
    withdrawals_cash_total = sum(
        (m.amount for m in movements if m.type == CashMovementType.WITHDRAWAL and m.method == PaymentMethod.CASH),
        Decimal("0"),
    )

    # Fórmula item "Saldo físico em Caixa": inicial + dinheiro recebido
    # + entradas manuais EM DINHEIRO - despesas manuais EM DINHEIRO.
    # Pix/cartão/permuta/despesa paga em Pix etc. NÃO entram aqui — só
    # aumentam faturamento/entradas "de fato", nunca o dinheiro físico.
    expected_cash_balance = (
        register.initial_amount + cash_payments_total + supplies_cash_total - withdrawals_cash_total
    )

    orders_count = len(order_ids)
    average_ticket = (total_revenue / orders_count) if orders_count else Decimal("0")

    return RegisterSummary(
        register=register,
        totals_by_method=totals_by_method,
        total_revenue=total_revenue,
        cash_payments_total=cash_payments_total,
        supplies_total=supplies_total,
        withdrawals_total=withdrawals_total,
        supplies_cash_total=supplies_cash_total,
        withdrawals_cash_total=withdrawals_cash_total,
        expected_cash_balance=expected_cash_balance,
        orders_count=orders_count,
        average_ticket=average_ticket,
        total_entries=total_revenue + supplies_total,
        movements=movements,
        payments=payments,
    )


def get_register_summary(session: Session, actor: ActorContext, register_id: uuid.UUID) -> RegisterSummary:
    """Resumo completo (item "Resumo do Caixa") — usado pelo detalhe do
    caixa, pela prévia de fechamento e pela resposta de sangria/
    suprimento/fechamento (pra frontend nunca precisar de uma segunda
    chamada só pra atualizar os totais)."""
    register = _get_register_or_404(session, actor.organization_id, register_id)
    return build_summary(session, actor.organization_id, register)


def close_register(
    session: Session,
    actor: ActorContext,
    register_id: uuid.UUID,
    counted_amount: Decimal | None,
    closing_notes: str | None,
) -> CashRegister:
    register = _get_register_or_404(session, actor.organization_id, register_id)
    if register.status != CashRegisterStatus.OPEN:
        raise ConflictError("Este caixa já está fechado.")

    config = cash_register_config_service.get_effective_config(session, actor.organization_id)
    _assert_role_allowed_to_open_close(actor, config)

    summary = build_summary(session, actor.organization_id, register)
    name = _resolve_user_name(session, actor.user_id)

    register.status = CashRegisterStatus.CLOSED
    register.closed_at = datetime.now(timezone.utc)
    register.closed_by = actor.user_id
    register.closed_by_name = name
    register.closing_notes = closing_notes
    register.expected_amount = summary.expected_cash_balance
    register.counted_amount = counted_amount
    register.difference = (counted_amount - summary.expected_cash_balance) if counted_amount is not None else None
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="cash_register",
        entity_id=register.id,
        action=AuditAction.UPDATE,
        old_values={"status": "open"},
        new_values={
            "status": "closed",
            "change_type": "close_register",
            "expected_amount": str(summary.expected_cash_balance),
            "counted_amount": str(counted_amount) if counted_amount is not None else None,
            "difference": str(register.difference) if register.difference is not None else None,
        },
    )
    return _reload(session, actor.organization_id, register_id)
