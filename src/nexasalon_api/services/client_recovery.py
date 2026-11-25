"""Central de Recuperação de Clientes (Clientes → Sem retorno).

Transforma o filtro client-side de "Sem retorno" numa fila operacional
real. Conceitos-chave:

  - "Dias sem retorno" é SEMPRE derivado, na leitura, da comanda FECHADA
    mais recente do cliente (`order.closed_at`, timezone da
    organização) — NUNCA um campo congelado. Ter um agendamento futuro
    válido só torna o cliente temporariamente fora da fila de Pendentes
    (filtro computado); o contador por trás nunca "pausa"/reinicia só
    por isso — se esse agendamento for cancelado/virar falta antes de um
    atendimento novo acontecer, o cliente reaparece com o número de dias
    real, sem nenhuma lógica extra.

  - Um ciclo (`ClientRecoveryCycle`) só é criado quando existe uma ação
    operacional de verdade (registrar contato) — nunca ao simplesmente
    listar/visualizar a fila (item explícito "não criar milhares de
    registros só por consultar a tela").

  - Subaba é sempre CALCULADA a partir do estado atual, nunca um campo
    próprio: sem ciclo ou `status=pending` → Pendentes; `in_progress`
    com `next_contact_at` ainda no futuro → Acompanhamento;
    `in_progress` com `next_contact_at` vencido → volta pra Pendentes
    (vencido = precisa de ação agora); `scheduled` (retorno marcado,
    atendimento ainda não realizado) também é Acompanhamento — só é
    "Concluído" quando o ciclo de fato se encerra (`recovered`/`lost`/
    `invalid_phone`/`opted_out`/`cancelled`).

  - "Total gasto" usa `order_totals.order_charged_total` (preço menos
    benefício) — MESMA fórmula usada na Ficha do Cliente (Resumo),
    nunca uma conta paralela. Sempre calculado aqui; quem decide
    devolver `None` em vez do valor real (sem `finance.view`) é a
    rota (`api/v1/client_recovery.py`), nunca este módulo.

  - Escala: a listagem continua carregando todos os clientes ATIVOS da
    organização para calcular elegibilidade (mesmo princípio já aceito
    em `services/clients.py::list_clients_with_summary` — sempre em
    LOTE, nunca 1 query por cliente/linha), mas a RESPOSTA da API é
    paginada no servidor (o cliente HTTP nunca recebe a lista inteira,
    diferente da aba "Sem retorno" anterior, que embarcava tudo no
    bundle do navegador)."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.core.normalize import normalize_whatsapp_number
from nexasalon_api.models.client import Client
from nexasalon_api.models.client_recovery import (
    ClientRecoveryCycle,
    ClientRecoveryEvent,
    OrganizationRecoverySettings,
)
from nexasalon_api.models.enums import (
    AppointmentStatus,
    RecoveryContactChannel,
    RecoveryContactResult,
    RecoveryCycleStatus,
    RecoveryEventType,
)
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import (
    appointment_repo,
    client_recovery_repo,
    client_repo,
    order_repo,
    organization_recovery_settings_repo,
    organization_repo,
    user_repo,
)
from nexasalon_api.services.clients import UPCOMING_APPOINTMENT_STATUSES

# `SCHEDULED` conta como ATIVO (nunca "concluído") — um retorno marcado
# mas ainda não realizado continua em Acompanhamento, com a ação "Ver
# agendamento" (item explícito do pedido V3). Precisa estar em sincronia
# com `client_recovery_repo._ACTIVE_STATUSES` (usado por
# `get_active_for_client[_for_update]`) — sem isso, registrar um novo
# contato ou religar um agendamento pra um cliente já `scheduled`
# encontraria "nenhum ciclo ativo" e criaria um ciclo duplicado.
_ACTIVE_CYCLE_STATUSES = frozenset(
    {RecoveryCycleStatus.PENDING, RecoveryCycleStatus.IN_PROGRESS, RecoveryCycleStatus.SCHEDULED}
)
_CONCLUDED_STATUSES = frozenset(
    {
        RecoveryCycleStatus.RECOVERED,
        RecoveryCycleStatus.LOST,
        RecoveryCycleStatus.INVALID_PHONE,
        RecoveryCycleStatus.OPTED_OUT,
        RecoveryCycleStatus.CANCELLED,
    }
)

_DEFAULT_NO_RETURN_DAYS = 60
_DEFAULT_FOLLOW_UP_DAYS = 3
# VESTIGIAL (ajuste "Recusou, encerrou"): a carência temporária de
# reentrada após "Sem interesse" foi ELIMINADA por completo — "sem
# interesse" agora só desbloqueia com uma visita nova de verdade,
# nunca por tempo decorrido (ver `_is_blocked_from_new_cycle`). Este
# valor nunca mais é lido por nenhuma regra; mantido só porque
# organizações já podem ter uma linha salva em
# `organization_recovery_settings.default_snooze_days` (nunca excluído
# — "não excluir configurações armazenadas desnecessariamente"), e o
# campo continua existindo na leitura/escrita da API por
# compatibilidade. A tela de Configurações não oferece mais este campo.
_DEFAULT_SNOOZE_DAYS = 60
_DEFAULT_WHATSAPP_TEMPLATE = (
    "Olá, {first_name}! Tudo bem? Aqui é {attendant_name}, do {business_name}. Faz um "
    "tempinho desde seu último atendimento e gostaríamos de saber se você deseja agendar "
    "seu retorno. Podemos verificar um horário para você?"
)

# Transição de status por resultado do contato (seção 12 do pedido).
# `RETURN_SCHEDULED` exige `linked_appointment_id` (validado em
# `log_contact`, nunca alcançável só pela escolha manual do resultado).
_STATUS_BY_RESULT: dict[RecoveryContactResult, RecoveryCycleStatus] = {
    RecoveryContactResult.AWAITING_RESPONSE: RecoveryCycleStatus.IN_PROGRESS,
    RecoveryContactResult.INTERESTED: RecoveryCycleStatus.IN_PROGRESS,
    RecoveryContactResult.CALL_LATER: RecoveryCycleStatus.IN_PROGRESS,
    RecoveryContactResult.RETURN_SCHEDULED: RecoveryCycleStatus.SCHEDULED,
    RecoveryContactResult.NOT_INTERESTED: RecoveryCycleStatus.LOST,
    RecoveryContactResult.INVALID_PHONE: RecoveryCycleStatus.INVALID_PHONE,
    RecoveryContactResult.OPTED_OUT: RecoveryCycleStatus.OPTED_OUT,
    RecoveryContactResult.UNREACHABLE: RecoveryCycleStatus.IN_PROGRESS,
}
# Resultados que EXIGEM `next_contact_at` (item "obrigatório quando o
# resultado exigir acompanhamento futuro").
_RESULTS_REQUIRING_NEXT_CONTACT = frozenset(
    {RecoveryContactResult.AWAITING_RESPONSE, RecoveryContactResult.CALL_LATER, RecoveryContactResult.UNREACHABLE}
)
# Resultados terminais (fecham o ciclo, `closed_at` preenchido).
_TERMINAL_RESULTS = frozenset(
    {
        RecoveryContactResult.NOT_INTERESTED,
        RecoveryContactResult.INVALID_PHONE,
        RecoveryContactResult.OPTED_OUT,
    }
)


@dataclass(frozen=True)
class RecoverySettingsData:
    default_no_return_days: int
    default_follow_up_days: int
    # VESTIGIAL — nunca mais aplicado a nenhuma regra (ver
    # `_is_blocked_from_new_cycle`); mantido só pra não descartar
    # configuração já salva por alguma organização.
    default_snooze_days: int
    whatsapp_message_template: str


def get_effective_settings(session: Session, organization_id: uuid.UUID) -> RecoverySettingsData:
    """Mesmo padrão de `CashRegisterConfig` — sem linha ainda, devolve
    os defaults abaixo em memória, nunca grava nada numa leitura."""
    row = organization_recovery_settings_repo.get(session, organization_id)
    return RecoverySettingsData(
        default_no_return_days=(row.default_no_return_days if row and row.default_no_return_days else _DEFAULT_NO_RETURN_DAYS),
        default_follow_up_days=(row.default_follow_up_days if row and row.default_follow_up_days else _DEFAULT_FOLLOW_UP_DAYS),
        default_snooze_days=(row.default_snooze_days if row and row.default_snooze_days else _DEFAULT_SNOOZE_DAYS),
        whatsapp_message_template=(
            row.whatsapp_message_template if row and row.whatsapp_message_template else _DEFAULT_WHATSAPP_TEMPLATE
        ),
    )


def update_settings(
    session: Session,
    actor: ActorContext,
    *,
    default_no_return_days: int | None,
    default_follow_up_days: int | None,
    default_snooze_days: int | None,
    whatsapp_message_template: str | None,
) -> OrganizationRecoverySettings:
    row = organization_recovery_settings_repo.get(session, actor.organization_id)
    if row is None:
        row = OrganizationRecoverySettings(organization_id=actor.organization_id)
        organization_recovery_settings_repo.create(session, row)
    row.default_no_return_days = default_no_return_days
    row.default_follow_up_days = default_follow_up_days
    row.default_snooze_days = default_snooze_days
    row.whatsapp_message_template = whatsapp_message_template
    row.updated_by = actor.user_id
    organization_recovery_settings_repo.save(session, row)
    return row


def _organization_timezone(session: Session, organization_id: uuid.UUID) -> ZoneInfo:
    organization = organization_repo.get(session, organization_id)
    tz_name = organization.timezone if organization else "America/Sao_Paulo"
    return ZoneInfo(tz_name)


def _days_between(earlier: datetime, later: datetime, tz: ZoneInfo) -> int:
    return (later.astimezone(tz).date() - earlier.astimezone(tz).date()).days


def build_whatsapp_href_for_client(
    session: Session, actor: ActorContext, client: Client
) -> str | None:
    """`href` `wa.me` PRONTO, montado no backend (nunca no frontend) —
    mesmo padrão de `services/public_page.py::_build_href`. `None`
    quando não há telefone/WhatsApp válido OU o cliente optou por não
    receber contato comercial (item explícito "nunca disponibilizar a
    ação... quando o cliente tiver solicitado não receber contatos")."""
    if client.commercial_contact_opt_out_at is not None:
        return None
    raw_phone = client.whatsapp or client.phone
    if not raw_phone:
        return None

    organization = organization_repo.get(session, actor.organization_id)
    actor_name = _resolve_actor_name(session, actor)
    settings = get_effective_settings(session, actor.organization_id)
    first_name = client.name.split(" ")[0] if client.name else client.name
    message = settings.whatsapp_message_template.format(
        first_name=first_name,
        attendant_name=actor_name or "",
        business_name=organization.name if organization else "",
    )
    number = normalize_whatsapp_number(raw_phone)
    return f"https://wa.me/{number}?text={quote(message)}"


@dataclass
class RecoveryQueueRow:
    client: Client
    cycle: ClientRecoveryCycle | None
    last_visit_at: datetime | None
    days_since_last_visit: int | None
    last_professional_names: list[str]
    last_service_names: list[str]
    # Ids correspondentes (mesma ordem/origem de `last_*_names`) — usados
    # só para filtrar por procedimento/profissional (nunca por nome, que
    # pode colidir entre registros diferentes).
    last_service_ids: list[uuid.UUID]
    last_professional_ids: list[uuid.UUID]
    next_appointment_at: datetime | None
    total_spent: Decimal
    is_overdue: bool


@dataclass
class RecoveryQueueResult:
    items: list[RecoveryQueueRow]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class _OrderItemSummary:
    professional_names: list[str]
    service_names: list[str]
    professional_ids: list[uuid.UUID]
    service_ids: list[uuid.UUID]


def _summarize_order_items(order) -> _OrderItemSummary:
    return _OrderItemSummary(
        professional_names=list(dict.fromkeys(item.professional_name for item in order.items)),
        service_names=list(dict.fromkeys(item.service_name for item in order.items)),
        professional_ids=list(dict.fromkeys(item.professional_id for item in order.items)),
        service_ids=list(dict.fromkeys(item.service_id for item in order.items)),
    )


def _most_relevant_cycle(cycles: list[ClientRecoveryCycle]) -> tuple[ClientRecoveryCycle | None, ClientRecoveryCycle | None]:
    """Devolve `(ciclo_ativo, ciclo_mais_recente_qualquer_status)` — o
    índice único parcial garante no banco que há no máximo 1 ativo."""
    active = next((c for c in cycles if c.status in _ACTIVE_CYCLE_STATUSES), None)
    most_recent = max(cycles, key=lambda c: c.created_at) if cycles else None
    return active, most_recent


def _is_blocked_from_new_cycle(
    client: Client, most_recent_cycle: ClientRecoveryCycle | None, last_visit_at: datetime | None,
    *, now: datetime, tz: ZoneInfo, no_return_days: int,
) -> bool:
    """Aplica a recusa e o prazo de inatividade a partir da última comanda fechada."""
    if client.commercial_contact_opt_out_at is not None:
        return True
    if most_recent_cycle is None:
        return False
    if last_visit_at is None:
        return True
    if most_recent_cycle.status == RecoveryCycleStatus.LOST and (
        most_recent_cycle.closed_at is None or last_visit_at <= most_recent_cycle.closed_at
    ):
        return True
    return _days_between(last_visit_at, now, tz) < no_return_days


def _assert_can_start_new_cycle(
    session: Session, organization_id: uuid.UUID, client: Client, now: datetime
) -> None:
    all_cycles = client_recovery_repo.list_all_for_clients(session, organization_id, [client.id])
    _, most_recent_cycle = _most_relevant_cycle(all_cycles)
    closed_orders = order_repo.list_closed_for_clients(session, organization_id, [client.id])
    last_visit_at = closed_orders[0].closed_at if closed_orders else None
    if _is_blocked_from_new_cycle(
        client, most_recent_cycle, last_visit_at, now=now,
        tz=_organization_timezone(session, organization_id),
        no_return_days=get_effective_settings(session, organization_id).default_no_return_days,
    ):
        raise ValidationDomainError(
            "Cliente indisponível para um novo ciclo de recuperação. Verifique o prazo de "
            "inatividade, se houve novo atendimento após a recusa e a preferência de contato."
        )


def list_recovery_queue(
    session: Session,
    actor: ActorContext,
    *,
    subtab: str,
    min_days: int | None = None,
    max_days: int | None = None,
    search: str | None = None,
    assigned_user_id: uuid.UUID | None = None,
    has_phone: bool | None = None,
    overdue_only: bool = False,
    concluded_statuses: frozenset[RecoveryCycleStatus] | None = None,
    last_procedure_service_id: uuid.UUID | None = None,
    last_professional_id: uuid.UUID | None = None,
    sort: str = "days_desc",
    page: int = 1,
    page_size: int = 20,
) -> RecoveryQueueResult:
    """Todos os filtros abaixo (dias, procedimento, profissional) são
    aplicados sobre a população ELEGÍVEL INTEIRA, sempre ANTES do corte
    de paginação no final — nunca sobre a página já fatiada."""
    if subtab not in ("pending", "in_progress", "concluded"):
        raise ValueError(f"subtab inválida: {subtab!r}")

    organization_id = actor.organization_id
    now = datetime.now(timezone.utc)
    tz = _organization_timezone(session, organization_id)
    can_view_team = "clients.recovery.view_team" in actor.permissions

    if subtab == "concluded":
        rows = _list_concluded(
            session, organization_id, search=search, assigned_user_id=assigned_user_id,
            concluded_statuses=concluded_statuses, can_view_team=can_view_team, actor_user_id=actor.user_id,
        )
    else:
        rows = _list_pending_or_in_progress(
            session, organization_id, subtab=subtab, now=now, tz=tz, search=search,
            assigned_user_id=assigned_user_id, has_phone=has_phone, overdue_only=overdue_only,
            can_view_team=can_view_team, actor_user_id=actor.user_id,
        )

    if min_days is not None:
        rows = [r for r in rows if r.days_since_last_visit is not None and r.days_since_last_visit >= min_days]
    if max_days is not None:
        rows = [r for r in rows if r.days_since_last_visit is not None and r.days_since_last_visit <= max_days]
    if last_procedure_service_id is not None:
        rows = [r for r in rows if last_procedure_service_id in r.last_service_ids]
    if last_professional_id is not None:
        rows = [r for r in rows if last_professional_id in r.last_professional_ids]

    rows = _sort_rows(rows, sort)

    total = len(rows)
    start = (page - 1) * page_size
    page_items = rows[start : start + page_size]
    return RecoveryQueueResult(items=page_items, total=total, page=page, page_size=page_size)


def _sort_rows(rows: list[RecoveryQueueRow], sort: str) -> list[RecoveryQueueRow]:
    def _days_key(r: RecoveryQueueRow) -> int:
        return r.days_since_last_visit if r.days_since_last_visit is not None else -1

    def _last_visit_key(r: RecoveryQueueRow) -> datetime:
        return r.last_visit_at or datetime.min.replace(tzinfo=timezone.utc)

    def _next_contact_key(r: RecoveryQueueRow) -> datetime:
        if r.cycle is not None and r.cycle.next_contact_at is not None:
            return r.cycle.next_contact_at
        return datetime.max.replace(tzinfo=timezone.utc)

    if sort == "days_asc":
        return sorted(rows, key=_days_key)
    if sort == "total_spent_desc":
        return sorted(rows, key=lambda r: r.total_spent, reverse=True)
    if sort == "total_spent_asc":
        return sorted(rows, key=lambda r: r.total_spent)
    if sort == "last_visit_desc":
        return sorted(rows, key=_last_visit_key, reverse=True)
    if sort == "last_visit_asc":
        return sorted(rows, key=_last_visit_key)
    if sort == "next_contact_asc":
        return sorted(rows, key=_next_contact_key)
    # default: "days_desc"
    return sorted(rows, key=_days_key, reverse=True)


def _list_pending_or_in_progress(
    session: Session,
    organization_id: uuid.UUID,
    *,
    subtab: str,
    now: datetime,
    tz: ZoneInfo,
    search: str | None,
    assigned_user_id: uuid.UUID | None,
    has_phone: bool | None,
    overdue_only: bool,
    can_view_team: bool,
    actor_user_id: uuid.UUID,
) -> list[RecoveryQueueRow]:
    clients = client_repo.list_all(session, organization_id, include_inactive=False, search=search)
    if not clients:
        return []
    client_ids = [c.id for c in clients]

    last_closed_order: dict[uuid.UUID, Order] = {}
    for closed_order in order_repo.list_closed_for_clients(session, organization_id, client_ids):
        last_closed_order.setdefault(closed_order.client_id, closed_order)

    total_spent_by_client = order_repo.sum_charged_total_by_client(session, organization_id, client_ids)

    next_appointment: dict[uuid.UUID, datetime] = {}
    for appointment in appointment_repo.list_for_clients(session, organization_id, client_ids):
        if appointment.status in UPCOMING_APPOINTMENT_STATUSES and appointment.starts_at is not None:
            current = next_appointment.get(appointment.client_id)
            if current is None or appointment.starts_at < current:
                next_appointment[appointment.client_id] = appointment.starts_at

    cycles_by_client: dict[uuid.UUID, list[ClientRecoveryCycle]] = {}
    for cycle in client_recovery_repo.list_all_for_clients(session, organization_id, client_ids):
        cycles_by_client.setdefault(cycle.client_id, []).append(cycle)

    no_return_days = get_effective_settings(session, organization_id).default_no_return_days
    rows: list[RecoveryQueueRow] = []
    for client in clients:
        order = last_closed_order.get(client.id)
        if order is None:
            continue  # nunca atendida -> nunca é "sem retorno"

        cycles = cycles_by_client.get(client.id, [])
        active_cycle, most_recent_cycle = _most_relevant_cycle(cycles)

        if active_cycle is None:
            if _is_blocked_from_new_cycle(
                client, most_recent_cycle, order.closed_at, now=now, tz=tz, no_return_days=no_return_days,
            ):
                continue
            if client.id in next_appointment:
                continue  # agendamento futuro (espontâneo ou não) -> não elegível agora

        row_subtab = _classify_subtab(active_cycle, now)
        if row_subtab != subtab:
            continue

        if assigned_user_id is not None and (active_cycle is None or active_cycle.assigned_user_id != assigned_user_id):
            continue
        if not can_view_team and active_cycle is not None and active_cycle.assigned_user_id not in (None, actor_user_id):
            continue
        if has_phone is True and not (client.phone or client.whatsapp):
            continue
        if has_phone is False and (client.phone or client.whatsapp):
            continue

        is_overdue = (
            active_cycle is not None
            and active_cycle.status == RecoveryCycleStatus.IN_PROGRESS
            and active_cycle.next_contact_at is not None
            and active_cycle.next_contact_at <= now
        )
        if overdue_only and not is_overdue:
            continue

        summary = _summarize_order_items(order)
        rows.append(
            RecoveryQueueRow(
                client=client,
                cycle=active_cycle,
                last_visit_at=order.closed_at,
                days_since_last_visit=_days_between(order.closed_at, now, tz) if order.closed_at else None,
                last_professional_names=summary.professional_names,
                last_service_names=summary.service_names,
                last_service_ids=summary.service_ids,
                last_professional_ids=summary.professional_ids,
                next_appointment_at=next_appointment.get(client.id),
                total_spent=total_spent_by_client.get(client.id, Decimal(0)),
                is_overdue=is_overdue,
            )
        )
    return rows


def _classify_subtab(cycle: ClientRecoveryCycle | None, now: datetime) -> str:
    if cycle is None or cycle.status == RecoveryCycleStatus.PENDING:
        return "pending"
    if cycle.status == RecoveryCycleStatus.IN_PROGRESS:
        if cycle.next_contact_at is not None and cycle.next_contact_at <= now:
            return "pending"
        return "in_progress"
    if cycle.status == RecoveryCycleStatus.SCHEDULED:
        # Retorno marcado, atendimento ainda não realizado — nunca
        # "vencido" (não há data de contato pendente, há uma visita
        # futura real) e nunca "concluído" (só quando a comanda fechar).
        return "in_progress"
    return "concluded"


def _list_concluded(
    session: Session,
    organization_id: uuid.UUID,
    *,
    search: str | None,
    assigned_user_id: uuid.UUID | None,
    concluded_statuses: frozenset[RecoveryCycleStatus] | None,
    can_view_team: bool,
    actor_user_id: uuid.UUID,
) -> list[RecoveryQueueRow]:
    statuses = concluded_statuses if concluded_statuses else _CONCLUDED_STATUSES
    cycles = client_recovery_repo.list_by_org(
        session, organization_id, statuses=statuses, assigned_user_id=assigned_user_id
    )
    if not can_view_team:
        cycles = [c for c in cycles if c.assigned_user_id in (None, actor_user_id)]
    if not cycles:
        return []

    client_ids = list({c.client_id for c in cycles})
    clients_by_id = {c.id: c for c in client_repo.list_by_ids(session, organization_id, set(client_ids))}

    last_closed_order: dict[uuid.UUID, Order] = {}
    for closed_order in order_repo.list_closed_for_clients(session, organization_id, client_ids):
        last_closed_order.setdefault(closed_order.client_id, closed_order)
    total_spent_by_client = order_repo.sum_charged_total_by_client(session, organization_id, client_ids)

    tz = _organization_timezone(session, organization_id)
    now = datetime.now(timezone.utc)

    rows: list[RecoveryQueueRow] = []
    for cycle in cycles:
        client = clients_by_id.get(cycle.client_id)
        if client is None:
            continue
        if search:
            needle = search.strip().lower()
            if needle and needle not in client.name.lower():
                continue
        order = last_closed_order.get(client.id)
        summary = _summarize_order_items(order) if order is not None else _OrderItemSummary([], [], [], [])
        rows.append(
            RecoveryQueueRow(
                client=client,
                cycle=cycle,
                last_visit_at=order.closed_at if order is not None else None,
                days_since_last_visit=(
                    _days_between(order.closed_at, now, tz) if order is not None and order.closed_at else None
                ),
                last_professional_names=summary.professional_names,
                last_service_names=summary.service_names,
                last_service_ids=summary.service_ids,
                last_professional_ids=summary.professional_ids,
                next_appointment_at=None,
                total_spent=total_spent_by_client.get(client.id, Decimal(0)),
                is_overdue=False,
            )
        )
    return rows


@dataclass
class RecoverySummary:
    pending_count: int
    in_progress_count: int
    # Recuperados/Sem interesse contam CLIENTES DISTINTOS encerrados no
    # período (nunca ciclos) — se o mesmo cliente tiver 2 ciclos
    # `recovered` no mesmo período, entra uma única vez aqui, mas conta 2x
    # em `concluded_count` (ambos os ciclos continuam visíveis no
    # histórico/Concluídos). Essa distinção é explícita na UI.
    recovered_count: int
    not_interested_count: int
    # Soma de TODOS os CICLOS com status terminal encerrados no período
    # (badge da aba Concluídos) — conta ciclos, não clientes distintos.
    concluded_count: int
    date_from: datetime
    date_to: datetime


def get_recovery_summary(
    session: Session, actor: ActorContext, *, date_from: datetime, date_to: datetime
) -> RecoverySummary:
    """4 indicadores (item 3 do pedido). Pendentes/Em acompanhamento são
    ESTOQUE atual (nunca dependem do período); Recuperados/Sem
    interesse/`concluded_count` são HISTÓRICOS (ciclos cujo `closed_at`
    cai dentro do período informado) — nunca misturados na mesma conta,
    exatamente como pedido."""
    organization_id = actor.organization_id
    can_view_team = "clients.recovery.view_team" in actor.permissions
    assigned_user_id = None if can_view_team else actor.user_id

    active_cycles = client_recovery_repo.list_by_org(
        session, organization_id, statuses=_ACTIVE_CYCLE_STATUSES, assigned_user_id=assigned_user_id
    )
    now = datetime.now(timezone.utc)
    pending_count = 0
    in_progress_count = 0
    for cycle in active_cycles:
        if _classify_subtab(cycle, now) == "pending":
            pending_count += 1
        else:
            in_progress_count += 1

    # Clientes elegíveis SEM nenhum ciclo ainda também contam como
    # "Pendentes" (item "calculado pelo backend, nunca pela página
    # atual") — reaproveita a mesma listagem, só a contagem total.
    pending_count += len(
        [
            r
            for r in _list_pending_or_in_progress(
                session, organization_id, subtab="pending", now=now,
                tz=_organization_timezone(session, organization_id), search=None,
                assigned_user_id=assigned_user_id, has_phone=None, overdue_only=False,
                can_view_team=can_view_team, actor_user_id=actor.user_id,
            )
            if r.cycle is None
        ]
    )

    closed_counts = client_recovery_repo.count_by_status_in_period(
        session, organization_id, date_from, date_to, assigned_user_id=assigned_user_id,
    )
    concluded_count = sum(closed_counts.values())

    distinct_client_counts = client_recovery_repo.count_distinct_clients_by_status_in_period(
        session, organization_id, date_from, date_to, assigned_user_id=assigned_user_id,
    )
    recovered_count = distinct_client_counts.get(RecoveryCycleStatus.RECOVERED, 0)
    not_interested_count = distinct_client_counts.get(RecoveryCycleStatus.LOST, 0)

    return RecoverySummary(
        pending_count=pending_count,
        in_progress_count=in_progress_count,
        recovered_count=recovered_count,
        not_interested_count=not_interested_count,
        concluded_count=concluded_count,
        date_from=date_from,
        date_to=date_to,
    )


def _resolve_actor_name(session: Session, actor: ActorContext) -> str | None:
    user = user_repo.get(session, actor.user_id)
    return user.name if user is not None else None


def log_contact(
    session: Session,
    actor: ActorContext,
    client_id: uuid.UUID,
    *,
    contact_channel: RecoveryContactChannel | None,
    contact_result: RecoveryContactResult,
    notes: str | None,
    next_contact_at: datetime | None,
    linked_appointment_id: uuid.UUID | None,
) -> ClientRecoveryCycle:
    """Registra o evento e cria ou atualiza o ciclo sob locks transacionais."""
    # Serializa contatos mesmo quando não há ciclo ativo para travar.
    # Uma recusa concorrente precisa estar visível antes de decidir a reentrada.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"client_recovery:{actor.organization_id}:{client_id}"},
    )
    client = client_repo.get(session, actor.organization_id, client_id)
    if client is None:
        raise NotFoundError("Cliente não encontrado.")

    if next_contact_at is not None and next_contact_at <= datetime.now(timezone.utc):
        raise ValidationDomainError("A data de próximo contato não pode estar no passado.")
    if contact_result in _RESULTS_REQUIRING_NEXT_CONTACT and next_contact_at is None:
        raise ValidationDomainError("Este resultado exige uma data de próximo contato.")
    if contact_result == RecoveryContactResult.RETURN_SCHEDULED:
        if linked_appointment_id is None:
            raise ValidationDomainError(
                "Retorno agendado exige um agendamento real vinculado — use 'Agendar retorno'."
            )
        appointment = appointment_repo.get(session, actor.organization_id, linked_appointment_id)
        if appointment is None or appointment.client_id != client_id:
            raise ValidationDomainError("Agendamento de retorno inválido para este cliente.")
        if appointment.status == AppointmentStatus.CANCELLED:
            raise ValidationDomainError("O agendamento de retorno informado está cancelado.")

    existing = client_recovery_repo.get_active_for_client_for_update(session, actor.organization_id, client_id)
    actor_name = _resolve_actor_name(session, actor)
    now = datetime.now(timezone.utc)
    new_status = _STATUS_BY_RESULT[contact_result]

    if existing is None:
        # "Não deseja receber contatos" (bloqueio PERMANENTE) nunca é
        # bloqueado por esta regra — item explícito: o opt-out
        # independe do estado de "sem interesse", precisa sempre poder
        # ser aplicado (inclusive pra um cliente JÁ `lost` sem visita
        # nova, que é exatamente o caso mais comum de uso real).
        if contact_result != RecoveryContactResult.OPTED_OUT:
            _assert_can_start_new_cycle(session, actor.organization_id, client, now)
        cycle = ClientRecoveryCycle(
            organization_id=actor.organization_id,
            client_id=client_id,
            status=RecoveryCycleStatus.PENDING,
            assigned_user_id=actor.user_id,
            assigned_user_name=actor_name,
        )
        try:
            # O índice da 0062 protege a unicidade no banco. O savepoint
            # permite reler um ciclo ativo sem invalidar a transação.
            with session.begin_nested():
                client_recovery_repo.create(session, cycle)
        except IntegrityError:
            # Perdeu a corrida: a outra requisição já criou o ciclo.
            # Nunca um erro genérico de conflito pro usuário, nunca
            # perde o contato que estava sendo registrado — relê o
            # ciclo (agora existe, agora SIM travável) e aplica a MESMA
            # transição nele, como se tivesse encontrado o ciclo ativo
            # desde o início.
            existing = client_recovery_repo.get_active_for_client_for_update(
                session, actor.organization_id, client_id
            )
            if existing is None:
                raise ConflictError(
                    "Não foi possível registrar o contato — tente novamente."
                ) from None
            cycle = existing
            previous_status = cycle.status
        else:
            _record_event(
                session, actor, cycle, event_type=RecoveryEventType.CREATED,
                previous_status=None, new_status=RecoveryCycleStatus.PENDING, now=now,
            )
            previous_status = RecoveryCycleStatus.PENDING
    else:
        cycle = existing
        previous_status = cycle.status

    cycle.status = new_status
    cycle.next_contact_at = next_contact_at
    if contact_result == RecoveryContactResult.RETURN_SCHEDULED:
        cycle.linked_appointment_id = linked_appointment_id
    if contact_result in _TERMINAL_RESULTS:
        cycle.closed_at = now
        if contact_result == RecoveryContactResult.OPTED_OUT:
            client.commercial_contact_opt_out_at = now
            client.commercial_contact_opt_out_reason = "Solicitado durante contato de recuperação."
    cycle.version = cycle.version + 1
    client_recovery_repo.save(session, cycle)

    _record_event(
        session, actor, cycle, event_type=RecoveryEventType.CONTACT_LOGGED,
        previous_status=previous_status, new_status=new_status, now=now,
        contact_channel=contact_channel, contact_result=contact_result, notes=notes,
        next_contact_at=next_contact_at,
        linked_appointment_id=(linked_appointment_id if contact_result == RecoveryContactResult.RETURN_SCHEDULED else None),
    )
    return cycle


def reassign_cycle(
    session: Session, actor: ActorContext, client_id: uuid.UUID, *, new_assigned_user_id: uuid.UUID | None, expected_version: int
) -> ClientRecoveryCycle:
    cycle = client_recovery_repo.get_active_for_client_for_update(session, actor.organization_id, client_id)
    if cycle is None:
        raise NotFoundError("Não existe ciclo de recuperação ativo para este cliente.")
    if expected_version != cycle.version:
        raise ConflictError("Este ciclo foi alterado por outro usuário. Atualize antes de tentar novamente.")

    new_user = user_repo.get(session, new_assigned_user_id) if new_assigned_user_id else None
    cycle.assigned_user_id = new_assigned_user_id
    cycle.assigned_user_name = new_user.name if new_user is not None else None
    cycle.version = cycle.version + 1
    client_recovery_repo.save(session, cycle)

    _record_event(
        session, actor, cycle, event_type=RecoveryEventType.REASSIGNED,
        previous_status=cycle.status, new_status=cycle.status, now=datetime.now(timezone.utc),
    )
    return cycle


def reopen_cycle(session: Session, actor: ActorContext, client_id: uuid.UUID) -> ClientRecoveryCycle:
    """Reabre um ciclo encerrado como `invalid_phone` (item 12 — "Permitir
    reabrir o ciclo quando o telefone for corrigido ou mediante ação
    autorizada"). Nunca reabre `opted_out` (exige alteração explícita da
    preferência, não um simples reopen) nem um ciclo já ativo."""
    active = client_recovery_repo.get_active_for_client(session, actor.organization_id, client_id)
    if active is not None:
        raise ConflictError("Já existe um ciclo de recuperação ativo para este cliente.")

    cycles = client_recovery_repo.list_all_for_clients(session, actor.organization_id, [client_id])
    if not cycles:
        raise NotFoundError("Nenhum ciclo de recuperação encontrado para este cliente.")
    latest = max(cycles, key=lambda c: c.created_at)
    if latest.status != RecoveryCycleStatus.INVALID_PHONE:
        raise ValidationDomainError("Só é possível reabrir um ciclo encerrado como 'telefone inválido'.")

    now = datetime.now(timezone.utc)
    previous_status = latest.status
    latest.status = RecoveryCycleStatus.PENDING
    latest.closed_at = None
    latest.version = latest.version + 1
    client_recovery_repo.save(session, latest)

    _record_event(
        session, actor, latest, event_type=RecoveryEventType.REOPENED,
        previous_status=previous_status, new_status=RecoveryCycleStatus.PENDING, now=now,
    )
    return latest


def link_return_appointment(
    session: Session, actor: ActorContext, client_id: uuid.UUID, *, appointment_id: uuid.UUID, expected_version: int
) -> ClientRecoveryCycle:
    """"Agendar retorno" (item 12) — chamado DEPOIS que o agendamento já
    foi criado pelo fluxo normal da Agenda (nunca duplica lógica de
    disponibilidade/conflito aqui). Só vincula um ciclo já ABERTO pela
    recepção — nunca cria um ciclo novo a partir de um agendamento
    espontâneo (item explícito "sem atribuir indevidamente")."""
    cycle = client_recovery_repo.get_active_for_client_for_update(session, actor.organization_id, client_id)
    if cycle is None:
        raise NotFoundError("Não existe ciclo de recuperação ativo para vincular este agendamento.")
    if expected_version != cycle.version:
        raise ConflictError("Este ciclo foi alterado por outro usuário. Atualize antes de tentar novamente.")

    appointment = appointment_repo.get(session, actor.organization_id, appointment_id)
    if appointment is None or appointment.client_id != client_id:
        raise ValidationDomainError("Agendamento inválido para este cliente.")
    if appointment.status == AppointmentStatus.CANCELLED:
        raise ValidationDomainError("O agendamento informado está cancelado.")

    previous_status = cycle.status
    cycle.status = RecoveryCycleStatus.SCHEDULED
    cycle.linked_appointment_id = appointment_id
    cycle.version = cycle.version + 1
    client_recovery_repo.save(session, cycle)

    _record_event(
        session, actor, cycle, event_type=RecoveryEventType.APPOINTMENT_LINKED,
        previous_status=previous_status, new_status=RecoveryCycleStatus.SCHEDULED,
        now=datetime.now(timezone.utc), linked_appointment_id=appointment_id,
    )
    return cycle


def mark_recovered_if_linked_appointment_completed(
    session: Session, actor: ActorContext, appointment_id: uuid.UUID
) -> None:
    """Chamado quando a Comanda do agendamento de retorno FECHA (ver
    hook em `services/orders.py::close_order`) — só então o ciclo vira
    `recovered` (item "só vira recuperado quando o atendimento vinculado
    é efetivamente realizado", nunca só por agendar)."""
    cycle = client_recovery_repo.get_scheduled_by_linked_appointment(session, actor.organization_id, appointment_id)
    if cycle is None:
        return
    now = datetime.now(timezone.utc)
    previous_status = cycle.status
    cycle.status = RecoveryCycleStatus.RECOVERED
    cycle.closed_at = now
    cycle.version = cycle.version + 1
    client_recovery_repo.save(session, cycle)
    _record_event(
        session, actor, cycle, event_type=RecoveryEventType.CLOSED,
        previous_status=previous_status, new_status=RecoveryCycleStatus.RECOVERED, now=now,
    )


def _next_upcoming_appointment_id(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID, *, excluding_appointment_id: uuid.UUID
) -> uuid.UUID | None:
    """Mesmo critério de "agendamento futuro válido" já usado em
    `_list_pending_or_in_progress` (`UPCOMING_APPOINTMENT_STATUSES`),
    aplicado a um único cliente — usado por
    `reopen_cycle_if_linked_appointment_cancelled` pra decidir se existe
    OUTRO agendamento (além do que acabou de ser cancelado/virar falta)
    antes de devolver o ciclo pra Pendentes."""
    candidates = [
        (a.starts_at, a.id)
        for a in appointment_repo.list_for_clients(session, organization_id, [client_id])
        if a.id != excluding_appointment_id
        and a.status in UPCOMING_APPOINTMENT_STATUSES
        and a.starts_at is not None
    ]
    if not candidates:
        return None
    return min(candidates)[1]


def reopen_cycle_if_linked_appointment_cancelled(
    session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID
) -> None:
    """Hook chamado de `services/appointments.py` sempre que um
    `Appointment` que é o `linked_appointment_id` de um ciclo
    `scheduled` vira `CANCELLED`/`NO_SHOW` (reentrada automática, item
    aprovado explicitamente: "se o agendamento de retorno for cancelado
    ou a cliente faltar, ela volta automaticamente para a fila"). Ação
    do SISTEMA — `actor_user_id=NULL`, nunca atribuída a uma pessoa.
    Nunca levanta erro se não houver ciclo vinculado (caminho comum —
    a maioria dos cancelamentos não tem nada a ver com recuperação).

    Antes de decidir, checa se o cliente já tem OUTRO agendamento futuro
    válido (ex.: recepção já remarcou por outro caminho antes de
    cancelar o antigo) — se tiver, o ciclo permanece `scheduled`,
    só atualizando o vínculo pro agendamento novo (nunca cria ciclo
    novo); só volta pra `pending` quando não existe nenhum outro."""
    cycle = client_recovery_repo.get_scheduled_by_linked_appointment(session, organization_id, appointment_id)
    if cycle is None:
        return
    now = datetime.now(timezone.utc)
    previous_status = cycle.status

    other_appointment_id = _next_upcoming_appointment_id(
        session, organization_id, cycle.client_id, excluding_appointment_id=appointment_id
    )
    if other_appointment_id is not None:
        cycle.linked_appointment_id = other_appointment_id
        new_status = RecoveryCycleStatus.SCHEDULED
    else:
        cycle.status = RecoveryCycleStatus.PENDING
        cycle.linked_appointment_id = None
        new_status = RecoveryCycleStatus.PENDING
    cycle.version = cycle.version + 1
    client_recovery_repo.save(session, cycle)

    event = ClientRecoveryEvent(
        organization_id=organization_id,
        recovery_cycle_id=cycle.id,
        client_id=cycle.client_id,
        actor_user_id=None,
        actor_user_name=None,
        event_type=RecoveryEventType.REOPENED,
        previous_status=previous_status,
        new_status=new_status,
        linked_appointment_id=other_appointment_id,
        created_at=now,
    )
    client_recovery_repo.create_event(session, event)


def _record_event(
    session: Session,
    actor: ActorContext,
    cycle: ClientRecoveryCycle,
    *,
    event_type: RecoveryEventType,
    previous_status: RecoveryCycleStatus | None,
    new_status: RecoveryCycleStatus | None,
    now: datetime,
    contact_channel: RecoveryContactChannel | None = None,
    contact_result: RecoveryContactResult | None = None,
    notes: str | None = None,
    next_contact_at: datetime | None = None,
    linked_appointment_id: uuid.UUID | None = None,
) -> ClientRecoveryEvent:
    actor_name = _resolve_actor_name(session, actor)
    event = ClientRecoveryEvent(
        organization_id=actor.organization_id,
        recovery_cycle_id=cycle.id,
        client_id=cycle.client_id,
        actor_user_id=actor.user_id,
        actor_user_name=actor_name,
        event_type=event_type,
        contact_channel=contact_channel,
        contact_result=contact_result,
        notes=notes,
        previous_status=previous_status,
        new_status=new_status,
        next_contact_at=next_contact_at,
        linked_appointment_id=linked_appointment_id,
        created_at=now,
    )
    return client_recovery_repo.create_event(session, event)


def get_client_recovery_timeline(
    session: Session, actor: ActorContext, client_id: uuid.UUID
) -> list[ClientRecoveryEvent]:
    """Aba "Recuperação" da Ficha do Cliente — mais recente primeiro."""
    client = client_repo.get(session, actor.organization_id, client_id)
    if client is None:
        raise NotFoundError("Cliente não encontrado.")
    return list(reversed(client_recovery_repo.list_events_for_client(session, actor.organization_id, client_id)))
