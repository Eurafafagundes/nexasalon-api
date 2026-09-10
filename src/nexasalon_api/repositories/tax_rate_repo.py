import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.finance import OrganizationTaxRate


def get_by_competence(
    session: Session, organization_id: uuid.UUID, competence_month: date
) -> OrganizationTaxRate | None:
    stmt = select(OrganizationTaxRate).where(
        OrganizationTaxRate.organization_id == organization_id,
        OrganizationTaxRate.competence_month == competence_month,
    )
    return session.scalars(stmt).first()


def get_effective(
    session: Session, organization_id: uuid.UUID, competence_month: date
) -> OrganizationTaxRate | None:
    """"Herança"/vigência: a alíquota vigente numa competência é a
    última linha conhecida com `competence_month <= :m` — nunca a mais
    recente cadastrada em absoluto (isso vazaria uma alíquota FUTURA
    pra uma competência passada). `None` quando a organização nunca
    configurou nenhuma alíquota até esta competência (nunca inventa
    0%)."""
    stmt = (
        select(OrganizationTaxRate)
        .where(
            OrganizationTaxRate.organization_id == organization_id,
            OrganizationTaxRate.competence_month <= competence_month,
        )
        .order_by(OrganizationTaxRate.competence_month.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def list_all(session: Session, organization_id: uuid.UUID) -> list[OrganizationTaxRate]:
    stmt = (
        select(OrganizationTaxRate)
        .where(OrganizationTaxRate.organization_id == organization_id)
        .order_by(OrganizationTaxRate.competence_month.desc())
    )
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> OrganizationTaxRate:
    rate = OrganizationTaxRate(organization_id=organization_id, **fields)
    session.add(rate)
    session.flush()
    return rate


def save(session: Session, rate: OrganizationTaxRate) -> OrganizationTaxRate:
    session.flush()
    return rate
