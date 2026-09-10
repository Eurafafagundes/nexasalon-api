import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.enums import CashMovementType, ExpenseNature, PaymentMethod
from nexasalon_api.models.finance import FinancialCategory


def list_for_register(session: Session, organization_id: uuid.UUID, cash_register_id: uuid.UUID) -> list[CashMovement]:
    stmt = (
        select(CashMovement)
        .where(CashMovement.organization_id == organization_id, CashMovement.cash_register_id == cash_register_id)
        .order_by(CashMovement.created_at)
    )
    return list(session.scalars(stmt).all())


def list_for_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    type: CashMovementType | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[CashMovement]:
    """Extrato (item "Movimentações") — todas as entradas/despesas
    manuais da organização num período, independente de qual caixa."""
    stmt = select(CashMovement).where(CashMovement.organization_id == organization_id)
    if type is not None:
        stmt = stmt.where(CashMovement.type == type)
    if date_from is not None:
        stmt = stmt.where(CashMovement.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(CashMovement.created_at <= date_to)
    return list(session.scalars(stmt.order_by(CashMovement.created_at.desc())).all())


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    cash_register_id: uuid.UUID,
    type: CashMovementType,
    amount: Decimal,
    description: str,
    created_by: uuid.UUID,
    created_by_name: str,
    category: str | None = None,
    financial_category_id: uuid.UUID | None = None,
    fixed_expense_id: uuid.UUID | None = None,
    method: PaymentMethod = PaymentMethod.CASH,
) -> CashMovement:
    movement = CashMovement(
        organization_id=organization_id,
        cash_register_id=cash_register_id,
        type=type,
        amount=amount,
        description=description,
        category=category,
        financial_category_id=financial_category_id,
        fixed_expense_id=fixed_expense_id,
        method=method,
        created_by=created_by,
        created_by_name=created_by_name,
    )
    session.add(movement)
    session.flush()
    return movement


def sum_withdrawals_by_nature(
    session: Session, organization_id: uuid.UUID, *, date_from: datetime, date_to: datetime,
    branch_id: uuid.UUID | None = None,
    provisioned_fixed_category_branches: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
) -> dict[str, Decimal]:
    """Soma de `CashMovement` tipo WITHDRAWAL no período, agrupada pela
    NATUREZA da `FinancialCategory` vinculada — usada pelo painel
    "Resultado disponível" (Dashboard) pras linhas "(-) Custos
    variáveis"/"(-) Despesas fixas". Mesma janela de período
    (`created_at >= date_from AND created_at <= date_to`) já usada por
    `list_for_org` (fonte de `FinancialSummary.expenses`) — nunca uma
    segunda definição de "despesa do período".

    Lançamentos sem `financial_category_id` (todo o histórico anterior
    a esta feature, ou qualquer lançamento novo deixado sem categoria)
    ficam em `"unclassified"` — nunca contam como fixo nem variável."""
    stmt = (
        select(FinancialCategory.nature, CashMovement.fixed_expense_id.is_not(None), func.coalesce(func.sum(CashMovement.amount), 0))
        .select_from(CashMovement)
        .join(CashRegister, CashRegister.id == CashMovement.cash_register_id)
        .outerjoin(FinancialCategory, FinancialCategory.id == CashMovement.financial_category_id)
        .where(
            CashMovement.organization_id == organization_id,
            CashMovement.type == CashMovementType.WITHDRAWAL,
            CashMovement.created_at >= date_from,
            CashMovement.created_at <= date_to,
        )
        .group_by(FinancialCategory.nature, CashMovement.fixed_expense_id.is_not(None))
    )
    if branch_id is not None:
        stmt = stmt.where(CashRegister.branch_id == branch_id)
    if provisioned_fixed_category_branches:
        stmt = stmt.where(
            (FinancialCategory.nature != ExpenseNature.FIXED)
            | FinancialCategory.nature.is_(None)
            | CashMovement.fixed_expense_id.is_not(None)
            | tuple_(CashMovement.financial_category_id, CashRegister.branch_id).not_in(
                provisioned_fixed_category_branches
            )
        )
    totals: dict[str, Decimal] = {"legacy_fixed": Decimal("0"), "linked_fixed_payments": Decimal("0"), "variable": Decimal("0"), "unclassified": Decimal("0")}
    for nature, linked, total in session.execute(stmt).all():
        if linked:
            totals["linked_fixed_payments"] += Decimal(total)
            continue
        key = nature.value if isinstance(nature, ExpenseNature) else "unclassified"
        if key == "fixed":
            key = "legacy_fixed"
        totals[key] = Decimal(total)
    return totals
