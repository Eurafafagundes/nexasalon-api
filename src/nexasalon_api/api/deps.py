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
from nexasalon_api.services import agenda_access
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
            # Escopo granular de agenda — ver services/agenda_access.py.
            # `None` (ALL) é o caso comum e não bate no banco de novo além
            # do SELECT já feito acima para resolver `membership`.
            agenda_viewable_professional_ids=agenda_access.resolve_viewable_ids(session, membership),
            agenda_editable_professional_ids=agenda_access.resolve_editable_ids(session, membership),
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
    # `not in (staging, production)` — mesma trava dupla de
    # `core/config.py` (que já recusa a aplicação subir com dev_auth=true
    # nesses ambientes), reforçada aqui em runtime como segunda barreira.
    if settings.dev_auth_enabled and settings.environment not in ("staging", "production"):
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
    """IP do cliente para as chaves de rate limiting (`login:<ip>` etc.).

    Isto NÃO é uma verdade genérica sobre `X-Forwarded-For` — é uma
    decisão por PROVEDOR, por isso lida de `settings.client_ip_strategy`
    (ver docstring do campo em `core/config.py`) em vez de fixa no
    código. Revisão feita explicitamente a pedido do usuário: reavaliar
    se é seguro confiar cegamente na primeira posição do header antes
    de usá-la pra rate limiting.

    NÃO usamos o `ProxyHeadersMiddleware`/`--proxy-headers` genérico do
    uvicorn nem `--forwarded-allow-ips='*'`. Esse mecanismo assume a
    topologia mais comum, onde o proxy ANEXA o IP real ao FINAL da
    lista `X-Forwarded-For` e você configura quantos "saltos" confiar a
    partir da direita — o oposto do que a Render faz (ver abaixo), o
    que tornaria esse mecanismo genérico explorável aqui.

    O QUE FOI VERIFICADO (Etapa 3C):
    A topologia real, confirmada por documentação oficial da Render
    ("How Render handles DDoS attacks", render.com/articles, abr/2026):
    todo tráfego passa por Cloudflare e DEPOIS pelo Load Balancer da
    Render antes de chegar no processo da aplicação — não há rota
    direta até o uvicorn. O mesmo artigo (seção "Rate limiting"), ao
    ensinar como fazer rate limiting por IP na própria Render, usa
    exatamente `x-forwarded-for.split(',')[0]` como exemplo oficial —
    ou seja, a própria Render instrui os clientes a confiar na primeira
    posição do header pra esse fim. Um tópico do fórum de feedback da
    Render descreve o mecanismo por trás disso: a borda grava o IP real
    do cliente na primeira posição em toda requisição, mas NÃO limpa
    nada que o cliente já tenha mandado depois dela — um cliente
    malicioso pode mandar `X-Forwarded-For: 1.1.1.1, 2.2.2.2` e o header
    que a aplicação recebe fica `<ip-real>, 1.1.1.1, 2.2.2.2`.

    LIMITAÇÃO HONESTA: isso é documentação de produto/comunidade da
    Render, não uma especificação formal e assinada do header — a
    garantia depende da Render manter esse comportamento (posição 0
    sempre escrita pela borda, nunca pelo cliente) e não temos como
    validar isso de dentro da aplicação, só confiar no que está
    documentado. Por isso a posição 0 é a ÚNICA posição usada (nunca a
    última, nunca "N saltos a partir da direita", nunca a lista
    inteira) — e por isso a estratégia é trocável por config
    (`client_ip_strategy=socket_only`) sem alterar código, caso essa
    suposição precise ser revista (mudança de provedor, ou dúvida sobre
    a topologia atual). `socket_only` usa só o peer TCP direto
    (`request.client`) — na Render isso é o IP do próprio Load
    Balancer, igual pra todo mundo, o que faz o rate limiting por IP
    virar, na prática, um limite global do serviço; é o preço de não
    confiar em nenhum header.

    Em ambiente local/testes (sem proxy na frente), o header
    simplesmente não vem e o fallback pro peer TCP é o comportamento
    correto e esperado em qualquer uma das duas estratégias.
    """
    if settings.client_ip_strategy == "trust_first_proxy_hop":
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            real_ip = forwarded_for.split(",")[0].strip()
            if real_ip:
                return real_ip
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
