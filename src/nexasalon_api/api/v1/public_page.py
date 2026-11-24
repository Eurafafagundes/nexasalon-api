"""Rotas PÚBLICAS do NexaLink — SEM autenticação, prefixo
`/public/s/{organization_slug}`. Nenhuma rota aqui usa `get_current_actor`/
`get_db`/`require_permission` — a dependency raiz é `get_public_page_context`
(resolve a organização pelo slug), e CADA rota chama explicitamente o
rate limit certo (navegação vs. eventos têm limites bem diferentes —
ver `api/deps.py`, nunca embutido na dependency raiz)."""
from fastapi import APIRouter, Depends, Request, status

from nexasalon_api.api.deps import (
    PublicPageContext,
    get_public_page_context,
    rate_limit_public_page_event,
    rate_limit_public_page_nav,
)
from nexasalon_api.schemas.public_page import PublicPageEventCreate, PublicPageRead
from nexasalon_api.services import public_page as public_page_service
from nexasalon_api.services import public_page_events as public_page_events_service

router = APIRouter(prefix="/public/s/{organization_slug}", tags=["public-page"])


@router.get("", response_model=PublicPageRead, summary="Dados públicos da página NexaLink")
def get_public_page(
    request: Request, ctx: PublicPageContext = Depends(get_public_page_context)
) -> PublicPageRead:
    rate_limit_public_page_nav(request)
    return public_page_service.get_public_page(ctx.session, ctx.organization)


@router.post(
    "/events",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Registrar visualização/clique na página NexaLink",
)
def record_public_page_event(
    payload: PublicPageEventCreate,
    request: Request,
    ctx: PublicPageContext = Depends(get_public_page_context),
) -> None:
    rate_limit_public_page_event(request)
    public_page_events_service.record_event(ctx.session, ctx.organization.id, payload)
