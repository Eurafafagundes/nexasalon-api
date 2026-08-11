import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_current_actor, get_db
from nexasalon_api.core.dev_auth import ActorContext
from nexasalon_api.schemas.professional import (
    ProfessionalCreate,
    ProfessionalRead,
    ProfessionalServiceRead,
    ProfessionalServicesReplaceRequest,
    ProfessionalUpdate,
    WorkingHourRead,
    WorkingHoursReplaceRequest,
)
from nexasalon_api.services import professionals as professionals_service

router = APIRouter(prefix="/professionals", tags=["professionals"])


@router.get("", response_model=list[ProfessionalRead], summary="Listar profissionais")
def list_professionals(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[ProfessionalRead]:
    professionals = professionals_service.list_professionals(session, actor.organization_id, include_inactive)
    return [ProfessionalRead.model_validate(p) for p in professionals]


@router.post(
    "", response_model=ProfessionalRead, status_code=status.HTTP_201_CREATED, summary="Criar profissional"
)
def create_professional(
    payload: ProfessionalCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ProfessionalRead:
    professional = professionals_service.create_professional(session, actor.organization_id, payload)
    return ProfessionalRead.model_validate(professional)


@router.get("/{professional_id}", response_model=ProfessionalRead, summary="Detalhar profissional")
def get_professional(
    professional_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ProfessionalRead:
    professional = professionals_service.get_professional(session, actor.organization_id, professional_id)
    return ProfessionalRead.model_validate(professional)


@router.put("/{professional_id}", response_model=ProfessionalRead, summary="Editar profissional")
def update_professional(
    professional_id: uuid.UUID,
    payload: ProfessionalUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ProfessionalRead:
    professional = professionals_service.update_professional(
        session, actor.organization_id, professional_id, payload
    )
    return ProfessionalRead.model_validate(professional)


@router.patch("/{professional_id}/activate", response_model=ProfessionalRead, summary="Ativar profissional")
def activate_professional(
    professional_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ProfessionalRead:
    professional = professionals_service.set_professional_active(
        session, actor.organization_id, professional_id, True
    )
    return ProfessionalRead.model_validate(professional)


@router.patch(
    "/{professional_id}/deactivate", response_model=ProfessionalRead, summary="Desativar profissional"
)
def deactivate_professional(
    professional_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ProfessionalRead:
    professional = professionals_service.set_professional_active(
        session, actor.organization_id, professional_id, False
    )
    return ProfessionalRead.model_validate(professional)


@router.get(
    "/{professional_id}/working-hours",
    response_model=list[WorkingHourRead],
    summary="Ver jornada semanal do profissional",
)
def list_working_hours(
    professional_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[WorkingHourRead]:
    rows = professionals_service.list_working_hours(session, actor.organization_id, professional_id)
    return [WorkingHourRead.model_validate(r) for r in rows]


@router.put(
    "/{professional_id}/working-hours",
    response_model=list[WorkingHourRead],
    summary="Definir jornada semanal do profissional (substitui a jornada inteira)",
)
def replace_working_hours(
    professional_id: uuid.UUID,
    payload: WorkingHoursReplaceRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[WorkingHourRead]:
    rows = professionals_service.replace_working_hours(
        session, actor.organization_id, professional_id, payload.items
    )
    return [WorkingHourRead.model_validate(r) for r in rows]


@router.get(
    "/{professional_id}/services",
    response_model=list[ProfessionalServiceRead],
    summary="Ver serviços que o profissional executa",
)
def list_professional_services(
    professional_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[ProfessionalServiceRead]:
    rows = professionals_service.list_professional_services(session, actor.organization_id, professional_id)
    return [ProfessionalServiceRead.model_validate(r) for r in rows]


@router.put(
    "/{professional_id}/services",
    response_model=list[ProfessionalServiceRead],
    summary="Definir serviços do profissional, com overrides (substitui o conjunto inteiro)",
)
def replace_professional_services(
    professional_id: uuid.UUID,
    payload: ProfessionalServicesReplaceRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[ProfessionalServiceRead]:
    rows = professionals_service.replace_professional_services(
        session, actor.organization_id, professional_id, payload.items
    )
    return [ProfessionalServiceRead.model_validate(r) for r in rows]
