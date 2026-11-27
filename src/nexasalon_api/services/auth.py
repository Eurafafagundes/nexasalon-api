"""Serviço de autenticação — login, seleção de organização, refresh e
logout. Ver `core/security.py` para a estratégia de tokens (Argon2id +
JWT de acesso minimalista + refresh token opaco com rotação).

Este módulo gerencia suas PRÓPRIAS sessões de banco (`SessionLocal()`
diretamente), em vez de receber uma `Session` já aberta como os outros
serviços (`services/clients.py` etc.). Motivo: até o momento em que o
usuário e a organização são conhecidos, não existe ainda um
`ActorContext`/`get_db` — é este módulo que RESOLVE quem é o usuário e
qual `app.current_user_id`/`app.current_org_id` valem para o restante da
sessão (o mesmo padrão já usado em `core/dev_auth.py` para o seed DEV).

Variáveis de sessão Postgres relevantes (via `SET LOCAL`, escopo de
transação):
  - `app.current_user_id`: identidade do usuário autenticado. Habilita a
    cláusula de auto-acesso da policy de `organization_memberships`
    (migration 0006) — necessária para listar as memberships do próprio
    usuário ATRAVÉS de organizações, sem ainda ter uma org escolhida.
  - `app.current_org_id`: organização atual (igual ao já usado em
    `api/deps.get_db`).
"""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ForbiddenError, UnauthorizedError
from nexasalon_api.core.security import (
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_org_selection_token,
    decode_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from nexasalon_api.models.auth import RefreshToken
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership
from nexasalon_api.models.organization import Organization
from nexasalon_api.repositories import (
    membership_repo,
    rbac_repo,
    refresh_token_repo,
    user_repo,
)


@contextmanager
def _session_scoped_to_user(user_id: uuid.UUID) -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True)
class OrganizationChoice:
    organization_id: uuid.UUID
    organization_name: str
    membership_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str


@dataclass(frozen=True)
class SessionTokens:
    access_token: str
    refresh_token: str
    organization_id: uuid.UUID
    membership_id: uuid.UUID
    token_type: str = "bearer"


@dataclass(frozen=True)
class LoginResult:
    """`status == "session"` (1 única membership ativa): login já
    completo, `tokens` preenchido. `status == "select_organization"`
    (múltiplas): frontend deve escolher e chamar `select_organization`
    com `org_selection_token`."""

    status: Literal["session", "select_organization"]
    tokens: SessionTokens | None = None
    org_selection_token: str | None = None
    organizations: list[OrganizationChoice] | None = None


def _resolve_organization_choices(
    session: Session, memberships: list[OrganizationMembership]
) -> list[OrganizationChoice]:
    """Para cada membership (já visível via auto-acesso de
    `app.current_user_id`), busca o nome da organização e o nome do role.

    `organizations` mantém sua policy RLS original (só enxerga a org cujo
    id é exatamente `app.current_org_id`) — não foi alterada nesta etapa.
    Para ler o nome de VÁRIAS organizações diferentes na mesma
    transação, setamos `app.current_org_id` momentaneamente para cada
    org — id que já veio de uma coluna de banco confiável (a própria
    membership), nunca de entrada do cliente.
    """
    choices: list[OrganizationChoice] = []
    for membership in memberships:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(membership.organization_id)},
        )
        org = session.get(Organization, membership.organization_id)
        role = rbac_repo.get_role(session, membership.role_id)
        choices.append(
            OrganizationChoice(
                organization_id=membership.organization_id,
                organization_name=org.name if org is not None else "",
                membership_id=membership.id,
                role_id=membership.role_id,
                role_name=role.name if role is not None else "",
            )
        )
    return choices


def issue_session_tokens(
    session: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
) -> SessionTokens:
    """Persist session tokens in the caller's transaction, without committing."""
    access_token = create_access_token(
        user_id=user_id, organization_id=organization_id, membership_id=membership_id
    )
    raw_refresh = generate_opaque_token()
    now = datetime.now(timezone.utc)

    session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(user_id)},
    )
    session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"),
        {"oid": str(organization_id)},
    )
    refresh_token_repo.create(
        session,
        user_id=user_id,
        organization_id=organization_id,
        membership_id=membership_id,
        token_hash=hash_opaque_token(raw_refresh),
        issued_at=now,
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        session_started_at=now,
        last_used_at=now,
    )

    return SessionTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        organization_id=organization_id,
        membership_id=membership_id,
    )


def _issue_session_tokens(
    *, user_id: uuid.UUID, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> SessionTokens:
    with SessionLocal.begin() as session:
        return issue_session_tokens(
            session,
            user_id=user_id,
            organization_id=organization_id,
            membership_id=membership_id,
        )


def login(email: str, password: str) -> LoginResult:
    """Falha de usuário inexistente e falha de senha incorreta retornam a
    MESMA mensagem/erro (`UnauthorizedError` genérico) — não dá pra um
    atacante descobrir por tentativa se um e-mail existe na base."""
    with SessionLocal() as probe:
        user = user_repo.get_by_email(probe, email)

    if (
        user is None
        or user.password_hash is None
        or not verify_password(password, user.password_hash)
    ):
        raise UnauthorizedError("E-mail ou senha inválidos.")
    if not user.is_active:
        raise UnauthorizedError("E-mail ou senha inválidos.")

    with _session_scoped_to_user(user.id) as session:
        memberships = membership_repo.list_active_for_user(session, user.id)
        if not memberships:
            raise ForbiddenError("Usuário sem acesso ativo a nenhuma organização.")

        choices = _resolve_organization_choices(session, memberships)

        fresh_user = user_repo.get(session, user.id)
        if fresh_user is not None:
            fresh_user.last_login_at = datetime.now(timezone.utc)
            user_repo.save(session, fresh_user)

    if len(choices) == 1:
        chosen = choices[0]
        tokens = _issue_session_tokens(
            user_id=user.id,
            organization_id=chosen.organization_id,
            membership_id=chosen.membership_id,
        )
        return LoginResult(status="session", tokens=tokens)

    org_selection_token = create_org_selection_token(user_id=user.id)
    return LoginResult(
        status="select_organization",
        org_selection_token=org_selection_token,
        organizations=choices,
    )


def select_organization(
    org_selection_token: str, organization_id: uuid.UUID
) -> SessionTokens:
    try:
        payload = decode_token(org_selection_token)
    except InvalidTokenError as exc:
        raise UnauthorizedError(
            "Token de seleção de organização inválido ou expirado."
        ) from exc

    if payload.get("type") != TokenType.ORG_SELECTION.value:
        raise UnauthorizedError("Token inválido para esta operação.")

    user_id = uuid.UUID(payload["sub"])

    with _session_scoped_to_user(user_id) as session:
        membership = membership_repo.get_by_user_and_org(
            session, user_id, organization_id
        )
        if membership is None or membership.status != MembershipStatus.ACTIVE:
            raise ForbiddenError("Usuário sem membership ativa nesta organização.")
        membership_id = membership.id

    return _issue_session_tokens(
        user_id=user_id, organization_id=organization_id, membership_id=membership_id
    )


_REUSE_GRACE_MAX_CHAIN_HOPS = 20


def _walk_to_valid_tip(session: Session, token: RefreshToken) -> RefreshToken | None:
    """Segue `replaced_by_id` a partir de um token JÁ revogado até achar
    a ponta ainda válida (não revogada) da cadeia, travando cada salto
    (`FOR UPDATE`) — usado só dentro da janela de graça de reuso (ver
    `refresh`), nunca fora dela. Devolve `None` se a cadeia termina sem
    nenhuma ponta válida (ex.: a sessão foi mesmo revogada por completo
    depois — logout manual ou desativação — e não há nada pra devolver;
    o chamador cai no caminho de revogação total nesse caso)."""
    current: RefreshToken | None = token
    for _ in range(_REUSE_GRACE_MAX_CHAIN_HOPS):
        if current is None or current.replaced_by_id is None:
            return None
        current = refresh_token_repo.get_by_id_for_update(session, current.replaced_by_id)
        if current is not None and current.revoked_at is None:
            return current
    return None


def refresh(raw_refresh_token: str) -> SessionTokens:
    """Rotação com detecção de reuso: cada refresh consome (revoga) o
    token apresentado e emite um novo. Se um token JÁ revogado for
    apresentado de novo FORA da janela de graça (`refresh_token_reuse_grace_seconds`),
    tratamos como possível vazamento/roubo e revogamos TODOS os refresh
    tokens ativos do usuário — a próxima tentativa de refresh de
    qualquer sessão antiga também falhará, forçando novo login.

    DENTRO da janela de graça, o reuso é tratado como uma corrida
    benigna do próprio usuário/navegador (duas abas ou requisições
    apresentando o mesmo token quase ao mesmo tempo, sem mutex/lock no
    frontend, ou em um navegador sem suporte a `navigator.locks`) —
    seguimos a cadeia até a ponta ainda válida e rotacionamos a partir
    dela (nunca reusamos o segredo já consumido, que nunca fica
    armazenado em claro). Padrão reconhecido como "reuse interval"
    (ex.: Auth0), não um afrouxamento de segurança: só neutraliza
    corridas do próprio usuário, nunca ajuda quem apresente o cookie
    roubado fora dessa janela de poucos segundos.

    Além do limite de uso único, a sessão agora tem um limite ABSOLUTO
    (`refresh_token_ttl_days`, contado desde `session_started_at` —
    nunca resetado por rotação) e um limite por INATIVIDADE
    (`refresh_token_inactivity_hours`, contado desde `last_used_at`)."""
    token_hash = hash_opaque_token(raw_refresh_token)
    now = datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        # Lock de linha: serializa chamadas concorrentes apresentando o
        # MESMO token (a causa raiz dos logouts prematuros) — a segunda
        # chamada só destrava depois que a primeira comitar a rotação,
        # e então já vê `revoked_at` preenchido (cai na janela de graça
        # abaixo, em vez de uma corrida real).
        token = refresh_token_repo.get_by_hash_for_update(session, token_hash)
        if token is None:
            session.commit()
            raise UnauthorizedError("Refresh token inválido.")

        if token.revoked_at is not None:
            grace_deadline = token.revoked_at + timedelta(
                seconds=settings.refresh_token_reuse_grace_seconds
            )
            valid_tip = _walk_to_valid_tip(session, token) if now <= grace_deadline else None
            if valid_tip is None:
                revoked_count = refresh_token_repo.revoke_all_for_user(
                    session, token.user_id, now
                )
                session.commit()
                raise UnauthorizedError(
                    f"Refresh token já utilizado. {revoked_count} sessão(ões) revogada(s) por segurança."
                )
            # Corrida benigna: rotaciona a partir da ponta válida da
            # cadeia, nunca a partir do segredo já consumido.
            token = valid_tip

        if token.expires_at <= now:
            session.commit()
            raise UnauthorizedError("Sessão expirada — faça login novamente.")

        if now - token.last_used_at > timedelta(hours=settings.refresh_token_inactivity_hours):
            session.commit()
            raise UnauthorizedError("Sessão expirada por inatividade — faça login novamente.")

        user_id = token.user_id
        organization_id = token.organization_id

        # A partir daqui já temos user_id/organization_id de uma linha de
        # banco confiável (não de input do cliente) — seguro escopar a
        # sessão com eles para reverificar a membership via RLS normal.
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(organization_id)},
        )

        membership = membership_repo.get(session, token.membership_id)
        if (
            membership is None
            or membership.organization_id != organization_id
            or membership.user_id != user_id
        ):
            session.commit()
            raise ForbiddenError("Membership associada a este token não é mais válida.")
        if membership.status != MembershipStatus.ACTIVE:
            session.commit()
            raise ForbiddenError("Membership inativa — acesso revogado.")

        # Captura o id ANTES do commit/close: `expire_on_commit=True` (default)
        # expira os atributos do objeto, e o objeto fica preso à sessão que
        # está prestes a ser fechada — acessar `.id` depois disso levanta
        # `DetachedInstanceError`.
        membership_id = membership.id

        new_raw_refresh = generate_opaque_token()
        new_token = refresh_token_repo.create(
            session,
            user_id=user_id,
            organization_id=organization_id,
            membership_id=membership_id,
            token_hash=hash_opaque_token(new_raw_refresh),
            issued_at=now,
            # `session_started_at` propagado sem alteração — nunca
            # resetado por rotação, é o que sustenta o limite ABSOLUTO.
            expires_at=token.session_started_at + timedelta(days=settings.refresh_token_ttl_days),
            session_started_at=token.session_started_at,
            last_used_at=now,
        )
        refresh_token_repo.mark_rotated(session, token, new_token, now)

        access_token = create_access_token(
            user_id=user_id,
            organization_id=organization_id,
            membership_id=membership_id,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return SessionTokens(
        access_token=access_token,
        refresh_token=new_raw_refresh,
        organization_id=organization_id,
        membership_id=membership_id,
    )


def logout(raw_refresh_token: str) -> None:
    """Idempotente e silencioso: token inexistente/já revogado não
    levanta erro (evita confirmar pra quem não tem o token se ele é
    válido ou não)."""
    token_hash = hash_opaque_token(raw_refresh_token)
    now = datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        token = refresh_token_repo.get_by_hash(session, token_hash)
        if token is not None and token.revoked_at is None:
            refresh_token_repo.revoke(session, token, now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def compute_effective_permissions(
    session: Session, membership: OrganizationMembership
) -> frozenset[str]:
    """(permissões do Role) ∪ (overrides GRANT) − (overrides DENY),
    recalculado do zero a cada chamada — nunca cacheado no token, para
    que mudanças de role/override tenham efeito imediato na próxima
    requisição."""
    from nexasalon_api.models.enums import PermissionEffect

    granted = set(rbac_repo.list_role_permission_keys(session, membership.role_id))
    for override in rbac_repo.list_overrides(session, membership.id):
        if override.effect == PermissionEffect.GRANT:
            granted.add(override.permission_key)
        elif override.effect == PermissionEffect.DENY:
            granted.discard(override.permission_key)

    # Invariante de produto: "Criar/editar Agenda" nunca pode existir sem
    # NENHUMA permission de visualização — o gate de entrada da Agenda
    # (`_view_agenda` em api/v1/agenda.py, e o `canView` equivalente no
    # frontend) sempre exigiu `agenda.view_own`/`agenda.view_all`, nunca
    # aceitando `agenda.create`/`agenda.edit` como substituto (e isso não
    # muda aqui). Bug real corrigido: até esta normalização, o convite era
    # só no MOMENTO DO SAVE (`ManageAccessDrawer::toggleAgendaEdit`) — um
    # role ou override GRAVADO ANTES dessa correção (estado legado) ou
    # criado por qualquer caminho que não passe pelo drawer (role
    # customizado, ajuste direto de override) continuava com
    # create/edit sem view pra sempre, travando a Agenda mesmo com "as
    # permissões ativadas". Normalizando aqui — o ÚNICO lugar onde o
    # conjunto efetivo é computado, sempre do zero, nunca cacheado —
    # corrige automaticamente QUALQUER estado antigo/futuro nesse formato
    # a cada request, sem exigir migration/backfill nem novo save.
    if ("agenda.create" in granted or "agenda.edit" in granted) and not (
        "agenda.view_own" in granted or "agenda.view_all" in granted
    ):
        granted.add("agenda.view_own")

    return frozenset(granted)


def accept_invite(invite_token: str, password: str) -> SessionTokens:
    """Aceita um convite: decodifica o token, confere que a membership
    referenciada ainda está INVITED (convite não usado/expirado por
    estado, não só por `exp` do JWT), define a senha do usuário e ativa
    a membership — retornando uma sessão completa já logada (evita mais
    uma ida a /auth/login logo em seguida)."""
    try:
        payload = decode_token(invite_token)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Convite inválido ou expirado.") from exc

    if payload.get("type") != TokenType.INVITE.value:
        raise UnauthorizedError("Token inválido para esta operação.")

    try:
        user_id = uuid.UUID(payload["sub"])
        membership_id = uuid.UUID(payload["membership_id"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Convite malformado.") from exc

    with _session_scoped_to_user(user_id) as session:
        membership = membership_repo.get(session, membership_id)
        if membership is None or membership.user_id != user_id:
            raise UnauthorizedError("Convite inválido.")
        if membership.status != MembershipStatus.INVITED:
            raise ForbiddenError(
                "Este convite já foi utilizado ou não está mais pendente."
            )

        user = user_repo.get(session, user_id)
        if user is None:
            raise UnauthorizedError("Convite inválido.")

        user.password_hash = hash_password(password)
        user_repo.save(session, user)

        membership.status = MembershipStatus.ACTIVE
        membership_repo.save(session, membership)

        organization_id = membership.organization_id

    return _issue_session_tokens(
        user_id=user_id, organization_id=organization_id, membership_id=membership_id
    )


def reset_password(reset_token: str, password: str) -> SessionTokens:
    """Consome um token de redefinição de senha ACIONADO POR ADMIN (ver
    `services/user_management.py::admin_reset_password` — o admin nunca
    define nem vê a senha, só gera este link pra repassar). Ao contrário
    de `accept_invite`, a membership já está ACTIVE e continua ACTIVE —
    isto não é um convite, é só uma troca de senha. Retorna uma sessão
    completa (auto-login), mesma conveniência do accept-invite."""
    try:
        payload = decode_token(reset_token)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Link de redefinição inválido ou expirado.") from exc

    if payload.get("type") != TokenType.PASSWORD_RESET.value:
        raise UnauthorizedError("Token inválido para esta operação.")

    try:
        user_id = uuid.UUID(payload["sub"])
        membership_id = uuid.UUID(payload["membership_id"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Link de redefinição malformado.") from exc

    with _session_scoped_to_user(user_id) as session:
        membership = membership_repo.get(session, membership_id)
        if membership is None or membership.user_id != user_id:
            raise UnauthorizedError("Link de redefinição inválido.")
        if membership.status not in (
            MembershipStatus.ACTIVE,
            MembershipStatus.SUSPENDED,
        ):
            raise ForbiddenError(
                "Esta membership não está mais disponível para redefinição de senha."
            )

        user = user_repo.get(session, user_id)
        if user is None:
            raise UnauthorizedError("Link de redefinição inválido.")

        user.password_hash = hash_password(password)
        user_repo.save(session, user)

        organization_id = membership.organization_id
        membership_status = membership.status

    # Membership SUSPENSA: a senha foi trocada com sucesso, mas login
    # continua bloqueado (mesma checagem de sempre em `login()`/
    # `_get_real_current_actor`) — não faz sentido devolver uma sessão
    # que o próximo request derrubaria de qualquer forma.
    if membership_status != MembershipStatus.ACTIVE:
        raise ForbiddenError(
            "Senha redefinida, mas o acesso desta membership está suspenso."
        )

    return _issue_session_tokens(
        user_id=user_id, organization_id=organization_id, membership_id=membership_id
    )


def list_my_organizations(user_id: uuid.UUID) -> list[OrganizationChoice]:
    """Memberships ativas do usuário em todas as organizações — usado
    pelo `GET /auth/me` para montar o seletor de empresas do frontend."""
    with _session_scoped_to_user(user_id) as session:
        memberships = membership_repo.list_active_for_user(session, user_id)
        return _resolve_organization_choices(session, memberships)
