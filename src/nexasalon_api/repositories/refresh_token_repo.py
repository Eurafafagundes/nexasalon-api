import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.auth import RefreshToken


def get_by_hash(session: Session, token_hash: str) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    return session.scalars(stmt).first()


def create(
    session: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    token_hash: str,
    issued_at: datetime,
    expires_at: datetime,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user_id,
        organization_id=organization_id,
        membership_id=membership_id,
        token_hash=token_hash,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    session.add(token)
    session.flush()
    return token


def revoke(session: Session, token: RefreshToken, revoked_at: datetime) -> RefreshToken:
    token.revoked_at = revoked_at
    session.flush()
    return token


def mark_rotated(
    session: Session, old_token: RefreshToken, new_token: RefreshToken, revoked_at: datetime
) -> None:
    old_token.revoked_at = revoked_at
    old_token.replaced_by_id = new_token.id
    session.flush()


def revoke_all_for_user(session: Session, user_id: uuid.UUID, revoked_at: datetime) -> int:
    """Revoga TODOS os refresh tokens ativos do usuário — resposta a um
    incidente de segurança (ex.: reuse detection: alguém apresentou um
    refresh token já rotacionado/revogado, sinal de possível roubo)."""
    stmt = select(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
    )
    tokens = list(session.scalars(stmt).all())
    for token in tokens:
        token.revoked_at = revoked_at
    session.flush()
    return len(tokens)
