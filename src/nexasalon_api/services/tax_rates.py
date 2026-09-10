"""Camada de negócio de `OrganizationTaxRate` — alíquota de imposto
provisionada, versionada por competência mensal (ver docstring do
model em `models/finance.py`). Usada exclusivamente pelo painel
"Resultado disponível" (Dashboard) para provisionar (nunca pagar/
lançar) imposto: `faturamento_aplicavel × alíquota_da_competência`.
"""
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
from nexasalon_api.schemas.tax_rate import TaxRateSet


def _current_competence_month() -> date:
    return datetime.now(timezone.utc).date().replace(day=1)


def _resolve_user_name(session: Session, user_id: uuid.UUID) -> str:
    user = user_repo.get(session, user_id)
    return user.name if user is not None else "Usuário removido"


def list_rates(session: Session, organization_id: uuid.UUID) -> list[OrganizationTaxRate]:
    return tax_rate_repo.list_all(session, organization_id)


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
