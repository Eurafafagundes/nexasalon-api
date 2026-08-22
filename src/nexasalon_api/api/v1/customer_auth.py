"""Rotas da Conta da Cliente (Etapa L, Blocos 5/6/9) —
`/api/v1/customer-auth/*`. NUNCA usa `get_current_actor`/
`require_permission` (isso é RBAC de FUNCIONÁRIO) — a autenticação aqui
é `get_current_customer` (`api/deps.py`), um seam completamente separado
(Bloco 7: "não misturar cliente com funcionário").

TRANSPORTE DE TOKENS (ajuste pós-Etapa L — persistência segura da
sessão): mesma decisão de `api/v1/auth.py` (funcionário), cookie
TOTALMENTE separado:
  - access_token: corpo JSON, `Bearer` no header `Authorization` — o
    frontend guarda em memória, curto (`settings.customer_access_token_ttl_minutes`).
  - refresh_token: NUNCA aparece em JSON. Cookie `HttpOnly` + `Secure` +
    `SameSite` PRÓPRIO (`settings.customer_refresh_cookie_*`) — nunca o
    `refresh_cookie_name` de staff, nunca lido/aceito pelas rotas
    internas. `/customer-auth/refresh` e `/customer-auth/logout` exigem
    o mesmo header CSRF genérico de `/auth/refresh`/`/auth/logout`
    (`require_csrf_header` — mitigação, não identidade, seguro
    reaproveitar)."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import (
    CustomerActor,
    get_current_customer,
    get_customer_db,
    rate_limit_customer_login,
    rate_limit_customer_refresh,
    rate_limit_customer_register,
    require_csrf_header,
)
from nexasalon_api.core.config import settings
from nexasalon_api.core.exceptions import UnauthorizedError
from nexasalon_api.repositories import customer_account_repo
from nexasalon_api.schemas.customer_account import (
    CustomerAccountRead,
    CustomerAuthResult,
    CustomerGoogleLoginRequest,
    CustomerLoginRequest,
    CustomerRegisterRequest,
    CustomerUpdateMeRequest,
)
from nexasalon_api.services import customer_accounts as customer_accounts_service
from nexasalon_api.services.customer_accounts import CustomerSessionTokens
from nexasalon_api.services.google_oauth import (
    GoogleIdentityVerifier,
    get_google_verifier,
    require_google_verifier,
)

router = APIRouter(prefix="/customer-auth", tags=["customer-auth"])


def _set_refresh_cookie(response: Response, raw_refresh_token: str) -> None:
    response.set_cookie(
        key=settings.customer_refresh_cookie_name,
        value=raw_refresh_token,
        httponly=True,
        secure=settings.customer_refresh_cookie_secure,
        samesite=settings.customer_refresh_cookie_samesite,
        path=settings.customer_refresh_cookie_path,
        domain=settings.customer_refresh_cookie_domain,
        max_age=settings.customer_refresh_token_ttl_days * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.customer_refresh_cookie_name,
        path=settings.customer_refresh_cookie_path,
        domain=settings.customer_refresh_cookie_domain,
    )


def _result(account, tokens: CustomerSessionTokens, *, phone_required: bool | None = None) -> CustomerAuthResult:
    return CustomerAuthResult(
        access_token=tokens.access_token,
        customer=CustomerAccountRead.model_validate(account),
        phone_required=(account.phone is None) if phone_required is None else phone_required,
    )


@router.post("/register", response_model=CustomerAuthResult, status_code=201, summary="Criar conta (Bloco 5)")
def register(
    payload: CustomerRegisterRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_customer_db),
) -> CustomerAuthResult:
    rate_limit_customer_register(request)
    account = customer_accounts_service.register(session, payload)
    tokens = customer_accounts_service.issue_session(session, account.id)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _result(account, tokens)


@router.post("/login", response_model=CustomerAuthResult, summary="Entrar com e-mail/senha (Bloco 9)")
def login(
    payload: CustomerLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_customer_db),
) -> CustomerAuthResult:
    rate_limit_customer_login(request)
    account = customer_accounts_service.login(session, email=payload.email, password=payload.password)
    tokens = customer_accounts_service.issue_session(session, account.id)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _result(account, tokens)


@router.post("/google", response_model=CustomerAuthResult, summary="Entrar/criar conta com Google (Bloco 6)")
def google_login(
    payload: CustomerGoogleLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_customer_db),
    verifier: GoogleIdentityVerifier | None = Depends(get_google_verifier),
) -> CustomerAuthResult:
    rate_limit_customer_login(request)
    verifier = require_google_verifier(verifier)
    identity = verifier.verify(payload.id_token)
    account = customer_accounts_service.login_or_register_with_google(session, identity)
    tokens = customer_accounts_service.issue_session(session, account.id)
    _set_refresh_cookie(response, tokens.refresh_token)
    return _result(account, tokens)


@router.post(
    "/refresh",
    response_model=CustomerAuthResult,
    summary="Renovar sessão (rotaciona o refresh token do cookie) — ajuste pós-Etapa L",
    dependencies=[Depends(require_csrf_header), Depends(rate_limit_customer_refresh)],
)
def refresh(request: Request, response: Response, session: Session = Depends(get_customer_db)) -> CustomerAuthResult:
    """Chamado no BOOT da página pública (`/agendar/[slug]`) pra restaurar
    a sessão da cliente a partir do cookie HttpOnly — sem isso, a cliente
    precisaria logar de novo a cada F5 (o access_token só vive em
    memória no frontend, exatamente como o de staff)."""
    raw_refresh_token = request.cookies.get(settings.customer_refresh_cookie_name)
    if not raw_refresh_token:
        raise UnauthorizedError("Sessão ausente (cookie não enviado).")
    tokens = customer_accounts_service.refresh_session(session, raw_refresh_token)
    _set_refresh_cookie(response, tokens.refresh_token)
    account = customer_account_repo.get(session, tokens.customer_account_id)
    if account is None:
        raise UnauthorizedError("Conta inválida.")
    return _result(account, tokens)


@router.post(
    "/logout",
    status_code=204,
    summary="Logout (revoga o refresh token desta sessão e limpa o cookie) — ajuste pós-Etapa L",
    dependencies=[Depends(require_csrf_header)],
)
def logout(request: Request, response: Response, session: Session = Depends(get_customer_db)) -> None:
    raw_refresh_token = request.cookies.get(settings.customer_refresh_cookie_name)
    if raw_refresh_token:
        customer_accounts_service.revoke_session(session, raw_refresh_token)
    _clear_refresh_cookie(response)


@router.get("/me", response_model=CustomerAccountRead, summary="Perfil da conta autenticada")
def me(
    customer: CustomerActor = Depends(get_current_customer),
    session: Session = Depends(get_customer_db),
) -> CustomerAccountRead:
    account = customer_account_repo.get(session, customer.customer_account_id)
    return CustomerAccountRead.model_validate(account)


@router.patch(
    "/me",
    response_model=CustomerAccountRead,
    summary="Completar WhatsApp após login (Bloco 6: 'pedir WhatsApp se necessário')",
)
def update_me(
    payload: CustomerUpdateMeRequest,
    customer: CustomerActor = Depends(get_current_customer),
    session: Session = Depends(get_customer_db),
) -> CustomerAccountRead:
    account = customer_account_repo.get(session, customer.customer_account_id)
    updated = customer_accounts_service.update_phone(session, account, payload.normalized_phone())
    return CustomerAccountRead.model_validate(updated)
