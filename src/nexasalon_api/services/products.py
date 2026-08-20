"""Camada de negócio do catálogo de Produtos — Etapa B (Estoque).

`Product` nasce sem nenhuma linha de `StockLevel` (nenhuma unidade tem
saldo "de graça"): as linhas de saldo por unidade são criadas sob
demanda pela primeira movimentação/contagem daquele produto naquela
unidade (`repositories/stock_level_repo.py::lock_or_create`)."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.enums import AuditAction
from nexasalon_api.models.product import Product
from nexasalon_api.repositories import audit_log_repo, product_repo
from nexasalon_api.schemas.product import ProductCreate, ProductUpdate


def list_products(
    session: Session,
    actor: ActorContext,
    *,
    include_inactive: bool = False,
    category: str | None = None,
    for_sale: bool | None = None,
    search: str | None = None,
) -> list[Product]:
    return product_repo.list_all(
        session,
        actor.organization_id,
        include_inactive=include_inactive,
        category=category,
        for_sale=for_sale,
        search=search,
    )


def get_product(session: Session, actor: ActorContext, product_id: uuid.UUID) -> Product:
    product = product_repo.get(session, actor.organization_id, product_id)
    if product is None:
        raise NotFoundError("Produto não encontrado.")
    return product


def create_product(session: Session, actor: ActorContext, data: ProductCreate) -> Product:
    if data.sku and product_repo.get_by_sku(session, actor.organization_id, data.sku) is not None:
        raise ConflictError("Já existe um produto com este SKU.")

    product = product_repo.create(session, actor.organization_id, **data.model_dump())
    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="product",
        entity_id=product.id,
        action=AuditAction.CREATE,
        new_values={"name": product.name, "sku": product.sku, "unit": product.unit.value},
    )
    return product


def update_product(session: Session, actor: ActorContext, product_id: uuid.UUID, data: ProductUpdate) -> Product:
    product = get_product(session, actor, product_id)
    if data.sku and data.sku != product.sku:
        existing = product_repo.get_by_sku(session, actor.organization_id, data.sku)
        if existing is not None and existing.id != product.id:
            raise ConflictError("Já existe um produto com este SKU.")

    old_values = {
        "name": product.name,
        "category": product.category,
        "sku": product.sku,
        "unit": product.unit.value,
        "cost_price": str(product.cost_price),
        "sale_price": str(product.sale_price) if product.sale_price is not None else None,
        "supplier_name": product.supplier_name,
        "for_sale": product.for_sale,
    }
    for field, value in data.model_dump().items():
        setattr(product, field, value)
    product = product_repo.save(session, product)

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="product",
        entity_id=product.id,
        action=AuditAction.UPDATE,
        old_values=old_values,
        new_values={
            "name": product.name,
            "category": product.category,
            "sku": product.sku,
            "unit": product.unit.value,
            "cost_price": str(product.cost_price),
            "sale_price": str(product.sale_price) if product.sale_price is not None else None,
            "supplier_name": product.supplier_name,
            "for_sale": product.for_sale,
        },
    )
    return product


def set_product_active(session: Session, actor: ActorContext, product_id: uuid.UUID, is_active: bool) -> Product:
    """Desativar um produto NUNCA apaga histórico (movimentações,
    saldo) — só tira do catálogo ativo (listas de venda, criação de
    nova movimentação continua bloqueada pela rota, ver
    `api/v1/products.py`)."""
    product = get_product(session, actor, product_id)
    product.is_active = is_active
    product = product_repo.save(session, product)
    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="product",
        entity_id=product.id,
        action=AuditAction.UPDATE,
        new_values={"change_type": "set_active", "is_active": is_active},
    )
    return product
