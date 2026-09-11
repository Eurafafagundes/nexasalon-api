import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

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


class TaxRateHistoryStatus(str, Enum):
    """Nunca inclui "vigente" — o histórico paginado (`GET /tax-rates/
    history`) exclui explicitamente a linha vigente (ver
    `services/tax_rates.py::list_history`), então toda linha aqui é uma
    de duas coisas:

      - ENCERRADA: `competence_month` já é passado (ou já foi superada
        por uma competência mais recente que hoje) — tem
        `effective_until` sempre preenchido (a MESMA competência vigia
        até o mês anterior ao início da próxima versão cronológica).
      - PROGRAMADA: `competence_month` ainda é futuro em relação a
        hoje — nunca é tratada como "encerrada" só porque uma versão
        AINDA MAIS futura já foi cadastrada depois dela; o status
        reflete a relação com HOJE, não com a última versão cadastrada."""

    ENCERRADA = "encerrada"
    PROGRAMADA = "programada"


class TaxRateHistoryRow(BaseModel):
    id: uuid.UUID
    competence_month: date
    tax_rate: Decimal
    # Último mês em que esta versão vigorou — mês imediatamente anterior
    # ao `competence_month` da PRÓXIMA versão na sequência cronológica
    # completa (que pode ser a vigente, outra encerrada ou outra
    # programada). `None` só é possível pra uma linha PROGRAMADA que
    # ainda não tem nenhuma versão mais recente cadastrada depois dela
    # (vigência em aberto, "a partir de X").
    effective_until: date | None
    status: TaxRateHistoryStatus


class TaxRateHistoryPage(BaseModel):
    """Página do histórico (nunca inclui a vigente — ver
    `TaxRateHistoryStatus`). `page` no retorno é o valor REALMENTE usado
    (já ajustado/clampado pro intervalo válido se o `page` pedido tiver
    ficado fora de alcance após alguma mudança nos dados — nunca um
    erro nesse caso; histórico vazio sempre devolve `page=1`,
    `total_pages=0`, nunca `page=0`).

    `has_programmed` é calculado sobre o CONJUNTO COMPLETO (não só a
    página atual) — existe pra permitir que a UI rotule o card
    corretamente ("Ver histórico e programadas" vs. "Ver histórico de
    alíquotas") mesmo quando a página exibida no momento não tiver
    nenhuma linha `programada` (ela pode estar numa página diferente)."""

    items: list[TaxRateHistoryRow]
    page: int
    page_size: int
    total: int
    has_programmed: bool
    total_pages: int
