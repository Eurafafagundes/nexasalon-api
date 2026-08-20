import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.product import Product


def get(session: Session, organization_id: uuid.UUID, product_id: uuid.UUID) -> Product | None:
    stmt = select(Product).where(Product.id == product_id, Product.organization_id == organization_id)
    return session.scalars(stmt).first()


def get_by_sku(session: Session, organization_id: uuid.UUID, sku: str) -> Product | None:
    stmt = select(Product).where(Product.organization_id == organization_id, Product.sku == sku)
    return session.scalars(stmt).first()


def list_all(
    session: Session,
    organization_id: uuid.UUID,
    *,
    include_inactive: bool = False,
    category: str | None = None,
    for_sale: bool | None = None,
    search: str | None = None,
) -> list[Product]:
    stmt = select(Product).where(Product.organization_id == organization_id)
    if not include_inactive:
        stmt = stmt.where(Product.is_active.is_(True))
    if category is not None:
        stmt = stmt.where(Product.category == category)
    if for_sale is not None:
        stmt = stmt.where(Product.for_sale.is_(for_sale))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(Product.name.ilike(term))
    stmt = stmt.order_by(Product.name)
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> Product:
    product = Product(organization_id=organization_id, **fields)
    session.add(product)
    session.flush()
    # `refresh` normaliza campos Numeric (`cost_price`/`sale_price`) pra
    # escala exata da coluna (ex.: "3.5" enviado -> "3.50" devolvido) —
    # sem isso, a resposta desta chamada mostraria a precisão "crua" do
    # Decimal que o Pydantic parseou, diferente de qualquer GET
    # posterior (que sempre lê de volta do Postgres já na escala da
    # coluna). Mesmo raciocínio nos demais `create`/mutação abaixo.
    session.refresh(product)
    return product


def save(session: Session, product: Product) -> Product:
    session.flush()
    session.refresh(product)
    return product
