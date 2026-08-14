import uuid
from datetime import date as date_type
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.repositories import branch_repo, organization_repo
from nexasalon_api.schemas.schedule_block import ScheduleBlockCreate, ScheduleBlockRead
from nexasalon_api.services import availability as availability_service
from nexasalon_api.services import schedule_blocks as schedule_blocks_service

router = APIRouter(prefix="/schedule-blocks", tags=["schedule-blocks"])

# Ver bloqueios é parte normal de visualizar a Agenda — reaproveita as
# mesmas permissions de leitura da agenda, não cria uma nova de leitura.
_view = require_permission("agenda.view_all")
_manage = require_permission("agenda.manage_blocks")


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


@router.get("", response_model=list[ScheduleBlockRead], summary="Listar bloqueios de agenda num período")
def list_schedule_blocks(
    date: date_type | None = Query(None, description="Atalho: dia inteiro, no fuso da unidade/organização."),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    branch_id: uuid.UUID | None = Query(None),
    professional_id: uuid.UUID | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[ScheduleBlockRead]:
    if date is not None:
        range_start, range_end = _resolve_day_range(session, actor.organization_id, branch_id, date)
    elif date_from is not None and date_to is not None:
        range_start, range_end = date_from, date_to
    else:
        raise ValidationDomainError("Informe 'date', ou o par 'date_from' e 'date_to'.")

    blocks = schedule_blocks_service.list_schedule_blocks(
        session, actor.organization_id, range_start=range_start, range_end=range_end,
        branch_id=branch_id, professional_id=professional_id,
    )
    return [ScheduleBlockRead.model_validate(b) for b in blocks]


@router.post(
    "", response_model=ScheduleBlockRead, status_code=status.HTTP_201_CREATED, summary="Criar bloqueio de agenda"
)
def create_schedule_block(
    payload: ScheduleBlockCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ScheduleBlockRead:
    block = schedule_blocks_service.create_schedule_block(session, actor.organization_id, payload)
    return ScheduleBlockRead.model_validate(block)


@router.delete("/{block_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remover bloqueio de agenda")
def delete_schedule_block(
    block_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> None:
    schedule_blocks_service.delete_schedule_block(session, actor.organization_id, block_id)
