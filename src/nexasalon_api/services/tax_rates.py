"""Camada de negócio de `OrganizationTaxRate` — alíquota de imposto
provisionada, versionada por competência mensal (ver docstring do
model em `models/finance.py`). Usada exclusivamente pelo painel
"Resultado disponível" (Dashboard) para provisionar (nunca pagar/
lançar) imposto: `faturamento_aplicavel × alíquota_da_competência`.
"""
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError
from nexasalon_api.models.enums import AuditAction
from nexasalon_api.models.finance import OrganizationTaxRate
from nexasalon_api.repositories import audit_log_repo, tax_rate_repo, user_repo
from nexasalon_api.schemas.tax_rate import (
    TaxRateHistoryPage,
    TaxRateHistoryRow,
    TaxRateHistoryStatus,
    TaxRateSet,
)

HISTORY_PAGE_SIZE = 12


def _current_competence_month() -> date:
    return datetime.now(timezone.utc).date().replace(day=1)


def _resolve_user_name(session: Session, user_id: uuid.UUID) -> str:
    user = user_repo.get(session, user_id)
    return user.name if user is not None else "Usuário removido"


def list_rates(session: Session, organization_id: uuid.UUID) -> list[OrganizationTaxRate]:
    return tax_rate_repo.list_all(session, organization_id)


def _month_before(month: date) -> date:
    if month.month == 1:
        return date(month.year - 1, 12, 1)
    return date(month.year, month.month - 1, 1)


def list_history(session: Session, organization_id: uuid.UUID, page: int) -> TaxRateHistoryPage:
    """Histórico paginado (Configurações > Preferências > Alíquota de
    Imposto — card "Ver histórico") — EXCLUI a vigente (ver
    `TaxRateHistoryStatus`) e SEMPRE ordena da competência mais recente
    pra mais antiga, no máximo `HISTORY_PAGE_SIZE` linhas por página.

    A vigência (`effective_until`) de cada linha só pode ser calculada
    corretamente com o conjunto COMPLETO ordenado (o fim de uma versão
    é o início da PRÓXIMA — que pode estar numa página diferente) —
    por isso o cálculo acontece aqui, sobre todas as linhas, e só DEPOIS
    disso a lista é fatiada pra paginação. Nunca calculado no frontend."""
    current_month = _current_competence_month()
    # `list_all` já vem desc; construímos a ordem asc (mais antiga
    # primeiro) pra achar a vigente e o "próximo" de cada linha com um
    # único passe, sem reconsultar o banco.
    ascending = list(reversed(tax_rate_repo.list_all(session, organization_id)))

    vigente_id: uuid.UUID | None = None
    for rate in ascending:
        if rate.competence_month <= current_month:
            vigente_id = rate.id  # a última (mais recente) <= hoje vence as anteriores.

    rows: list[TaxRateHistoryRow] = []
    for index, rate in enumerate(ascending):
        if rate.id == vigente_id:
            continue
        next_rate = ascending[index + 1] if index + 1 < len(ascending) else None
        effective_until = _month_before(next_rate.competence_month) if next_rate is not None else None
        status = (
            TaxRateHistoryStatus.PROGRAMADA
            if rate.competence_month > current_month
            else TaxRateHistoryStatus.ENCERRADA
        )
        rows.append(
            TaxRateHistoryRow(
                id=rate.id,
                competence_month=rate.competence_month,
                tax_rate=rate.tax_rate,
                effective_until=effective_until,
                status=status,
            )
        )

    rows.sort(key=lambda row: row.competence_month, reverse=True)

    total = len(rows)
    total_pages = math.ceil(total / HISTORY_PAGE_SIZE) if total > 0 else 0
    # Nunca erro por página fora de alcance (ex.: usuário estava na
    # página 2, um registro foi reclassificado/excluído e só sobrou 1
    # página) — clampa pro intervalo válido e devolve o `page` REAL usado.
    resolved_page = min(max(page, 1), total_pages) if total_pages > 0 else 1
    start = (resolved_page - 1) * HISTORY_PAGE_SIZE
    page_items = rows[start : start + HISTORY_PAGE_SIZE]
    # Sobre o conjunto COMPLETO, não só a página atual — uma linha
    # "programada" pode estar em outra página, mas o rótulo do card
    # ("histórico" vs. "histórico e programadas") precisa refletir o
    # total, não só o que está visível agora.
    has_programmed = any(row.status == TaxRateHistoryStatus.PROGRAMADA for row in rows)

    return TaxRateHistoryPage(
        items=page_items,
        page=resolved_page,
        page_size=HISTORY_PAGE_SIZE,
        total=total,
        total_pages=total_pages,
        has_programmed=has_programmed,
    )


def get_effective_rate(
    session: Session, organization_id: uuid.UUID, competence_month: date
) -> OrganizationTaxRate | None:
    return tax_rate_repo.get_effective(session, organization_id, competence_month.replace(day=1))


def set_rate(session: Session, actor: ActorContext, data: TaxRateSet) -> OrganizationTaxRate:
    """Cria ou edita (upsert por competência) uma linha. Editar uma
    competência já EXISTENTE só é permitido quando ela é o mês atual ou
    futuro, OU quando `confirm_past=True` é enviado explicitamente —
    nunca uma edição silenciosa de um período que o Dashboard já pode
    ter mostrado pra alguém (item "não permitir que o usuário altere
    acidentalmente meses anteriores pensando que está alterando apenas
    o atual")."""
    competence_month = data.competence_month  # já normalizado (validator do schema)
    is_past = competence_month < _current_competence_month()
    if is_past and not data.confirm_past:
        raise ConflictError(
            "Esta competência já é um período passado. Confirme explicitamente para "
            "editá-la — o Dashboard daquele período será recalculado.",
        )

    existing = tax_rate_repo.get_by_competence(session, actor.organization_id, competence_month)
    if existing is None:
        user_name = _resolve_user_name(session, actor.user_id)
        rate = tax_rate_repo.create(
            session,
            actor.organization_id,
            competence_month=competence_month,
            tax_rate=data.tax_rate,
            created_by=actor.user_id,
            created_by_name=user_name,
        )
        audit_log_repo.create(
            session,
            organization_id=actor.organization_id,
            user_id=actor.user_id,
            entity_type="organization_tax_rate",
            entity_id=rate.id,
            action=AuditAction.CREATE,
            old_values=None,
            new_values={"competence_month": competence_month.isoformat(), "tax_rate": str(data.tax_rate)},
        )
        return rate

    old_rate = existing.tax_rate
    existing.tax_rate = data.tax_rate
    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="organization_tax_rate",
        entity_id=existing.id,
        action=AuditAction.UPDATE,
        old_values={"competence_month": competence_month.isoformat(), "tax_rate": str(old_rate)},
        new_values={"competence_month": competence_month.isoformat(), "tax_rate": str(data.tax_rate)},
    )
    return tax_rate_repo.save(session, existing)


@dataclass
class CompetenceTaxResolution:
    competence_month: date
    tax_rate: Decimal | None
    source_competence_month: date | None


def resolve_rates_for_months(
    session: Session, organization_id: uuid.UUID, months: list[date]
) -> dict[date, CompetenceTaxResolution]:
    """Resolve a alíquota vigente pra cada mês de `months` (lista de
    primeiros-dias-do-mês distintos). Usada pelo Dashboard pra aplicar
    a alíquota CORRETA de cada competência dentro de um período que
    pode atravessar várias mudanças de alíquota — nunca a alíquota
    atual aplicada sobre todo o intervalo."""
    resolutions: dict[date, CompetenceTaxResolution] = {}
    for month in months:
        effective = tax_rate_repo.get_effective(session, organization_id, month)
        resolutions[month] = CompetenceTaxResolution(
            competence_month=month,
            tax_rate=effective.tax_rate if effective is not None else None,
            source_competence_month=effective.competence_month if effective is not None else None,
        )
    return resolutions


def _add_month(month: date) -> date:
    if month.month == 12:
        return month.replace(year=month.year + 1, month=1)
    return month.replace(month=month.month + 1)


def months_between(date_from: datetime, date_to: datetime) -> list[date]:
    """Lista de primeiros-dias-do-mês tocados por `[date_from, date_to)`
    — usada pra saber quais competências precisam ter a alíquota
    resolvida ao provisionar imposto de um período que pode atravessar
    virada(s) de mês."""
    start = date_from.date().replace(day=1)
    # `date_to` é EXCLUSIVE (convenção do Dashboard) — um instante
    # exatamente à meia-noite do 1º dia não deve "contar" aquele mês.
    end_inclusive = (date_to - timedelta(microseconds=1)) if date_to > date_from else date_from
    end = end_inclusive.date().replace(day=1)
    months: list[date] = []
    current = start
    while current <= end:
        months.append(current)
        current = _add_month(current)
    return months
