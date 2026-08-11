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
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership
from nexasalon_api.models.organization import Organization
from nexasalon_api.repositories import membership_repo, rbac_repo, refresh_token_repo, user_repo


@contextmanager
def _session_scoped_to_user(user_id: uuid.UUID) -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)}
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


def _issue_session_tokens(
    *, user_id: uuid.UUID, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> SessionTokens:
    access_token = create_access_token(
        user_id=user_id, organization_id=organization_id, membership_id=membership_id
    )
    raw_refresh = generate_opaque_token()
    now = datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)}
        )
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(organization_id)}
        )
        refresh_token_repo.create(
            session,
            user_id=user_id,
            organization_id=organization_id,
            membership_id=membership_id,
            token_hash=hash_opaque_token(raw_refresh),
            issued_at=now,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return SessionTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        organization_id=organization_id,
        membership_id=membership_id,
    )


def login(email: str, password: str) -> LoginResult:
    """Falha de usuário inexistente e falha de senha incorreta retornam a
    MESMA mensagem/erro (`UnauthorizedError` genérico) — não dá pra um
    atacante descobrir por tentativa se um e-mail existe na base."""
    with SessionLocal() as probe:
        user = user_repo.get_by_email(probe, email)

    if user is None or user.password_hash is None or not verify_password(password, user.password_hash):
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


def select_organization(org_selection_token: str, organization_id: uuid.UUID) -> SessionTokens:
    try:
        payload = decode_token(org_selection_token)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Token de seleção de organização inválido ou expirado.") from exc

    if payload.get("type") != TokenType.ORG_SELECTION.value:
        raise UnauthorizedError("Token inválido para esta operação.")

    user_id = uuid.UUID(payload["sub"])

    with _session_scoped_to_user(user_id) as session:
        membership = membership_repo.get_by_user_and_org(session, user_id, organization_id)
        if membership is None or membership.status != MembershipStatus.ACTIVE:
            raise ForbiddenError("Usuário sem membership ativa nesta organização.")
        membership_id = membership.id

    return _issue_session_tokens(
        user_id=user_id, organization_id=organization_id, membership_id=membership_id
    )


def refresh(raw_refresh_token: str) -> SessionTokens:
    """Rotação com detecção de reuso: cada refresh consome (revoga) o
    token apresentado e emite um novo. Se um token JÁ revogado for
    apresentado de novo, tratamos como possível vazamento/roubo e
    revogamos TODOS os refresh tokens ativos do usuário — a próxima
    tentativa de refresh de qualquer sessão antiga também falhará,
    forçando novo login."""
    token_hash = hash_opaque_token(raw_refresh_token)
    now = datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        token = refresh_token_repo.get_by_hash(session, token_hash)
        if token is None:
            session.commit()
            raise UnauthorizedError("Refresh token inválido.")

        if token.revoked_at is not None:
            revoked_count = refresh_token_repo.revoke_all_for_user(session, token.user_id, now)
            session.commit()
            raise UnauthorizedError(
                f"Refresh token já utilizado. {revoked_count} sessão(ões) revogada(s) por segurança."
            )

        if token.expires_at <= now:
            session.commit()
            raise UnauthorizedError("Refresh token expirado.")

        user_id = token.user_id
        organization_id = token.organization_id

        # A partir daqui já temos user_id/organization_id de uma linha de
        # banco confiável (não de input do cliente) — seguro escopar a
        # sessão com eles para reverificar a membership via RLS normal.
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)}
        )
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(organization_id)}
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
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        )
        refresh_token_repo.mark_rotated(session, token, new_token, now)

        access_token = create_access_token(
            user_id=user_id, organization_id=organization_id, membership_id=membership_id
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


def compute_effective_permissions(session: Session, membership: OrganizationMembership) -> frozenset[str]:
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
            raise ForbiddenError("Este convite já foi utilizado ou não está mais pendente.")

        user = user_repo.get(session, user_id)
        if user is None:
            raise UnauthorizedError("Convite inválido.")

        user.password_hash = hash_password(password)
        user_repo.save(session, user)

        membership.status = MembershipStatus.ACTIVE
        membership_repo.save(session, membership)

        organization_id = membership.organization_id

    return _issue_session_tokens(user_id=user_id, organization_id=organization_id, membership_id=membership_id)


def list_my_organizations(user_id: uuid.UUID) -> list[OrganizationChoice]:
    """Memberships ativas do usuário em todas as organizações — usado
    pelo `GET /auth/me` para montar o seletor de empresas do frontend."""
    with _session_scoped_to_user(user_id) as session:
        memberships = membership_repo.list_active_for_user(session, user_id)
        return _resolve_organization_choices(session, memberships)
