"""Rotas de autenticação.

`/login`, `/select-organization`, `/refresh` e `/logout` são as ÚNICAS
rotas autenticadas por posse de credencial/token puro — não passam por
`get_current_actor`/`get_db` porque, por definição, ainda não há (ou não
é mais preciso ter) um `ActorContext` resolvido nesse ponto.
`services.auth` gerencia suas próprias sessões de banco internamente
(ver docstring de `services/auth.py`).

`/me` já é uma rota autenticada normal — usa `get_current_actor`/`get_db`
como qualquer outra rota de recurso.

TRANSPORTE DE TOKENS (decisão da Etapa 2D):
  - access_token: corpo JSON, `Bearer` no header `Authorization` — o
    frontend guarda em memória (nunca `localStorage`/`sessionStorage`),
    perdido ao fechar a aba/recarregar (por isso o refresh existe).
  - refresh_token: NUNCA aparece em JSON. É setado como cookie
    `HttpOnly` + `Secure` + `SameSite` (config em `core/config.py`) —
    inacessível a qualquer JavaScript, inclusive um XSS bem-sucedido no
    frontend não consegue roubá-lo. O preço disso é precisar de proteção
    CSRF nas rotas que dependem dele (`require_csrf_header`, ver
    `api/deps.py`), já que o browser anexa o cookie automaticamente.

CORS/CSRF quando frontend e API estão em domínios DIFERENTES:
  - O cookie do refresh token só é enviado cross-site pelo browser se
    `SameSite=None` (e por regra do browser, `Secure=True` junto) — ver
    `NEXASALON_REFRESH_COOKIE_SAMESITE`. Se frontend e API ficarem no
    mesmo domínio-pai (ex.: `app.nexasalon.com` + `api.nexasalon.com`),
    `SameSite=Lax` já basta e é mais seguro — prefira essa topologia.
  - CORS precisa de `allow_credentials=True` (main.py) para o browser
    sequer enviar/aceitar o cookie numa resposta cross-origin — e isso
    exige uma allowlist explícita de origins (nunca `*`), configurada em
    `NEXASALON_CORS_ALLOWED_ORIGINS`.
  - Com `SameSite=None`, o cookie passa a ser enviado em QUALQUER
    request cross-site (inclusive de sites maliciosos) — daí a
    necessidade do header CSRF customizado em refresh/logout.
"""
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import (
    get_current_actor,
    get_db,
    rate_limit_login,
    rate_limit_refresh,
    rate_limit_select_organization,
    require_csrf_header,
)
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.config import settings
from nexasalon_api.core.exceptions import UnauthorizedError
from nexasalon_api.repositories import organization_repo, user_repo
from nexasalon_api.schemas.auth import (
    AcceptInviteRequest,
    CurrentMembershipRead,
    CurrentOrganizationRead,
    CurrentUserRead,
    LoginRequest,
    LoginResponse,
    MeResponse,
    OrganizationChoiceRead,
    ResetPasswordRequest,
    SelectOrganizationRequest,
    TokenPairRead,
)
from nexasalon_api.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens_to_schema(tokens: auth_service.SessionTokens) -> TokenPairRead:
    return TokenPairRead(
        access_token=tokens.access_token,
        token_type=tokens.token_type,
        organization_id=tokens.organization_id,
        membership_id=tokens.membership_id,
    )


def _choices_to_schema(choices: list[auth_service.OrganizationChoice]) -> list[OrganizationChoiceRead]:
    return [
        OrganizationChoiceRead(
            organization_id=c.organization_id,
            organization_name=c.organization_name,
            membership_id=c.membership_id,
            role_id=c.role_id,
            role_name=c.role_name,
        )
        for c in choices
    ]


def _set_refresh_cookie(response: Response, raw_refresh_token: str) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=raw_refresh_token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Login com e-mail e senha",
    dependencies=[Depends(rate_limit_login)],
)
def login(payload: LoginRequest, response: Response) -> LoginResponse:
    result = auth_service.login(payload.email, payload.password)
    if result.tokens is not None:
        _set_refresh_cookie(response, result.tokens.refresh_token)
    return LoginResponse(
        status=result.status,
        tokens=_tokens_to_schema(result.tokens) if result.tokens else None,
        org_selection_token=result.org_selection_token,
        organizations=_choices_to_schema(result.organizations) if result.organizations else None,
    )


@router.post(
    "/select-organization",
    response_model=TokenPairRead,
    summary="Escolher organização (usuários com múltiplas empresas)",
    dependencies=[Depends(rate_limit_select_organization)],
)
def select_organization(payload: SelectOrganizationRequest, response: Response) -> TokenPairRead:
    tokens = auth_service.select_organization(payload.org_selection_token, payload.organization_id)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _tokens_to_schema(tokens)


@router.post(
    "/refresh",
    response_model=TokenPairRead,
    summary="Renovar sessão (rotaciona o refresh token do cookie)",
    dependencies=[Depends(require_csrf_header), Depends(rate_limit_refresh)],
)
def refresh(request: Request, response: Response) -> TokenPairRead:
    raw_refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if not raw_refresh_token:
        raise UnauthorizedError("Refresh token ausente (cookie não enviado).")
    tokens = auth_service.refresh(raw_refresh_token)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _tokens_to_schema(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout (revoga o refresh token e limpa o cookie)",
    dependencies=[Depends(require_csrf_header)],
)
def logout(request: Request, response: Response) -> None:
    raw_refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if raw_refresh_token:
        auth_service.logout(raw_refresh_token)
    _clear_refresh_cookie(response)


@router.post(
    "/accept-invite",
    response_model=TokenPairRead,
    summary="Aceitar convite: definir a própria senha e ativar a membership",
)
def accept_invite(payload: AcceptInviteRequest, response: Response) -> TokenPairRead:
    tokens = auth_service.accept_invite(payload.invite_token, payload.password)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _tokens_to_schema(tokens)


@router.post(
    "/reset-password",
    response_model=TokenPairRead,
    summary="Consumir o link de redefinição de senha gerado por um administrador",
)
def reset_password(payload: ResetPasswordRequest, response: Response) -> TokenPairRead:
    tokens = auth_service.reset_password(payload.reset_token, payload.password)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _tokens_to_schema(tokens)


@router.get("/me", response_model=MeResponse, summary="Usuário autenticado, organização atual e permissões")
def me(
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> MeResponse:
    user = user_repo.get(session, actor.user_id)
    organization = organization_repo.get(session, actor.organization_id)

    organizations = auth_service.list_my_organizations(actor.user_id)

    return MeResponse(
        user=CurrentUserRead(id=user.id, email=user.email, name=user.name),
        organization=CurrentOrganizationRead(
            id=organization.id, name=organization.name, slug=organization.slug
        ),
        membership=CurrentMembershipRead(
            id=actor.membership_id,
            role_id=actor.role_id,
            role_name=actor.role_name,
            professional_id=actor.professional_id,
        ),
        permissions=sorted(actor.permissions),
        organizations=_choices_to_schema(organizations),
    )
