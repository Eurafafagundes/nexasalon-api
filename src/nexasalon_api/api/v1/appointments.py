import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_any_permission, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.repositories import client_repo
from nexasalon_api.schemas.appointment import (
    AppointmentCreate,
    AppointmentItemUpdate,
    AppointmentRead,
    AppointmentRelatedRead,
    AppointmentReplace,
    AppointmentStatusUpdate,
)
from nexasalon_api.services import appointments as appointments_service

router = APIRouter(prefix="/appointments", tags=["appointments"])

_create = require_permission("agenda.create")
_edit = require_permission("agenda.edit")
_cancel = require_permission("agenda.cancel")
_view = require_any_permission("agenda.view_own", "agenda.view_all")


@router.post("", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED, summary="Criar agendamento")
def create_appointment(
    payload: AppointmentCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_create),
) -> AppointmentRead:
    appointment = appointments_service.create_appointment(session, actor, payload)
    return AppointmentRead.model_validate(appointment)


@router.get("/{appointment_id}", response_model=AppointmentRead, summary="Detalhar agendamento")
def get_appointment(
    appointment_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> AppointmentRead:
    appointment = appointments_service.get_appointment(session, actor, appointment_id)
    return AppointmentRead.model_validate(appointment)


@router.put("/{appointment_id}", response_model=AppointmentRead, summary="Editar agendamento (substitui itens)")
def replace_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentReplace,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_edit),
) -> AppointmentRead:
    appointment = appointments_service.replace_appointment(session, actor, appointment_id, payload)
    return AppointmentRead.model_validate(appointment)


@router.patch(
    "/{appointment_id}/items/{item_id}",
    response_model=AppointmentRead,
    summary="Editar preço/duração/profissional/horário de UM item (drawer da Agenda + drag-and-drop)",
)
def update_appointment_item(
    appointment_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: AppointmentItemUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_edit),
) -> AppointmentRead:
    appointment = appointments_service.update_appointment_item(session, actor, appointment_id, item_id, payload)
    return AppointmentRead.model_validate(appointment)


@router.get(
    "/{appointment_id}/related",
    response_model=AppointmentRelatedRead,
    summary="Agendamentos relacionados (mesma cliente, mesmo dia) — Etapa I",
)
def get_related_appointments(
    appointment_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> AppointmentRelatedRead:
    appointment = appointments_service.get_appointment(session, actor, appointment_id)
    related = appointments_service.get_related_appointments(session, actor, appointment_id)
    client = client_repo.get(session, actor.organization_id, appointment.client_id)
    return AppointmentRelatedRead(
        client_name=client.name if client is not None else "Cliente",
        related=[AppointmentRead.model_validate(a) for a in related],
    )


@router.patch(
    "/{appointment_id}/status", response_model=AppointmentRead, summary="Mudar status do agendamento"
)
def update_status(
    appointment_id: uuid.UUID,
    payload: AppointmentStatusUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_edit),
) -> AppointmentRead:
    appointment = appointments_service.update_status(
        session, actor, appointment_id, payload.status, scope=payload.scope
    )
    return AppointmentRead.model_validate(appointment)


@router.post("/{appointment_id}/cancel", response_model=AppointmentRead, summary="Cancelar agendamento")
def cancel_appointment(
    appointment_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_cancel),
) -> AppointmentRead:
    appointment = appointments_service.cancel_appointment(session, actor, appointment_id)
    return AppointmentRead.model_validate(appointment)
