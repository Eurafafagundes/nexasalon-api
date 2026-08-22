import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexasalon_api.models.customer_account import CustomerAccount, CustomerAccountLink


def get(session: Session, customer_account_id: uuid.UUID) -> CustomerAccount | None:
    return session.get(CustomerAccount, customer_account_id)


def get_by_email(session: Session, email: str) -> CustomerAccount | None:
    """`email` já deve vir normalizado (minúsculas, sem espaços) —
    `services/customer_accounts.py` é o único ponto de normalização."""
    stmt = select(CustomerAccount).where(func.lower(CustomerAccount.email) == email.lower())
    return session.scalars(stmt).first()


def get_by_google_subject(session: Session, google_subject: str) -> CustomerAccount | None:
    stmt = select(CustomerAccount).where(CustomerAccount.google_subject == google_subject)
    return session.scalars(stmt).first()


def create(session: Session, **fields) -> CustomerAccount:
    account = CustomerAccount(**fields)
    session.add(account)
    session.flush()
    return account


def save(session: Session, account: CustomerAccount) -> CustomerAccount:
    session.flush()
    return account


# ---------------------------------------------------------------------
# CustomerAccountLink — vínculo CustomerAccount <-> Client por Organization
# (Bloco 8: "não criar outro Client a cada Agendamento Online").
# ---------------------------------------------------------------------


def get_link(
    session: Session, customer_account_id: uuid.UUID, organization_id: uuid.UUID
) -> CustomerAccountLink | None:
    stmt = select(CustomerAccountLink).where(
        CustomerAccountLink.customer_account_id == customer_account_id,
        CustomerAccountLink.organization_id == organization_id,
    )
    return session.scalars(stmt).first()


def create_link(
    session: Session, *, customer_account_id: uuid.UUID, organization_id: uuid.UUID, client_id: uuid.UUID
) -> CustomerAccountLink:
    link = CustomerAccountLink(
        customer_account_id=customer_account_id,
        organization_id=organization_id,
        client_id=client_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(link)
    session.flush()
    return link
