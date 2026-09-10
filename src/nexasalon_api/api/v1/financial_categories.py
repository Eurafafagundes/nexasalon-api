"""Rotas de categorias financeiras (`/api/v1/financial-categories`) —
painel "Resultado disponível" (Dashboard).

Leitura usa `finance.view`, pois as categorias são necessárias nos fluxos
financeiros. Criação, edição, ativação e exclusão continuam protegidas por
`organization.manage`, como configuração organizacional. Nenhuma permission
nova é criada."""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.financial_category import (
    FinancialCategoryCreate,
    FinancialCategoryRead,
    FinancialCategoryUpdate,
)
from nexasalon_api.services import financial_categories as financial_categories_service

router = APIRouter(prefix="/financial-categories", tags=["financial-categories"])

_view = require_permission("finance.view")
_manage = require_permission("organization.manage")


@router.get("", response_model=list[FinancialCategoryRead], summary="Listar categorias financeiras")
def list_financial_categories(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[FinancialCategoryRead]:
    categories = financial_categories_service.list_categories(session, actor.organization_id, include_inactive)
    return [FinancialCategoryRead.model_validate(c) for c in categories]


@router.post(
    "", response_model=FinancialCategoryRead, status_code=status.HTTP_201_CREATED, summary="Criar categoria financeira"
)
def create_financial_category(
    payload: FinancialCategoryCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> FinancialCategoryRead:
    category = financial_categories_service.create_category(session, actor.organization_id, payload)
    return FinancialCategoryRead.model_validate(category)


@router.get("/{category_id}", response_model=FinancialCategoryRead, summary="Detalhar categoria financeira")
def get_financial_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> FinancialCategoryRead:
    category = financial_categories_service.get_category(session, actor.organization_id, category_id)
    return FinancialCategoryRead.model_validate(category)


@router.put("/{category_id}", response_model=FinancialCategoryRead, summary="Editar categoria financeira")
def update_financial_category(
    category_id: uuid.UUID,
    payload: FinancialCategoryUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> FinancialCategoryRead:
    category = financial_categories_service.update_category(session, actor.organization_id, category_id, payload)
    return FinancialCategoryRead.model_validate(category)


@router.patch("/{category_id}/activate", response_model=FinancialCategoryRead, summary="Ativar categoria financeira")
def activate_financial_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> FinancialCategoryRead:
    category = financial_categories_service.set_category_active(session, actor.organization_id, category_id, True)
    return FinancialCategoryRead.model_validate(category)


@router.patch(
    "/{category_id}/deactivate", response_model=FinancialCategoryRead, summary="Desativar categoria financeira"
)
def deactivate_financial_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> FinancialCategoryRead:
    category = financial_categories_service.set_category_active(session, actor.organization_id, category_id, False)
    return FinancialCategoryRead.model_validate(category)


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Excluir categoria financeira (só permitido sem nenhum lançamento vinculado)",
)
def delete_financial_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> None:
    financial_categories_service.delete_category(session, actor.organization_id, category_id)
