import uuid
from datetime import date

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.fixed_expense import (
    FixedExpenseCreate,
    FixedExpenseRead,
    FixedExpenseStatusUpdate,
    FixedExpenseSummary,
    FixedExpenseUpdate,
)
from nexasalon_api.services import fixed_expenses as service

router = APIRouter(prefix="/fixed-expenses", tags=["fixed-expenses"])
_view = require_permission("finance.view")
_manage = require_permission("finance.manage")


@router.get("", response_model=list[FixedExpenseRead])
def list_fixed_expenses(
    branch_id: uuid.UUID | None = None,
    competence: date | None = None,
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
):
    selected = competence or service.current_month()
    return service.list_expenses(
        session,
        actor,
        branch_id=branch_id,
        competence=selected.replace(day=1),
        include_inactive=include_inactive,
    )


@router.get("/summary", response_model=FixedExpenseSummary)
def fixed_expense_summary(
    branch_id: uuid.UUID | None = None,
    competence: date | None = None,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
):
    selected = competence or service.current_month()
    return service.summary(
        session, actor, branch_id=branch_id, competence=selected.replace(day=1)
    )


@router.post("", response_model=FixedExpenseRead, status_code=status.HTTP_201_CREATED)
def create_fixed_expense(
    payload: FixedExpenseCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    return service.create_expense(session, actor, payload)


@router.put("/{expense_id}", response_model=FixedExpenseRead)
def update_fixed_expense(
    expense_id: uuid.UUID,
    payload: FixedExpenseUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    return service.update_expense(session, actor, expense_id, payload)


@router.patch("/{expense_id}/status", response_model=FixedExpenseRead)
def update_fixed_expense_status(
    expense_id: uuid.UUID,
    payload: FixedExpenseStatusUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    return service.set_status(session, actor, expense_id, payload)
