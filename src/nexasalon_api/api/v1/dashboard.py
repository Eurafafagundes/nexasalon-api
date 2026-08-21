import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.dashboard import DashboardKpiDetailResponse, DashboardOverviewResponse
from nexasalon_api.services import dashboard as dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# `dashboard.view` (migration 0026, Etapa G) — antes emprestava
# `reports.view` (comentário revertido: o Gerenciador de Acessos precisa
# de um toggle "Dashboard" próprio, independente de um futuro módulo de
# Relatórios). Seeded pra OWNER/ADMIN, fora do alcance de
# RECEPTIONIST/PROFESSIONAL — mesmo público de antes.
_view = require_permission("dashboard.view")


@router.get("/overview", response_model=DashboardOverviewResponse, summary="Dashboard — visão geral (KPIs + gráficos)")
def get_overview(
    date_from: datetime = Query(..., description="Início do período analisado (inclusive)."),
    date_to: datetime = Query(..., description="Fim do período analisado (EXCLUSIVE — passe o instante logo após o último dia incluído)."),
    compare_from: datetime | None = Query(None, description="Início do período comparativo (opcional)."),
    compare_to: datetime | None = Query(None, description="Fim do período comparativo (opcional, EXCLUSIVE)."),
    branch_id: uuid.UUID | None = Query(None, description="Filtra por unidade — omitido = todas as unidades da organização."),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> DashboardOverviewResponse:
    return dashboard_service.get_overview(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=compare_from, compare_to=compare_to,
    )


@router.get(
    "/kpi/{key}",
    response_model=DashboardKpiDetailResponse,
    summary="Dashboard — drill-down de um KPI (card clicado)",
)
def get_kpi_detail(
    key: str,
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    compare_from: datetime | None = Query(None),
    compare_to: datetime | None = Query(None),
    branch_id: uuid.UUID | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> DashboardKpiDetailResponse:
    return dashboard_service.get_kpi_detail(
        session, actor, key=key, branch_id=branch_id, date_from=date_from, date_to=date_to,
        compare_from=compare_from, compare_to=compare_to,
    )
