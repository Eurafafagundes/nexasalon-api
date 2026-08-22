"""Rotas PÚBLICAS do Agendamento Online (Etapa K) — SEM autenticação,
prefixo `/public/booking/{organization_slug}`. Nenhuma rota aqui usa
`get_current_actor`/`get_db`/`require_permission` — a única dependency é
`get_public_context` (resolve a organização pelo slug, aplica rate
limit, ver `api/deps.py`)."""
import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, Query, Request

from nexasalon_api.api.deps import (
    CustomerActor,
    PublicBookingContext,
    get_current_customer,
    get_public_context,
    rate_limit_public_booking_create,
)
from nexasalon_api.core.config import settings
from nexasalon_api.core.exceptions import UnauthorizedError
from nexasalon_api.repositories import customer_account_repo
from nexasalon_api.schemas.customer_account import PublicMyAppointmentRead
from nexasalon_api.schemas.public_booking import (
    PublicAvailabilitySlotRead,
    PublicBookingCreate,
    PublicBookingRead,
    PublicOrganizationRead,
    PublicProfessionalRead,
    PublicServiceRead,
)
from nexasalon_api.services import appointments as appointments_service
from nexasalon_api.services import customer_accounts as customer_accounts_service
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
    customer: CustomerActor = Depends(get_current_customer),
) -> PublicBookingRead:
    """Etapa L, Blocos 4/5/8/11 — "Identificação" agora é um GATE de
    autenticação: só uma `CustomerAccount` válida (Bloco 9) confirma um
    agendamento, nunca mais um nome/telefone soltos no corpo da
    requisição (ver docstring de `PublicBookingCreate`). O `Client`
    usado é sempre resolvido/reaproveitado pela mesma conta na mesma
    organização (Bloco 8) — nunca um cadastro novo a cada reserva."""
    rate_limit_public_booking_create(request)
    account = customer_account_repo.get(ctx.session, customer.customer_account_id)
    if account is None or not account.is_active:
        raise UnauthorizedError("Conta inválida ou inativa.")

    client_id = customer_accounts_service.resolve_client_for_customer_account(
        ctx.session, ctx.organization, account
    )
    # Bloco 11 — anti-fake: teto de agendamentos FUTUROS ativos por
    # conta, checado ANTES de tentar reservar (nunca depois — não faz
    # sentido gastar o trabalho de resolver profissional/conflito só
    # pra rejeitar no fim).
    customer_accounts_service.assert_can_book_more(
        ctx.session,
        ctx.organization.id,
        client_id,
        limit=settings.max_active_future_appointments_per_customer,
    )

    branch = public_booking_service.get_default_branch(ctx.session, ctx.organization.id)
    appointment = appointments_service.create_public_appointment_for_customer(
        ctx.session,
        ctx.organization,
        branch_id=branch.id,
        professional_id=payload.professional_id,
        service_id=payload.service_id,
        start_at=payload.start_at,
        client_id=client_id,
    )
    return PublicBookingRead(
        id=appointment.id, status=appointment.status, starts_at=appointment.starts_at, ends_at=appointment.ends_at
    )


@router.get(
    "/me/appointments",
    response_model=list[PublicMyAppointmentRead],
    summary="Meus agendamentos (Bloco 10) — só os da CustomerAccount autenticada, nesta organização",
)
def list_my_appointments(
    ctx: PublicBookingContext = Depends(get_public_context),
    customer: CustomerActor = Depends(get_current_customer),
) -> list[PublicMyAppointmentRead]:
    account = customer_account_repo.get(ctx.session, customer.customer_account_id)
    if account is None or not account.is_active:
        raise UnauthorizedError("Conta inválida ou inativa.")
    return customer_accounts_service.list_my_appointments(ctx.session, ctx.organization, account)
