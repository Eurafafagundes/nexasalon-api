import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaxRateSet(BaseModel):
    """Upsert de uma linha de `organization_tax_rates`. `competence_month`
    aceita qualquer dia do mês desejado — normalizado para o primeiro
    dia na camada de serviço (nunca confiado à UI). `confirm_past=True`
    é OBRIGATÓRIO quando a competência informada já é anterior ao mês
    atual (edição de período passado) — sem isso a API recusa com 409,
    para nunca editar silenciosamente um período que o Dashboard já
    mostrou para alguém."""

    competence_month: date
    tax_rate: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    confirm_past: bool = False

    @field_validator("competence_month")
    @classmethod
    def _normalize_to_first_day(cls, value: date) -> date:
        return value.replace(day=1)


class TaxRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    competence_month: date
    tax_rate: Decimal
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime


class EffectiveTaxRateRead(BaseModel):
    """Resolução da alíquota vigente numa competência específica —
    devolve `None` em `tax_rate` quando a organização nunca configurou
    nenhuma alíquota até aquela competência (nunca inventa 0%)."""

    competence_month: date
    tax_rate: Decimal | None
    source_competence_month: date | None
