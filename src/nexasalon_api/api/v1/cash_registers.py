import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import CashRegisterStatus
from nexasalon_api.schemas.cash_register import (
    CashMovementCreate,
    CashMovementRead,
    CashRegisterClose,
    CashRegisterDetail,
    CashRegisterOpen,
    CashRegisterRead,
    PaymentMethodTotal,
)
from nexasalon_api.schemas.order import PaymentRead
from nexasalon_api.services import cash_register as cash_register_service

router = APIRouter(prefix="/cash-registers", tags=["cash-registers"])

# Reaproveita o catálogo de permissions já existente (`finance.view`/
# `finance.manage`, migration 0007) — não cria chaves novas pra não
# duplicar conceito que o projeto já tinha, só nunca tinha usado.
_view = require_permission("finance.view")
_manage = require_permission("finance.manage")


def _to_detail(summary: cash_register_service.RegisterSummary) -> CashRegisterDetail:
    return CashRegisterDetail(
        cash_register=CashRegisterRead.from_model(summary.register),
        totals_by_method=[
            PaymentMethodTotal(method=method, total=total, count=count)
            for method, (total, count) in summary.totals_by_method.items()
        ],
        total_revenue=summary.total_revenue,
        cash_payments_total=summary.cash_payments_total,
        supplies_total=summary.supplies_total,
        withdrawals_total=summary.withdrawals_total,
        expected_cash_balance=summary.expected_cash_balance,
        orders_count=summary.orders_count,
        average_ticket=summary.average_ticket,
        total_entries=summary.total_entries,
        movements=[CashMovementRead.from_model(m) for m in summary.movements],
        payments=[PaymentRead.model_validate(p) for p in summary.payments],
    )


@router.post("", response_model=CashRegisterRead, status_code=status.HTTP_201_CREATED, summary="Abrir caixa")
def open_register(
    payload: CashRegisterOpen,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> CashRegisterRead:
    register = cash_register_service.open_register(
        session, actor, payload.branch_id, payload.initial_amount, payload.notes
    )
    return CashRegisterRead.from_model(register)


@router.get("", response_model=list[CashRegisterRead], summary="Listar caixas (abertos, ou histórico com filtros)")
def list_registers(
    status_filter: CashRegisterStatus | None = Query(None, alias="status"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    opened_by: uuid.UUID | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[CashRegisterRead]:
    registers = cash_register_service.list_registers(
        session, actor, status=status_filter, date_from=date_from, date_to=date_to, opened_by=opened_by
    )
    return [CashRegisterRead.from_model(r) for r in registers]


@router.get("/{register_id}", response_model=CashRegisterDetail, summary="Detalhe/resumo de um caixa")
def get_register(
    register_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> CashRegisterDetail:
    summary = cash_register_service.get_register_summary(session, actor, register_id)
    return _to_detail(summary)


@router.post("/{register_id}/movements", response_model=CashRegisterDetail, summary="Registrar entrada ou despesa")
def register_movement(
    register_id: uuid.UUID,
    payload: CashMovementCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> CashRegisterDetail:
    cash_register_service.register_movement(
        session, actor, register_id, payload.type, payload.amount, payload.description,
        category=payload.category, method=payload.method,
    )
    summary = cash_register_service.get_register_summary(session, actor, register_id)
    return _to_detail(summary)


@router.post("/{register_id}/close", response_model=CashRegisterDetail, summary="Fechar caixa")
def close_register(
    register_id: uuid.UUID,
    payload: CashRegisterClose,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> CashRegisterDetail:
    cash_register_service.close_register(session, actor, register_id, payload.counted_amount, payload.notes)
    summary = cash_register_service.get_register_summary(session, actor, register_id)
    return _to_detail(summary)
