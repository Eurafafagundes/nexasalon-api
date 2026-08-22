"""Espelha `repositories/refresh_token_repo.py` (funcionário) — mesma
técnica (token opaco por hash, rotação, revogação em massa por
reuso), tabela totalmente separada. Ver docstring de
`models/customer_account.py::CustomerRefreshToken`."""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.customer_account import CustomerRefreshToken


def get_by_hash(session: Session, token_hash: str) -> CustomerRefreshToken | None:
    stmt = select(CustomerRefreshToken).where(CustomerRefreshToken.token_hash == token_hash)
    return session.scalars(stmt).first()


def create(
    session: Session,
    *,
    customer_account_id: uuid.UUID,
    token_hash: str,
    issued_at: datetime,
    expires_at: datetime,
) -> CustomerRefreshToken:
    token = CustomerRefreshToken(
        customer_account_id=customer_account_id,
        token_hash=token_hash,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    session.add(token)
    session.flush()
    return token


def revoke(session: Session, token: CustomerRefreshToken, revoked_at: datetime) -> CustomerRefreshToken:
    token.revoked_at = revoked_at
    session.flush()
    return token


def mark_rotated(
    session: Session, old_token: CustomerRefreshToken, new_token: CustomerRefreshToken, revoked_at: datetime
) -> None:
    old_token.revoked_at = revoked_at
    old_token.replaced_by_id = new_token.id
    session.flush()


def revoke_all_for_account(session: Session, customer_account_id: uuid.UUID, revoked_at: datetime) -> int:
    """Revoga TODOS os refresh tokens ativos da conta — resposta a
    detecção de reuso (alguém apresentou um token já rotacionado/revogado,
    sinal de possível roubo). Mesma lógica de
    `refresh_token_repo.revoke_all_for_user`."""
    stmt = select(CustomerRefreshToken).where(
        CustomerRefreshToken.customer_account_id == customer_account_id,
        CustomerRefreshToken.revoked_at.is_(None),
    )
    tokens = list(session.scalars(stmt).all())
    for token in tokens:
        token.revoked_at = revoked_at
    session.flush()
    return len(tokens)
