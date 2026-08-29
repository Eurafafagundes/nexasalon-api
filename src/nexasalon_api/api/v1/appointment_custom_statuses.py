"""Rotas de status personalizado de agendamento
(`/api/v1/appointment-custom-statuses`).

Leitura aberta a qualquer autenticado (mesmo padrão de
`appointment_status_styles.py`) — qualquer membro precisa disto pra a
Agenda renderizar as etiquetas certas, não é configuração sensível.
Escrita do CATÁLOGO (criar/editar/ativar/desativar) exige
`settings.manage` — nenhuma permission nova (mesmo raciocínio de
`service_categories.py`)."""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_current_actor, get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.appointment_custom_status import (
    AppointmentCustomStatusCreate,
    AppointmentCustomStatusRead,
    AppointmentCustomStatusUpdate,
)
from nexasalon_api.services import appointment_custom_statuses as service

router = APIRouter(prefix="/appointment-custom-statuses", tags=["appointment-custom-statuses"])

_manage = require_permission("settings.manage")


@router.get("", response_model=list[AppointmentCustomStatusRead], summary="Listar status personalizados")
def list_appointment_custom_statuses(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[AppointmentCustomStatusRead]:
    custom_statuses = service.list_custom_statuses(session, actor.organization_id, include_inactive)
    return [AppointmentCustomStatusRead.model_validate(s) for s in custom_statuses]


@router.post(
    "", response_model=AppointmentCustomStatusRead, status_code=status.HTTP_201_CREATED,
    summary="Criar status personalizado",
)
def create_appointment_custom_status(
    payload: AppointmentCustomStatusCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> AppointmentCustomStatusRead:
    custom_status = service.create_custom_status(session, actor.organization_id, payload)
    return AppointmentCustomStatusRead.model_validate(custom_status)


@router.put("/{status_id}", response_model=AppointmentCustomStatusRead, summary="Editar status personalizado")
def update_appointment_custom_status(
    status_id: uuid.UUID,
    payload: AppointmentCustomStatusUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> AppointmentCustomStatusRead:
    custom_status = service.update_custom_status(session, actor.organization_id, status_id, payload)
    return AppointmentCustomStatusRead.model_validate(custom_status)


@router.patch(
    "/{status_id}/activate", response_model=AppointmentCustomStatusRead, summary="Ativar status personalizado"
)
def activate_appointment_custom_status(
    status_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> AppointmentCustomStatusRead:
    custom_status = service.set_custom_status_active(session, actor.organization_id, status_id, True)
    return AppointmentCustomStatusRead.model_validate(custom_status)


@router.patch(
    "/{status_id}/deactivate", response_model=AppointmentCustomStatusRead, summary="Desativar status personalizado"
)
def deactivate_appointment_custom_status(
    status_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> AppointmentCustomStatusRead:
    custom_status = service.set_custom_status_active(session, actor.organization_id, status_id, False)
    return AppointmentCustomStatusRead.model_validate(custom_status)
