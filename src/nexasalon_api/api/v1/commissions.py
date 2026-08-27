import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_any_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.commission import (
    CommissionDetailRead,
    CommissionOverviewRead,
)
from nexasalon_api.services import commissions as commissions_service

router = APIRouter(prefix="/commissions", tags=["commissions"])

# Etapa C3 — aceita QUALQUER uma das duas (mesmo padrão de
# `agenda.view_own`/`agenda.view_all`, `api/v1/agenda.py`): a rota só
# decide SE é acessível; o ESCOPO dentro dela (todos os profissionais
# vs. só o próprio) é decidido no service layer
# (`services/commissions.py::can_view_professional_commissions`), nunca
# aqui. `commissions.manage` (fechamento/pagamento) fica reservado pra
# Etapa C4 — nenhuma rota usa ainda.
_view = require_any_permission("commissions.view_all", "commissions.view_own")


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
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> CommissionDetailRead:
    detail = commissions_service.get_detail(
        session, actor, professional_id, date_from=date_from, date_to=date_to
    )
    return CommissionDetailRead.model_validate(detail, from_attributes=True)
