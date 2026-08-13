import uuid
from datetime import date as date_type
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_any_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.repositories import branch_repo, organization_repo
from nexasalon_api.schemas.agenda import AgendaItemRead, AvailabilitySlotRead
from nexasalon_api.services import agenda as agenda_service
from nexasalon_api.services import availability as availability_service

router = APIRouter(prefix="/agenda", tags=["agenda"])

# Leitura da agenda: quem tem QUALQUER uma das duas permissions de
# visualização entra na rota — o ESCOPO (só os próprios itens x todos)
# é resolvido dentro de `services/agenda.py`, não aqui.
_view_agenda = require_any_permission("agenda.view_own", "agenda.view_all")
# Consultar disponibilidade é um passo normal de quem VAI criar um
# agendamento — inclui `agenda.create` além das duas de visualização.
_view_availability = require_any_permission("agenda.view_own", "agenda.view_all", "agenda.create")


def _resolve_day_range(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None, target_date: date_type
) -> tuple[datetime, datetime]:
    if branch_id is not None and branch_repo.exists(session, organization_id, branch_id):
        tz = availability_service.effective_timezone(session, organization_id, branch_id)
    else:
        organization = organization_repo.get(session, organization_id)
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(organization.timezone)
    start = datetime.combine(target_date, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def _to_agenda_item_read(item) -> AgendaItemRead:
    return AgendaItemRead(
        id=item.id,
        appointment_id=item.appointment_id,
        branch_id=item.appointment.branch_id,
        client_id=item.appointment.client_id,
        service_id=item.service_id,
        professional_id=item.professional_id,
        start_at=item.start_at,
        end_at=item.end_at,
        duration_minutes=item.duration_minutes,
        price=item.price,
        status=item.status or item.appointment.status,
    )


@router.get("", response_model=list[AgendaItemRead], summary="Listar agenda (itens de agendamento no período)")
def get_agenda(
    date: date_type | None = Query(None, description="Atalho: dia inteiro, no fuso da unidade/organização."),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    branch_id: uuid.UUID | None = Query(None),
    professional_id: uuid.UUID | None = Query(None),
    service_id: uuid.UUID | None = Query(None),
    status: AppointmentStatus | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view_agenda),
) -> list[AgendaItemRead]:
    if date is not None:
        range_start, range_end = _resolve_day_range(session, actor.organization_id, branch_id, date)
    elif date_from is not None and date_to is not None:
        range_start, range_end = date_from, date_to
    else:
        raise ValidationDomainError("Informe 'date', ou o par 'date_from' e 'date_to'.")

    items = agenda_service.list_agenda(
        session, actor, date_from=range_start, date_to=range_end, branch_id=branch_id,
        professional_id=professional_id, service_id=service_id, status=status,
    )
    return [_to_agenda_item_read(item) for item in items]


@router.get(
    "/availability", response_model=list[AvailabilitySlotRead], summary="Horários disponíveis num dia"
)
def get_availability(
    branch_id: uuid.UUID,
    professional_id: uuid.UUID,
    service_id: uuid.UUID,
    date: date_type,
    slot_minutes: int = Query(15, description="Granularidade da grade de horários oferecidos (15 ou 30)."),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view_availability),
) -> list[AvailabilitySlotRead]:
    slots = availability_service.compute_availability(
        session, actor.organization_id, branch_id=branch_id, professional_id=professional_id,
        service_id=service_id, target_date=date, slot_minutes=slot_minutes,
    )
    return [AvailabilitySlotRead(start_at=s.start_at, end_at=s.end_at) for s in slots]
