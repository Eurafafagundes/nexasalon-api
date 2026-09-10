"""Rotas de alíquota de imposto provisionada (`/api/v1/tax-rates`) —
painel "Resultado disponível" (Dashboard).

Reaproveita a permission `organization.manage` já existente (mesma
fronteira de autorização de Configurações > Taxas de Pagamento —
`payment_fee_rules.py`) — nenhuma permission nova. Isto é uma
CONFIGURAÇÃO (provisão gerencial), nunca gera lançamento de caixa/
pagamento — ver docstring de `services/tax_rates.py`."""
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.tax_rate import EffectiveTaxRateRead, TaxRateRead, TaxRateSet
from nexasalon_api.services import tax_rates as tax_rates_service

router = APIRouter(prefix="/tax-rates", tags=["tax-rates"])

_view = require_permission("organization.manage")
_manage = require_permission("organization.manage")


@router.get("", response_model=list[TaxRateRead], summary="Listar alíquotas de imposto configuradas")
def list_tax_rates(
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[TaxRateRead]:
    rates = tax_rates_service.list_rates(session, actor.organization_id)
    return [TaxRateRead.model_validate(r) for r in rates]


@router.get(
    "/effective", response_model=EffectiveTaxRateRead, summary="Resolver a alíquota vigente numa competência"
)
def get_effective_tax_rate(
    competence_month: date,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> EffectiveTaxRateRead:
    normalized = competence_month.replace(day=1)
    effective = tax_rates_service.get_effective_rate(session, actor.organization_id, normalized)
    return EffectiveTaxRateRead(
        competence_month=normalized,
        tax_rate=effective.tax_rate if effective is not None else None,
        source_competence_month=effective.competence_month if effective is not None else None,
    )


@router.put("", response_model=TaxRateRead, summary="Criar ou editar a alíquota de uma competência")
def set_tax_rate(
    payload: TaxRateSet,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> TaxRateRead:
    rate = tax_rates_service.set_rate(session, actor, payload)
    return TaxRateRead.model_validate(rate)
