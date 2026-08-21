"""Rotas PÚBLICAS do Agendamento Online (Etapa K) — SEM autenticação,
prefixo `/public/booking/{organization_slug}`. Nenhuma rota aqui usa
`get_current_actor`/`get_db`/`require_permission` — a única dependency é
`get_public_context` (resolve a organização pelo slug, aplica rate
limit, ver `api/deps.py`)."""
import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, Query, Request

from nexasalon_api.api.deps import (
    PublicBookingContext,
    get_public_context,
    rate_limit_public_booking_create,
)
from nexasalon_api.schemas.public_booking import (
    PublicAvailabilitySlotRead,
    PublicBookingCreate,
    PublicBookingRead,
    PublicOrganizationRead,
    PublicProfessionalRead,
    PublicServiceRead,
)
from nexasalon_api.services import appointments as appointments_service
from nexasalon_api.services import public_booking as public_booking_service

router = APIRouter(prefix="/public/booking/{organization_slug}", tags=["public-booking"])


@router.get("", response_model=PublicOrganizationRead, summary="Dados públicos da organização")
def get_organization(ctx: PublicBookingContext = Depends(get_public_context)) -> PublicOrganizationRead:
    return PublicOrganizationRead.model_validate(ctx.organization)


@router.get("/services", response_model=list[PublicServiceRead], summary="Serviços habilitados para online")
def list_services(ctx: PublicBookingContext = Depends(get_public_context)) -> list[PublicServiceRead]:
    services = public_booking_service.list_public_services(ctx.session, ctx.organization.id)
    return [PublicServiceRead.model_validate(s) for s in services]


@router.get(
    "/professionals",
    response_model=list[PublicProfessionalRead],
    summary="Profissionais habilitados para online (opcionalmente filtrado por serviço)",
)
def list_professionals(
    service_id: uuid.UUID | None = Query(default=None),
    ctx: PublicBookingContext = Depends(get_public_context),
) -> list[PublicProfessionalRead]:
    professionals = public_booking_service.list_public_professionals(
        ctx.session, ctx.organization.id, service_id=service_id
    )
    return [PublicProfessionalRead.model_validate(p) for p in professionals]


@router.get(
    "/availability",
    response_model=list[PublicAvailabilitySlotRead],
    summary="Horários disponíveis (professional_id ausente = Qualquer profissional)",
)
def get_availability(
    service_id: uuid.UUID,
    date: date_type,
    professional_id: uuid.UUID | None = Query(default=None),
    ctx: PublicBookingContext = Depends(get_public_context),
) -> list[PublicAvailabilitySlotRead]:
    branch = public_booking_service.get_default_branch(ctx.session, ctx.organization.id)
    slots = public_booking_service.get_public_availability(
        ctx.session,
        ctx.organization.id,
        branch_id=branch.id,
        service_id=service_id,
        professional_id=professional_id,
        target_date=date,
    )
    return [PublicAvailabilitySlotRead(start_at=s.start_at, end_at=s.end_at) for s in slots]


@router.post("", response_model=PublicBookingRead, status_code=201, summary="Confirmar agendamento online")
def create_booking(
    payload: PublicBookingCreate,
    request: Request,
    ctx: PublicBookingContext = Depends(get_public_context),
) -> PublicBookingRead:
    rate_limit_public_booking_create(request)
    branch = public_booking_service.get_default_branch(ctx.session, ctx.organization.id)
    appointment = appointments_service.create_public_appointment(
        ctx.session,
        ctx.organization,
        branch_id=branch.id,
        professional_id=payload.professional_id,
        service_id=payload.service_id,
        start_at=payload.start_at,
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        client_email=payload.client_email,
    )
    return PublicBookingRead(
        id=appointment.id, status=appointment.status, starts_at=appointment.starts_at, ends_at=appointment.ends_at
    )
