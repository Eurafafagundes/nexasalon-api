import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_any_permission, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.commission import (
    CommissionAdjustmentCreate,
    CommissionAdjustmentRead,
    CommissionDetailRead,
    CommissionOverviewRead,
    CommissionSettlementCreate,
    CommissionSettlementDetailRead,
    CommissionSettlementRead,
)
from nexasalon_api.services import commissions as commissions_service

router = APIRouter(prefix="/commissions", tags=["commissions"])

# Etapa C3 — aceita QUALQUER uma das duas (mesmo padrão de
# `agenda.view_own`/`agenda.view_all`, `api/v1/agenda.py`): a rota só
# decide SE é acessível; o ESCOPO dentro dela (todos os profissionais
# vs. só o próprio) é decidido no service layer
# (`services/commissions.py::can_view_professional_commissions`), nunca
# aqui. `commissions.manage` também autoriza leitura (quem pode pagar
# precisa poder ver) — nunca o contrário: leitura NUNCA autoriza pagar.
_view = require_any_permission("commissions.view_all", "commissions.view_own", "commissions.manage")
# Etapa C4 — só `commissions.manage` registra pagamento ou cria ajuste;
# `view_all`/`view_own` sozinhos são só-leitura (backend é a autoridade
# final, nunca só esconder o botão no frontend).
_manage = require_permission("commissions.manage")


@router.get("/overview", response_model=CommissionOverviewRead, summary="Comissões — visão geral por profissional")
def get_overview(
    date_from: datetime = Query(..., description="Início do período (competência = data de fechamento da comanda)."),
    date_to: datetime = Query(..., description="Fim do período (inclusive)."),
    professional_id: uuid.UUID | None = Query(
        default=None, description="Filtra por profissional — só respeitado com commissions.view_all."
    ),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> CommissionOverviewRead:
    overview = commissions_service.get_overview(
        session, actor, date_from=date_from, date_to=date_to, professional_id=professional_id
    )
    return CommissionOverviewRead.model_validate(overview, from_attributes=True)


@router.get(
    "/{professional_id}/detail",
    response_model=CommissionDetailRead,
    summary="Comissões — detalhamento por OrderItem de um profissional",
)
def get_detail(
    professional_id: uuid.UUID,
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    status: str | None = Query(
        default=None, description="Filtra a listagem: pending (a pagar) | paid | unconfigured. Omitido = todos."
    ),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> CommissionDetailRead:
    detail = commissions_service.get_detail(
        session, actor, professional_id, date_from=date_from, date_to=date_to, status=status
    )
    return CommissionDetailRead.model_validate(detail, from_attributes=True)


@router.get(
    "/{professional_id}/pending-adjustments",
    response_model=list[CommissionAdjustmentRead],
    summary="Comissões — ajustes ainda não incluídos em nenhum pagamento",
)
def get_pending_adjustments(
    professional_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[CommissionAdjustmentRead]:
    rows = commissions_service.get_pending_adjustments(session, actor, professional_id)
    return [CommissionAdjustmentRead.model_validate(row, from_attributes=True) for row in rows]


@router.post(
    "/adjustments",
    response_model=CommissionAdjustmentRead,
    status_code=201,
    summary="Comissões — criar ajuste manual auditável",
)
def create_adjustment(
    data: CommissionAdjustmentCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> CommissionAdjustmentRead:
    adjustment = commissions_service.create_adjustment(
        session,
        actor,
        professional_id=data.professional_id,
        amount=data.amount,
        reason=data.reason,
        order_item_id=data.order_item_id,
    )
    return CommissionAdjustmentRead.model_validate(adjustment, from_attributes=True)


@router.get(
    "/settlements",
    response_model=list[CommissionSettlementRead],
    summary="Comissões — histórico de pagamentos",
)
def list_settlements(
    professional_id: uuid.UUID | None = Query(default=None),
    date_from: datetime | None = Query(default=None, description="Filtra por data de PAGAMENTO (paid_at)."),
    date_to: datetime | None = Query(default=None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[CommissionSettlementRead]:
    rows = commissions_service.list_settlements(
        session, actor, professional_id=professional_id, date_from=date_from, date_to=date_to
    )
    return [CommissionSettlementRead.model_validate(row, from_attributes=True) for row in rows]


@router.get(
    "/settlements/{settlement_id}",
    response_model=CommissionSettlementDetailRead,
    summary="Comissões — detalhe de um pagamento (itens e ajustes incluídos)",
)
def get_settlement_detail(
    settlement_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> CommissionSettlementDetailRead:
    detail = commissions_service.get_settlement_detail(session, actor, settlement_id)
    return CommissionSettlementDetailRead.model_validate(detail, from_attributes=True)


@router.post(
    "/settlements",
    response_model=CommissionSettlementRead,
    status_code=201,
    summary="Comissões — registrar pagamento (fechar comissão de um profissional/período)",
)
def create_settlement(
    data: CommissionSettlementCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> CommissionSettlementRead:
    settlement = commissions_service.create_settlement(
        session,
        actor,
        professional_id=data.professional_id,
        date_from=data.date_from,
        date_to=data.date_to,
        adjustment_ids=data.adjustment_ids,
    )
    return CommissionSettlementRead.model_validate(settlement, from_attributes=True)
