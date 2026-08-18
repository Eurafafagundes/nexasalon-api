"""Criação/edição/cancelamento de Appointment — Etapa 3A.

Camada de VALIDAÇÃO DE APLICAÇÃO (erro limpo e rápido). O trigger
`check_appointment_item_overlap` (migration 0004) continua sendo a
última barreira real contra concorrência — ver `_maybe_allow_overlap`.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationDomainError
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.enums import AppointmentStatus, AuditAction
from nexasalon_api.repositories import (
    appointment_item_repo,
    appointment_repo,
    audit_log_repo,
    branch_repo,
    client_repo,
    professional_repo,
    professional_service_repo,
    schedule_block_repo,
    service_repo,
)
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate, AppointmentReplace
from nexasalon_api.services import availability
from nexasalon_api.services.appointment_state_machine import assert_cancellable, next_status

FORCE_OVERLAP_PERMISSION = "agenda.force_overlap"
VIEW_ALL_PERMISSION = "agenda.view_all"
VIEW_OWN_PERMISSION = "agenda.view_own"


@dataclass
class _ItemSnapshot:
    professional_id: uuid.UUID
    service_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    price: Decimal
    has_conflict: bool


def _assert_within_working_hours(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID, professional_id: uuid.UUID,
    start_at: datetime, end_at: datetime,
) -> None:
    tz = availability.effective_timezone(session, organization_id, branch_id)
    local_date = start_at.astimezone(tz).date()
    windows = availability.working_windows_utc(session, organization_id, professional_id, local_date, tz)
    if not any(w_start <= start_at and end_at <= w_end for w_start, w_end in windows):
        raise ValidationDomainError(
            f"Horário fora da jornada de trabalho do profissional ({start_at.isoformat()})."
        )


def _assert_no_schedule_block(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID, professional_id: uuid.UUID,
    start_at: datetime, end_at: datetime,
) -> None:
    blocks = schedule_block_repo.list_overlapping(
        session, organization_id, professional_id=professional_id, branch_id=branch_id,
        range_start=start_at, range_end=end_at,
    )
    if blocks:
        raise ValidationDomainError("Horário coincide com um bloqueio de agenda do profissional/unidade.")


def _build_item_snapshot(
    session: Session,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    item_in: AppointmentItemCreate,
    *,
    exclude_appointment_id: uuid.UUID | None,
    siblings: list["_ItemSnapshot"],
) -> _ItemSnapshot:
    professional = professional_repo.get(session, organization_id, item_in.professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")
    if not professional.is_active:
        raise ValidationDomainError("Profissional está inativo.")
    if professional.branch_id is not None and professional.branch_id != branch_id:
        raise ValidationDomainError("Profissional não atende nesta unidade.")

    service = service_repo.get(session, organization_id, item_in.service_id)
    if service is None:
        raise NotFoundError("Serviço não encontrado.")
    if not service.is_active:
        raise ValidationDomainError("Serviço está inativo.")

    professional_service = professional_service_repo.get_for_pair(
        session, organization_id, item_in.professional_id, item_in.service_id
    )
    if professional_service is None or not professional_service.is_active:
        raise ValidationDomainError("Este profissional não executa este serviço.")

    duration_minutes, catalog_price = availability.effective_duration_and_price(service, professional_service)
    # `price_override` (item "valor editável por serviço") substitui só
    # o PREÇO efetivo deste item — nunca a duração, e nunca escreve de
    # volta em `Service.default_price`/`ProfessionalService.price_override`.
    # Mesmo padrão de snapshot já usado por `OrderItem.price` (ver
    # docstring de `AppointmentItemCreate`).
    price = item_in.price_override if item_in.price_override is not None else catalog_price
    start_at = item_in.start_at
    end_at = start_at + timedelta(minutes=duration_minutes)

    _assert_within_working_hours(session, organization_id, branch_id, item_in.professional_id, start_at, end_at)
    _assert_no_schedule_block(session, organization_id, branch_id, item_in.professional_id, start_at, end_at)

    db_conflicts = appointment_item_repo.list_conflicts(
        session, organization_id, professional_id=item_in.professional_id, start_at=start_at, end_at=end_at,
        exclude_appointment_id=exclude_appointment_id,
    )
    sibling_conflict = any(
        s.professional_id == item_in.professional_id and s.start_at < end_at and s.end_at > start_at
        for s in siblings
    )
    has_conflict = bool(db_conflicts) or sibling_conflict

    return _ItemSnapshot(
        professional_id=item_in.professional_id,
        service_id=item_in.service_id,
        start_at=start_at,
        end_at=end_at,
        duration_minutes=duration_minutes,
        price=price,
        has_conflict=has_conflict,
    )


def _build_all_item_snapshots(
    session: Session,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID,
    items_in: list[AppointmentItemCreate],
    *,
    exclude_appointment_id: uuid.UUID | None,
) -> list[_ItemSnapshot]:
    snapshots: list[_ItemSnapshot] = []
    for item_in in items_in:
        snapshot = _build_item_snapshot(
            session, organization_id, branch_id, item_in,
            exclude_appointment_id=exclude_appointment_id, siblings=snapshots,
        )
        snapshots.append(snapshot)
    return snapshots


def _resolve_force_overlap(actor: ActorContext, requested: bool) -> bool:
    """`force_overlap=true` exige a permission `agenda.force_overlap`.

    Ajuste pós-revisão: NÃO ignora mais em silêncio quando o ator não
    tem a permission — levanta 403 explícito. Um pedido de encaixe sem
    autorização é uma tentativa de burlar uma trava de negócio, não um
    "no-op" inofensivo que deveria só cair de volta pro caminho normal;
    o cliente precisa saber que o pedido foi recusado por falta de
    permissão, não descobrir isso indiretamente via um 409 de conflito
    (que também aconteceria mesmo sem nenhum `force_overlap` envolvido)."""
    if not requested:
        return False
    if FORCE_OVERLAP_PERMISSION not in actor.permissions:
        raise ForbiddenError(
            f"Permissão '{FORCE_OVERLAP_PERMISSION}' é necessária para usar force_overlap=true."
        )
    return True


def _apply_conflict_policy(snapshots: list[_ItemSnapshot], effective_force_overlap: bool) -> bool:
    """Levanta 409 se houver conflito e o encaixe não for permitido.
    Devolve True se algum conflito real foi (legitimamente) ignorado —
    sinal pra registrar auditoria de force_overlap."""
    any_forced = False
    for snapshot in snapshots:
        if snapshot.has_conflict:
            if not effective_force_overlap:
                raise ConflictError(
                    f"Profissional já tem um atendimento nesse horário ({snapshot.start_at.isoformat()})."
                )
            any_forced = True
    return any_forced


def _maybe_allow_overlap(session: Session, any_forced: bool) -> None:
    """`SET LOCAL` — vale só até o fim desta transação, nunca vaza pra
    fora dela. O trigger de overlap (migration 0004) é quem realmente
    decide; isto só é o sinal que ele lê."""
    if any_forced:
        session.execute(text("SET LOCAL app.allow_overlap = 'true'"))


def _insert_items(session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID, snapshots: list[_ItemSnapshot]) -> None:
    for snapshot in snapshots:
        appointment_item_repo.create(
            session,
            organization_id,
            appointment_id=appointment_id,
            service_id=snapshot.service_id,
            professional_id=snapshot.professional_id,
            start_at=snapshot.start_at,
            end_at=snapshot.end_at,
            duration_minutes=snapshot.duration_minutes,
            price=snapshot.price,
        )


def _audit_force_overlap(session: Session, actor: ActorContext, appointment_id: uuid.UUID, snapshots: list[_ItemSnapshot]) -> None:
    forced_items = [
        {
            "professional_id": str(s.professional_id),
            "service_id": str(s.service_id),
            "start_at": s.start_at.isoformat(),
            "end_at": s.end_at.isoformat(),
        }
        for s in snapshots
        if s.has_conflict
    ]
    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="appointment",
        entity_id=appointment_id,
        action=AuditAction.UPDATE,
        new_values={"change_type": "force_overlap", "items": forced_items},
    )


def _assert_branch_and_client(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID, client_id: uuid.UUID) -> None:
    if not branch_repo.exists(session, organization_id, branch_id):
        raise NotFoundError("Unidade não encontrada.")
    if client_repo.get(session, organization_id, client_id) is None:
        raise NotFoundError("Cliente não encontrado.")


def create_appointment(session: Session, actor: ActorContext, data: AppointmentCreate) -> Appointment:
    organization_id = actor.organization_id
    # Checagem de permissão de force_overlap primeiro — falha rápido e
    # barato (403) antes de gastar validação de branch/cliente/itens.
    effective_force_overlap = _resolve_force_overlap(actor, data.force_overlap)
    _assert_branch_and_client(session, organization_id, data.branch_id, data.client_id)

    snapshots = _build_all_item_snapshots(
        session, organization_id, data.branch_id, data.items, exclude_appointment_id=None
    )
    any_forced = _apply_conflict_policy(snapshots, effective_force_overlap)
    _maybe_allow_overlap(session, any_forced)

    appointment = appointment_repo.create(
        session, organization_id, branch_id=data.branch_id, client_id=data.client_id,
        notes=data.notes, created_by=actor.user_id, fit_in=data.fit_in,
    )
    _insert_items(session, organization_id, appointment.id, snapshots)
    session.flush()

    audit_log_repo.create(
        session, organization_id=organization_id, user_id=actor.user_id, entity_type="appointment",
        entity_id=appointment.id, action=AuditAction.CREATE,
        new_values={
            "branch_id": str(data.branch_id), "client_id": str(data.client_id),
            "items": [
                {"professional_id": str(s.professional_id), "service_id": str(s.service_id),
                 "start_at": s.start_at.isoformat(), "end_at": s.end_at.isoformat()}
                for s in snapshots
            ],
        },
    )
    if any_forced:
        _audit_force_overlap(session, actor, appointment.id, snapshots)

    return _reload(session, organization_id, appointment.id)


def _reload(session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID) -> Appointment:
    session.flush()
    appointment = appointment_repo.get(session, organization_id, appointment_id)
    assert appointment is not None
    return appointment


def _actor_can_view_all(actor: ActorContext) -> bool:
    return VIEW_ALL_PERMISSION in actor.permissions


def get_appointment(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Appointment:
    """404 (nunca 403) quando o ator não tem `view_all` e o agendamento
    não envolve seu próprio `professional_id` — mesmo padrão anti-leak
    usado no resto da API pra recursos fora do escopo do ator."""
    appointment = appointment_repo.get(session, actor.organization_id, appointment_id)
    if appointment is None:
        raise NotFoundError("Agendamento não encontrado.")
    if not _actor_can_view_all(actor):
        if actor.professional_id is None or not any(
            item.professional_id == actor.professional_id for item in appointment.items
        ):
            raise NotFoundError("Agendamento não encontrado.")
    return appointment


def replace_appointment(
    session: Session, actor: ActorContext, appointment_id: uuid.UUID, data: AppointmentReplace
) -> Appointment:
    """PUT — substitui unidade/cliente/notas e TODOS os itens. Reaproveita
    a mesma validação/snapshot do create; item antigo nunca é reciclado
    (sempre apagado e recriado), o que também é o que garante que o
    histórico de preço/duração dos itens ANTERIORES (se filtrado por
    AuditLog) não é tocado — só o estado atual muda."""
    organization_id = actor.organization_id
    appointment = get_appointment(session, actor, appointment_id)
    # Mesma ordem do create: 403 de force_overlap antes de qualquer
    # outra validação, inclusive antes de recalcular os itens antigos.
    effective_force_overlap = _resolve_force_overlap(actor, data.force_overlap)

    old_items_summary = [
        {"professional_id": str(i.professional_id), "service_id": str(i.service_id),
         "start_at": i.start_at.isoformat(), "end_at": i.end_at.isoformat()}
        for i in appointment.items
    ]

    _assert_branch_and_client(session, organization_id, data.branch_id, data.client_id)
    snapshots = _build_all_item_snapshots(
        session, organization_id, data.branch_id, data.items, exclude_appointment_id=appointment_id
    )
    any_forced = _apply_conflict_policy(snapshots, effective_force_overlap)
    _maybe_allow_overlap(session, any_forced)

    appointment_item_repo.delete_for_appointment(session, organization_id, appointment_id)
    session.flush()
    _insert_items(session, organization_id, appointment_id, snapshots)

    appointment.branch_id = data.branch_id
    appointment.client_id = data.client_id
    appointment.notes = data.notes
    appointment.fit_in = data.fit_in
    appointment.updated_by = actor.user_id
    session.flush()

    change_types = _diff_change_types(old_items_summary, snapshots)
    audit_log_repo.create(
        session, organization_id=organization_id, user_id=actor.user_id, entity_type="appointment",
        entity_id=appointment_id, action=AuditAction.UPDATE,
        old_values={"items": old_items_summary},
        new_values={
            "change_type": change_types,
            "items": [
                {"professional_id": str(s.professional_id), "service_id": str(s.service_id),
                 "start_at": s.start_at.isoformat(), "end_at": s.end_at.isoformat()}
                for s in snapshots
            ],
        },
    )
    if any_forced:
        _audit_force_overlap(session, actor, appointment_id, snapshots)

    return _reload(session, organization_id, appointment_id)


def _diff_change_types(old_items: list[dict], new_snapshots: list[_ItemSnapshot]) -> list[str]:
    """Comparação posicional best-effort (o PUT não manda IDs de item) —
    o objetivo é dar contexto útil no AuditLog, não uma reconciliação
    perfeita item-a-item."""
    if len(old_items) != len(new_snapshots):
        return ["items_replaced"]
    change_types: set[str] = set()
    for old, new in zip(old_items, new_snapshots):
        if old["professional_id"] != str(new.professional_id):
            change_types.add("professional_change")
        if old["service_id"] != str(new.service_id):
            change_types.add("service_change")
        if old["start_at"] != new.start_at.isoformat() or old["end_at"] != new.end_at.isoformat():
            change_types.add("reschedule")
    return sorted(change_types) or ["no_op"]


def update_status(
    session: Session, actor: ActorContext, appointment_id: uuid.UUID, target_status: AppointmentStatus
) -> Appointment:
    appointment = get_appointment(session, actor, appointment_id)
    old_status = appointment.status
    new_status = next_status(old_status, target_status)
    appointment.status = new_status
    appointment.updated_by = actor.user_id
    session.flush()

    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id, entity_type="appointment",
        entity_id=appointment_id, action=AuditAction.UPDATE,
        old_values={"status": old_status.value},
        new_values={"status": new_status.value, "change_type": "status_change"},
    )
    return _reload(session, actor.organization_id, appointment_id)


def cancel_appointment(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Appointment:
    appointment = get_appointment(session, actor, appointment_id)
    assert_cancellable(appointment.status)
    old_status = appointment.status
    appointment.status = AppointmentStatus.CANCELLED
    appointment.updated_by = actor.user_id
    session.flush()

    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id, entity_type="appointment",
        entity_id=appointment_id, action=AuditAction.UPDATE,
        old_values={"status": old_status.value},
        new_values={"status": AppointmentStatus.CANCELLED.value, "change_type": "cancel"},
    )
    return _reload(session, actor.organization_id, appointment_id)
