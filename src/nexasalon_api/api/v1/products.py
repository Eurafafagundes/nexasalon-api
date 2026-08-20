"""Rotas de Produto (`/api/v1/products`) — catálogo + saldo por unidade.

Toda resposta de produto passa por `_serialize_product`, que escolhe
entre `ProductRead` (sem custo) e `ProductReadWithCost` (com custo) de
acordo com `inventory.view_cost` no `actor.permissions` — nunca decide
isso no frontend (item "Ver estoque ≠ Ver custo dos produtos", backend
é sempre a autoridade final)."""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.product import Product
from nexasalon_api.repositories import branch_repo, stock_level_repo
from nexasalon_api.schemas.product import ProductCreate, ProductRead, ProductReadWithCost, ProductUpdate
from nexasalon_api.schemas.stock import StockLevelMinimumUpdate, StockLevelRead
from nexasalon_api.services import products as products_service

router = APIRouter(prefix="/products", tags=["products"])

_view = require_permission("inventory.view")
_manage = require_permission("inventory.manage")


def _serialize_product(product: Product, actor: ActorContext) -> ProductRead | ProductReadWithCost:
    if "inventory.view_cost" in actor.permissions:
        return ProductReadWithCost.model_validate(product)
    return ProductRead.model_validate(product)


# IMPORTANTE: nenhuma destas rotas declara `response_model` — um
# `response_model=ProductRead | ProductReadWithCost` faria o FastAPI/
# Pydantic tentar casar a união e, por `ProductRead` ser um subconjunto
# de campos de `ProductReadWithCost`, poderia colapsar silenciosamente
# pro modelo "errado" (ex.: descartar `cost_price` mesmo pra quem tem
# `inventory.view_cost`, ou o oposto). Sem `response_model`, o FastAPI
# serializa exatamente a instância já validada que `_serialize_product`
# devolveu — nunca reprocessa contra um schema mais permissivo.
@router.get("", summary="Listar produtos")
def list_products(
    include_inactive: bool = False,
    category: str | None = Query(None),
    for_sale: bool | None = Query(None),
    search: str | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
):
    items = products_service.list_products(
        session, actor, include_inactive=include_inactive, category=category, for_sale=for_sale, search=search
    )
    return [_serialize_product(p, actor) for p in items]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Criar produto")
def create_product(
    payload: ProductCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    product = products_service.create_product(session, actor, payload)
    return _serialize_product(product, actor)


@router.get("/{product_id}", summary="Detalhar produto")
def get_product(
    product_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
):
    product = products_service.get_product(session, actor, product_id)
    return _serialize_product(product, actor)


@router.put("/{product_id}", summary="Editar produto")
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    product = products_service.update_product(session, actor, product_id, payload)
    return _serialize_product(product, actor)


@router.patch("/{product_id}/activate", summary="Ativar produto")
def activate_product(
    product_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    product = products_service.set_product_active(session, actor, product_id, True)
    return _serialize_product(product, actor)


@router.patch("/{product_id}/deactivate", summary="Desativar produto")
def deactivate_product(
    product_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    product = products_service.set_product_active(session, actor, product_id, False)
    return _serialize_product(product, actor)


@router.get(
    "/{product_id}/stock-levels", response_model=list[StockLevelRead], summary="Saldo do produto por unidade"
)
def list_stock_levels(
    product_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[StockLevelRead]:
    products_service.get_product(session, actor, product_id)  # 404 se não existir/for de outra organização
    levels = stock_level_repo.list_for_product(session, actor.organization_id, product_id)
    return [StockLevelRead.model_validate(lv) for lv in levels]


@router.put(
    "/{product_id}/stock-levels/{branch_id}/minimum",
    response_model=StockLevelRead,
    summary="Definir estoque mínimo do produto nesta unidade",
)
def set_minimum_quantity(
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    payload: StockLevelMinimumUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> StockLevelRead:
    products_service.get_product(session, actor, product_id)
    if branch_repo.get(session, actor.organization_id, branch_id) is None:
        raise NotFoundError("Unidade não encontrada.")
    level = stock_level_repo.lock_or_create(session, actor.organization_id, product_id, branch_id)
    level = stock_level_repo.set_minimum_quantity(session, level, payload.minimum_quantity)
    return StockLevelRead.model_validate(level)
