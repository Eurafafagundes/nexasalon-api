import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import AuditAction
from nexasalon_api.schemas.client_observation import (
    ObservationCard,
    ObservationCreate,
    ObservationHistoryEntry,
    ObservationHistoryResponse,
    ObservationListResponse,
    ObservationOrigin,
    ObservationUpdate,
)
from nexasalon_api.services import client_observations as client_observations_service

router = APIRouter(prefix="/clients/{client_id}/observations", tags=["client-observations"])

_view = require_permission("clients.view")
_manage = require_permission("clients.manage")
_view_history = require_permission("clients.view_audit_log")

# A mesma chave guarda o conteúdo em `old_values`/`new_values` do
# `AuditLog`, por origem — ver `services/client_observations.py::
# _ENTITY_TYPE_BY_ORIGIN` e os `audit_log_repo.create(...)` em
# `services/clients.py`/`appointments.py`/`orders.py`.
_CONTENT_KEY_BY_ORIGIN = {"general": "content", "appointment": "notes", "order": "observation"}


def _to_card(data) -> ObservationCard:
    return ObservationCard(
        id=data.id,
        origin=data.origin,
        order_number=data.order_number,
        appointment_starts_at=data.appointment_starts_at,
        appointment_id=data.appointment_id,
        content=data.content,
        version=data.version,
        created_at=data.created_at,
        created_by_name=data.created_by_name,
        updated_at=data.updated_at,
        updated_by_name=data.updated_by_name,
        is_legacy_import=data.is_legacy_import,
        can_edit=data.can_edit,
        can_view_history=data.can_view_history,
    )


@router.get("", response_model=ObservationListResponse, summary="Central de observações do cliente")
def list_observations(
    client_id: uuid.UUID,
    origin: ObservationOrigin | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ObservationListResponse:
    result = client_observations_service.list_observations(
        session, actor, client_id, origin=origin, search=search, page=page, page_size=page_size
    )
    return ObservationListResponse(
        items=[_to_card(c) for c in result.items], total=result.total, page=result.page, page_size=result.page_size
    )


@router.post(
    "", response_model=ObservationCard, status_code=status.HTTP_201_CREATED, summary="Criar observação geral"
)
def create_observation(
    client_id: uuid.UUID,
    payload: ObservationCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ObservationCard:
    observation = client_observations_service.create_general_observation(
        session, actor, client_id, payload.content
    )
    return ObservationCard(
        id=observation.id,
        origin="general",
        content=observation.content,
        version=observation.version,
        created_at=observation.created_at,
        created_by_name=observation.created_by_name,
        updated_at=observation.updated_at,
        updated_by_name=observation.updated_by_name,
        is_legacy_import=observation.is_legacy_import,
        can_edit=True,
        can_view_history="clients.view_audit_log" in actor.permissions,
    )


@router.patch(
    "/{observation_id}", response_model=ObservationCard, summary="Editar observação geral (concorrência otimista)"
)
def update_observation(
    client_id: uuid.UUID,
    observation_id: uuid.UUID,
    payload: ObservationUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ObservationCard:
    observation = client_observations_service.update_general_observation(
        session, actor, client_id, observation_id, payload.content, payload.expected_version
    )
    return ObservationCard(
        id=observation.id,
        origin="general",
        content=observation.content,
        version=observation.version,
        created_at=observation.created_at,
        created_by_name=observation.created_by_name,
        updated_at=observation.updated_at,
        updated_by_name=observation.updated_by_name,
        is_legacy_import=observation.is_legacy_import,
        can_edit=True,
        can_view_history="clients.view_audit_log" in actor.permissions,
    )


@router.get(
    "/history",
    response_model=ObservationHistoryResponse,
    summary="Histórico de auditoria de uma observação (geral, de agendamento ou de comanda)",
)
def get_observation_history(
    client_id: uuid.UUID,
    origin: ObservationOrigin,
    origin_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view_history),
) -> ObservationHistoryResponse:
    entries = client_observations_service.get_observation_history(session, actor, client_id, origin, origin_id)
    content_key = _CONTENT_KEY_BY_ORIGIN[origin]
    items = [
        ObservationHistoryEntry(
            action="create" if entry.action == AuditAction.CREATE else "update",
            at=entry.created_at,
            user_name=entry.user_name,
            old_content=(entry.old_values or {}).get(content_key),
            new_content=(entry.new_values or {}).get(content_key),
        )
        for entry in entries
    ]
    return ObservationHistoryResponse(items=items)
