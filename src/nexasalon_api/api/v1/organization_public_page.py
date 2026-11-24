from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.organization_public_page import (
    OrganizationPublicPageAnalyticsRead,
    OrganizationPublicPageRead,
    OrganizationPublicPageUpdate,
)
from nexasalon_api.services import (
    organization_public_page as organization_public_page_service,
)
from nexasalon_api.services import public_page_events as public_page_events_service

router = APIRouter(prefix="/organization/public-page", tags=["organization-public-page"])

# `organization.manage_public_page` — NOVA, DELIBERADAMENTE separada de
# `organization.manage` (migration 0060): nunca permite alterar o
# cadastro global da organização (nome/CNPJ/endereço/upload de logo
# continuam exclusivamente em `organization.manage`, `api/v1/
# organizations.py`, nunca aceitos aqui).
_manage = require_permission("organization.manage_public_page")


@router.get("", response_model=OrganizationPublicPageRead, summary="Consultar config da página pública (NexaLink)")
def get_public_page_config(
    session: Session = Depends(get_db), actor: ActorContext = Depends(_manage)
) -> OrganizationPublicPageRead:
    return organization_public_page_service.get_public_page_config(session, actor.organization_id)


@router.put(
    "",
    response_model=OrganizationPublicPageRead,
    summary="Salvar config + links da página pública (NexaLink) — transação única",
)
def update_public_page_config(
    payload: OrganizationPublicPageUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> OrganizationPublicPageRead:
    return organization_public_page_service.update_public_page(
        session, actor.organization_id, payload, updated_by=actor.user_id
    )


@router.get(
    "/analytics",
    response_model=OrganizationPublicPageAnalyticsRead,
    summary="Painel de Desempenho do NexaLink — KPIs, série diária, canais, origem, serviços",
)
def get_public_page_analytics(
    date_from: datetime | None = Query(default=None, description="Início do período (default: 7 dias atrás)"),
    date_to: datetime | None = Query(default=None, description="Fim do período (default: agora)"),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> OrganizationPublicPageAnalyticsRead:
    now = datetime.now(timezone.utc)
    effective_to = date_to or now
    effective_from = date_from or (effective_to - timedelta(days=7))
    return public_page_events_service.get_analytics(
        session, actor.organization_id, date_from=effective_from, date_to=effective_to
    )
