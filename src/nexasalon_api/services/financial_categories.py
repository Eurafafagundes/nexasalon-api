"""Camada de negócio de `FinancialCategory` — cada organização cria e
mantém suas próprias categorias de lançamento financeiro (nenhuma é
fixa no código; ver docstring do model)."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.finance import FinancialCategory
from nexasalon_api.repositories import financial_category_repo
from nexasalon_api.schemas.financial_category import (
    FinancialCategoryCreate,
    FinancialCategoryUpdate,
)


def list_categories(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[FinancialCategory]:
    return financial_category_repo.list_all(session, organization_id, include_inactive)


def get_category(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> FinancialCategory:
    category = financial_category_repo.get(session, organization_id, category_id)
    if category is None:
        raise NotFoundError("Categoria financeira não encontrada.")
    return category


def create_category(
    session: Session, organization_id: uuid.UUID, data: FinancialCategoryCreate
) -> FinancialCategory:
    return financial_category_repo.create(session, organization_id, **data.model_dump())


def update_category(
    session: Session, organization_id: uuid.UUID, category_id: uuid.UUID, data: FinancialCategoryUpdate
) -> FinancialCategory:
    """Não permite que uma edição cadastral reescreva o ledger histórico."""
    category = get_category(session, organization_id, category_id)
    if data.nature != category.nature:
        used = financial_category_repo.count_movements_using(session, organization_id, category_id)
        used += financial_category_repo.count_fixed_expense_versions_using(session, organization_id, category_id)
        if used:
            raise ConflictError(
                "A natureza de uma categoria já utilizada não pode ser alterada; crie outra categoria."
            )
    for field, value in data.model_dump().items():
        setattr(category, field, value)
    return financial_category_repo.save(session, category)


def set_category_active(
    session: Session, organization_id: uuid.UUID, category_id: uuid.UUID, is_active: bool
) -> FinancialCategory:
    """Desativar não apaga a categoria nem desvincula os `CashMovement`
    que a referenciam (FK é SET NULL só quando a linha é de fato
    deletada) — só some da lista de seleção pra NOVOS lançamentos."""
    category = get_category(session, organization_id, category_id)
    category.is_active = is_active
    return financial_category_repo.save(session, category)


def delete_category(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> None:
    """Exclusão de verdade (hard delete) — só quando a categoria NÃO
    tem nenhum lançamento apontando pra ela. Mesmo raciocínio de
    `services/service_categories.py::delete_category`: bloqueia e pede
    pra reclassificar/mover os lançamentos primeiro, em vez de
    desvincular silenciosamente."""
    category = get_category(session, organization_id, category_id)
    movements_count = financial_category_repo.count_movements_using(session, organization_id, category_id)
    versions_count = financial_category_repo.count_fixed_expense_versions_using(session, organization_id, category_id)
    if movements_count > 0 or versions_count > 0:
        total_uses = movements_count + versions_count
        raise ConflictError(
            f"Esta categoria possui {total_uses} "
            f"{'registro' if total_uses == 1 else 'registros'} vinculados. "
            "Desative a categoria em vez de excluí-la, ou reclassifique os lançamentos antes."
        )
    financial_category_repo.delete(session, category)
