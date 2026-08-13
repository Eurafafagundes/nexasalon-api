"""Dependencies compartilhadas das rotas.

`get_current_actor` decide, em tempo de execução, entre dois caminhos:

  - Autenticação REAL (padrão): decodifica o JWT de acesso do header
    `Authorization: Bearer <token>`, reconfirma no banco — a cada
    request, nunca a partir de um valor cacheado no token — que o
    usuário está ativo e a membership está ACTIVE, e recalcula as
    permissões efetivas do zero.
  - DEV ONLY (`core/dev_auth.py`): mantido apenas para testes/desenvolvimento
    local, exatamente como pedido ("mantendo o modo DEV apenas para
    testes locais"). Só é alcançável se `NEXASALON_DEV_AUTH_ENABLED=true`
    E `environment != production` — as mesmas duas barreiras da Etapa 2C
    continuam valendo, agora reforçadas por uma TERCEIRA: mesmo com a
    flag ligada, isso nunca é o caminho padrão — é preciso opt-in
    explícito no ambiente.

Nenhuma rota deve importar `dev_auth` ou `core.security` diretamente —
este módulo é o único seam de troca.
"""
import uuid
from collections.abc import Generator

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.dev_auth import get_current_actor_DEV_ONLY
from nexasalon_api.core.exceptions import ForbiddenError, UnauthorizedError
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.core.security import InvalidTokenError, TokenType, decode_token
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.repositories import membership_repo, professional_repo, rbac_repo, user_repo
from nexasalon_api.services.auth import compute_effective_permissions

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_real_current_actor(
    credentials: HTTPAuthorizationCredentials | None,
) -> ActorContext:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Token de acesso ausente.")

    try:
        payload = decode_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Token de acesso inválido ou expirado.") from exc

    if payload.get("type") != TokenType.ACCESS.value:
        raise UnauthorizedError("Tipo de token inválido para esta operação.")

    try:
        user_id = uuid.UUID(payload["sub"])
        organization_id = uuid.UUID(payload["org_id"])
        membership_id = uuid.UUID(payload["membership_id"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Token de acesso malformado.") from exc

    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)}
        )
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(organization_id)}
        )

        user = user_repo.get(session, user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Usuário inválido ou inativo.")

        # Cross-tenant e membership inativa são cortados aqui, sempre a
        # partir do banco — nunca do que o token "lembra". Um usuário da
        # organização A não consegue, mesmo manipulando IDs, chegar aos
        # dados da B: o token só é aceito para a org com a qual ele foi
        # emitido, e essa membership é reconferida a cada request.
        membership = membership_repo.get(session, membership_id)
        if (
            membership is None
            or membership.user_id != user_id
            or membership.organization_id != organization_id
        ):
            raise ForbiddenError("Membership associada a este token não é mais válida.")
        if membership.status != MembershipStatus.ACTIVE:
            raise ForbiddenError("Membership inativa — acesso revogado.")

        role = rbac_repo.get_role(session, membership.role_id)
        permissions = compute_effective_permissions(session, membership)
        professional = professional_repo.get_by_user(session, organization_id, user_id)

        actor = ActorContext(
            organization_id=organization_id,
            user_id=user_id,
            membership_id=membership_id,
            role_id=membership.role_id,
            role_name=role.name if role is not None else "",
            permissions=permissions,
            professional_id=professional.id if professional is not None else None,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return actor


def get_current_actor(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> ActorContext:
    if settings.dev_auth_enabled and settings.environment != "production":
        return get_current_actor_DEV_ONLY()
    return _get_real_current_actor(credentials)


def require_permission(permission_key: str):
    """Dependency factory de autorização. Uso numa rota:

        @router.post("/professionals")
        def create_professional(
            ...,
            actor: ActorContext = Depends(require_permission("professionals.manage")),
        ): ...

    O backend é sempre a autoridade final — esconder um botão no
    frontend não substitui esta checagem."""

    def _dependency(actor: ActorContext = Depends(get_current_actor)) -> ActorContext:
        if permission_key not in actor.permissions:
            raise ForbiddenError(f"Permissão '{permission_key}' é necessária para esta ação.")
        return actor

    return _dependency


def require_any_permission(*permission_keys: str):
    """Como `require_permission`, mas passa se o ator tiver PELO MENOS
    UMA das permissions informadas — uso típico: uma rota de leitura que
    aceita tanto `agenda.view_own` quanto `agenda.view_all` (a diferença
    de ESCOPO dentro da rota, não se ela é acessível, fica por conta do
    service layer)."""

    def _dependency(actor: ActorContext = Depends(get_current_actor)) -> ActorContext:
        if not any(key in actor.permissions for key in permission_keys):
            keys = "' ou '".join(permission_keys)
            raise ForbiddenError(f"Permissão '{keys}' é necessária para esta ação.")
        return actor

    return _dependency


def _client_ip(request: Request) -> str:
    # Atrás de um proxy/load balancer, isto precisará ler
    # `X-Forwarded-For` (primeiro IP da lista) em vez de `request.client`
    # — deixado como próximo passo junto do deploy real, documentado
    # aqui de propósito pra não ser esquecido.
    return request.client.host if request.client else "unknown"


def rate_limit_login(request: Request) -> None:
    if not settings.rate_limit_enabled:
        return
    rate_limiter.hit(
        f"login:{_client_ip(request)}",
        max_attempts=settings.rate_limit_login_max_attempts,
        window_seconds=settings.rate_limit_login_window_seconds,
    )


def rate_limit_refresh(request: Request) -> None:
    if not settings.rate_limit_enabled:
        return
    rate_limiter.hit(
        f"refresh:{_client_ip(request)}",
        max_attempts=settings.rate_limit_refresh_max_attempts,
        window_seconds=settings.rate_limit_refresh_window_seconds,
    )


def rate_limit_select_organization(request: Request) -> None:
    if not settings.rate_limit_enabled:
        return
    rate_limiter.hit(
        f"select_org:{_client_ip(request)}",
        max_attempts=settings.rate_limit_select_organization_max_attempts,
        window_seconds=settings.rate_limit_select_organization_window_seconds,
    )


def require_csrf_header(request: Request) -> None:
    """Mitigação CSRF para as duas rotas que dependem do cookie HttpOnly
    do refresh token (`/auth/refresh`, `/auth/logout`) — posse automática
    de cookie pelo browser é exatamente o vetor clássico de CSRF. Exige
    um header customizado simples de presença; não precisa ser um
    "segredo" porque:

      1. Um `<form>` cross-site (o vetor mais simples de CSRF) não
         consegue adicionar headers customizados a uma submissão.
      2. Um `fetch`/XHR cross-site com header customizado dispara
         preflight de CORS — e `cors_allowed_origins` (main.py) barra
         qualquer origin que não seja o frontend real.

    Login não precisa disso: não depende de nenhum cookie ambiente, o
    próprio corpo da requisição já é a prova de posse das credenciais."""
    if request.headers.get(settings.csrf_header_name) is None:
        raise ForbiddenError(
            f"Header '{settings.csrf_header_name}' é obrigatório nesta rota (proteção CSRF)."
        )


def get_db(actor: ActorContext = Depends(get_current_actor)) -> Generator[Session, None, None]:
    """Sessão por request. Seta `app.current_org_id` (e `app.current_user_id`,
    para as poucas policies com cláusula de auto-acesso) via `SET LOCAL` —
    escopado à transação da request, nunca vaza pra outra request que
    reuse a mesma conexão do pool. RLS é a segunda barreira: toda query
    de repository também filtra `organization_id` explicitamente."""
    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(actor.organization_id)}
        )
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(actor.user_id)}
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
