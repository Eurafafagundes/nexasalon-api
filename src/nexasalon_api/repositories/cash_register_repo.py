import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.cash_register import CashRegister
from nexasalon_api.models.enums import CashRegisterStatus


def get(session: Session, organization_id: uuid.UUID, register_id: uuid.UUID) -> CashRegister | None:
    stmt = select(CashRegister).where(
        CashRegister.id == register_id, CashRegister.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def get_open_for_branch(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> CashRegister | None:
    """Regra desta rodada (mudou da 0014, ver docstring do model): só um
    caixa aberto POR UNIDADE, não por usuário — qualquer usuário com
    `finance.manage` pode fechar um caixa que outro abriu (item
    "fechamento por usuário diferente" é esperado, não bug)."""
    stmt = select(CashRegister).where(
        CashRegister.organization_id == organization_id,
        CashRegister.branch_id == branch_id,
        CashRegister.status == CashRegisterStatus.OPEN,
    )
    return session.scalars(stmt).first()


def list_open(session: Session, organization_id: uuid.UUID) -> list[CashRegister]:
    """Caixas abertos da organização inteira — usado tanto pela tela
    "Caixas Abertos" quanto pelo seletor de caixa na hora de registrar
    um pagamento (item "pagamento obrigatoriamente vinculado ao
    caixa")."""
    stmt = (
        select(CashRegister)
        .where(CashRegister.organization_id == organization_id, CashRegister.status == CashRegisterStatus.OPEN)
        .order_by(CashRegister.created_at)
    )
    return list(session.scalars(stmt).all())


def list_for_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    status: CashRegisterStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    opened_by: uuid.UUID | None = None,
) -> list[CashRegister]:
    """Histórico de caixas (item "Histórico de Caixas") — filtros por
    data/responsável/status, todos opcionais."""
    stmt = select(CashRegister).where(CashRegister.organization_id == organization_id)
    if status is not None:
        stmt = stmt.where(CashRegister.status == status)
    if date_from is not None:
        stmt = stmt.where(CashRegister.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(CashRegister.created_at < date_to)
    if opened_by is not None:
        stmt = stmt.where(CashRegister.opened_by == opened_by)
    stmt = stmt.order_by(CashRegister.created_at.desc())
    return list(session.scalars(stmt).all())


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    branch_id: uuid.UUID,
    opened_by: uuid.UUID,
    opened_by_name: str,
    initial_amount: Decimal,
    opening_notes: str | None,
) -> CashRegister:
    register = CashRegister(
        organization_id=organization_id,
        branch_id=branch_id,
        opened_by=opened_by,
        opened_by_name=opened_by_name,
        initial_amount=initial_amount,
        opening_notes=opening_notes,
    )
    session.add(register)
    session.flush()
    return register
