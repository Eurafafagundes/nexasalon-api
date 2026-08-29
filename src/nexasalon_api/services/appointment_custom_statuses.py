"""Camada de negócio de `AppointmentCustomStatus` — cada organização
cria e mantém suas próprias etiquetas (ver docstring do model). Mesmo
formato de `services/service_categories.py` (CRUD + desativar nunca
apaga)."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.appointment_custom_status import AppointmentCustomStatus
from nexasalon_api.repositories import appointment_custom_status_repo
from nexasalon_api.schemas.appointment_custom_status import (
    AppointmentCustomStatusCreate,
    AppointmentCustomStatusUpdate,
)


def list_custom_statuses(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[AppointmentCustomStatus]:
    return appointment_custom_status_repo.list_all(session, organization_id, include_inactive)


def get_custom_status(
    session: Session, organization_id: uuid.UUID, status_id: uuid.UUID
) -> AppointmentCustomStatus:
    custom_status = appointment_custom_status_repo.get(session, organization_id, status_id)
    if custom_status is None:
        raise NotFoundError("Status personalizado não encontrado.")
    return custom_status


def create_custom_status(
    session: Session, organization_id: uuid.UUID, data: AppointmentCustomStatusCreate
) -> AppointmentCustomStatus:
    return appointment_custom_status_repo.create(session, organization_id, **data.model_dump())


def update_custom_status(
    session: Session, organization_id: uuid.UUID, status_id: uuid.UUID, data: AppointmentCustomStatusUpdate
) -> AppointmentCustomStatus:
    custom_status = get_custom_status(session, organization_id, status_id)
    for field, value in data.model_dump().items():
        setattr(custom_status, field, value)
    return appointment_custom_status_repo.save(session, custom_status)


def set_custom_status_active(
    session: Session, organization_id: uuid.UUID, status_id: uuid.UUID, is_active: bool
) -> AppointmentCustomStatus:
    """Desativar não apaga a etiqueta nem desvincula agendamentos que já
    a usam (FK é SET NULL só quando a linha é de fato deletada, e não
    existe rota de delete) — só some da lista de seleção pra NOVAS
    atribuições."""
    custom_status = get_custom_status(session, organization_id, status_id)
    custom_status.is_active = is_active
    return appointment_custom_status_repo.save(session, custom_status)
