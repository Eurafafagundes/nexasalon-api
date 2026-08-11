import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.identity import User


def get(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def get_by_email(session: Session, email: str) -> User | None:
    # users é global (sem RLS) — não recebe filtro de organization_id.
    stmt = select(User).where(User.email == email.lower())
    return session.scalars(stmt).first()


def create(
    session: Session,
    *,
    email: str,
    name: str,
    password_hash: str | None = None,
    phone: str | None = None,
) -> User:
    user = User(email=email.lower(), name=name, password_hash=password_hash, phone=phone)
    session.add(user)
    session.flush()
    return user


def save(session: Session, user: User) -> User:
    session.flush()
    return user
