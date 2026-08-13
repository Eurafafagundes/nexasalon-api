import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.enums import AppointmentStatus

# Regra ÚNICA e centralizada de "quais status de agendamento ocupam a
# agenda" — usada tanto pela disponibilidade (`list_busy_for_professional_on_range`)
# quanto pela checagem de conflito ao criar/editar (`list_conflicts`).
# Nenhum dos dois duplica esta lista; os dois importam esta constante.
#
#   SCHEDULED, CONFIRMED, WAITING, IN_PROGRESS  -> ocupam
#   FINISHED                                     -> NÃO ocupa (fica só
#       no histórico; um atendimento já finalizado não pode travar uma
#       consulta de disponibilidade/conflito futura)
#   CANCELLED, NO_SHOW                           -> NÃO ocupam
#
# Assimetria conhecida com o trigger do banco: `check_appointment_item_overlap`
# (migration 0004) só exclui CANCELLED/NO_SHOW da checagem de overlap —
# ele não sabe (ainda) que FINISHED também deveria ser excluído. Alinhar
# o trigger exigiria uma nova migration (CREATE OR REPLACE FUNCTION),
# fora do escopo deste ajuste (mantido sem novas migrations/mudança de
# schema, por pedido explícito). Na prática o risco é baixo — um item só
# vira FINISHED depois de passar por IN_PROGRESS, ou seja, seu horário
# já é passado/presente, não futuro — mas fica registrado aqui como
# item conhecido para uma eventual migration de alinhamento.
OCCUPYING_STATUSES = frozenset(
    {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.WAITING,
        AppointmentStatus.IN_PROGRESS,
    }
)
_OCCUPYING_STATUS_VALUES = [s.value for s in OCCUPYING_STATUSES]


def get(session: Session, organization_id: uuid.UUID, item_id: uuid.UUID) -> AppointmentItem | None:
    stmt = select(AppointmentItem).where(
        AppointmentItem.id == item_id, AppointmentItem.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_agenda(
    session: Session,
    organization_id: uuid.UUID,
    *,
    date_from: datetime,
    date_to: datetime,
    branch_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    status: AppointmentStatus | None = None,
) -> list[AppointmentItem]:
    """Lista itens (não appointments) — a granularidade certa pra
    filtrar por profissional/serviço e pra aplicar `agenda.view_own`
    (um item por profissional, não o appointment inteiro)."""
    effective_status = func.coalesce(AppointmentItem.status, Appointment.status)
    stmt = (
        select(AppointmentItem)
        .join(Appointment, Appointment.id == AppointmentItem.appointment_id)
        .options(selectinload(AppointmentItem.appointment))
        .where(
            AppointmentItem.organization_id == organization_id,
            AppointmentItem.start_at < date_to,
            AppointmentItem.end_at > date_from,
        )
    )
    if branch_id is not None:
        stmt = stmt.where(Appointment.branch_id == branch_id)
    if professional_id is not None:
        stmt = stmt.where(AppointmentItem.professional_id == professional_id)
    if service_id is not None:
        stmt = stmt.where(AppointmentItem.service_id == service_id)
    if status is not None:
        stmt = stmt.where(effective_status == status.value)
    stmt = stmt.order_by(AppointmentItem.start_at)
    return list(session.scalars(stmt).all())


def list_conflicts(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    exclude_appointment_id: uuid.UUID | None = None,
) -> list[AppointmentItem]:
    """Pré-checagem em nível de aplicação — o trigger
    `check_appointment_item_overlap` (migration 0004) continua sendo a
    barreira definitiva contra corrida de concorrência; isto aqui só
    devolve um erro amigável cedo, antes de ir ao banco tentar o INSERT."""
    effective_status = func.coalesce(AppointmentItem.status, Appointment.status)
    stmt = (
        select(AppointmentItem)
        .join(Appointment, Appointment.id == AppointmentItem.appointment_id)
        .where(
            AppointmentItem.organization_id == organization_id,
            AppointmentItem.professional_id == professional_id,
            effective_status.in_(_OCCUPYING_STATUS_VALUES),
            AppointmentItem.start_at < end_at,
            AppointmentItem.end_at > start_at,
        )
    )
    if exclude_appointment_id is not None:
        stmt = stmt.where(AppointmentItem.appointment_id != exclude_appointment_id)
    return list(session.scalars(stmt).all())


def list_busy_for_professional_on_range(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    range_start: datetime,
    range_end: datetime,
) -> list[AppointmentItem]:
    """Itens ocupados do profissional que se sobrepõem à janela — usado
    pelo cálculo de disponibilidade."""
    effective_status = func.coalesce(AppointmentItem.status, Appointment.status)
    stmt = (
        select(AppointmentItem)
        .join(Appointment, Appointment.id == AppointmentItem.appointment_id)
        .where(
            AppointmentItem.organization_id == organization_id,
            AppointmentItem.professional_id == professional_id,
            effective_status.in_(_OCCUPYING_STATUS_VALUES),
            AppointmentItem.start_at < range_end,
            AppointmentItem.end_at > range_start,
        )
        .order_by(AppointmentItem.start_at)
    )
    return list(session.scalars(stmt).all())


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    appointment_id: uuid.UUID,
    service_id: uuid.UUID,
    professional_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    duration_minutes: int,
    price,
) -> AppointmentItem:
    item = AppointmentItem(
        organization_id=organization_id,
        appointment_id=appointment_id,
        service_id=service_id,
        professional_id=professional_id,
        start_at=start_at,
        end_at=end_at,
        duration_minutes=duration_minutes,
        price=price,
    )
    session.add(item)
    session.flush()
    return item


def delete_for_appointment(session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID) -> None:
    stmt = select(AppointmentItem).where(
        AppointmentItem.organization_id == organization_id,
        AppointmentItem.appointment_id == appointment_id,
    )
    for item in session.scalars(stmt).all():
        session.delete(item)
    session.flush()
