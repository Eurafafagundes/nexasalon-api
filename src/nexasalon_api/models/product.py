"""Catálogo de Produtos (`Product`) e saldo por unidade (`StockLevel`) —
Etapa B (Estoque).

Decisão de design deliberada, citada explicitamente no pedido ("estoque
POR UNIDADE — nunca misturar global com quantidade por filial"):
`Product` é só o CATÁLOGO (nome/categoria/SKU/unidade de medida/custo/
preço de venda/fornecedor/ativo/pra-venda-ou-uso-interno) — não carrega
NENHUM campo de quantidade. Quantidade em mão e estoque mínimo vivem em
`StockLevel`, uma linha por (produto, unidade/branch), nunca um número
único "global" por produto. Isso é o mesmo raciocínio de
`Professional`/`WorkingHours` (catálogo vs. estado operacional) aplicado
a produto/estoque.

`category` é texto livre simples nesta etapa (não uma tabela dedicada
tipo `ServiceCategory`) — mantém o escopo da Etapa B controlável; pode
evoluir para uma tabela própria depois, se necessário, sem quebrar o
texto já gravado (adicionar `category_id` nullable ao lado, no estilo
"legado + novo" já usado em outras migrations desta base)."""
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import ProductUnit, pg_enum


class Product(Base, UUIDPKMixin, TimestampMixin):
    """Item do catálogo de estoque. `for_sale` distingue produto vendável
    ao cliente (ex.: esmalte pra levar pra casa) de produto de uso
    interno exclusivo do salão (ex.: descartável, insumo de coloração)
    — item explícito do pedido ("produto-para-venda vs
    produto-de-uso-interno"). `cost_price` é o campo sensível por trás
    da regra "Ver estoque ≠ Ver custo dos produtos" (`inventory.view`
    nunca inclui custo; só `inventory.view_cost` inclui — ver
    `schemas/product.py`/`api/v1/products.py`)."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "sku", name="uq_products_organization_id_sku"),
        CheckConstraint("cost_price >= 0", name="ck_products_cost_price_not_negative"),
        CheckConstraint("sale_price IS NULL OR sale_price >= 0", name="ck_products_sale_price_not_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120))
    sku: Mapped[str | None] = mapped_column(String(60))
    unit: Mapped[ProductUnit] = mapped_column(
        pg_enum(ProductUnit, "product_unit"), nullable=False, server_default=ProductUnit.UNIT.value
    )
    cost_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="0")
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    supplier_name: Mapped[str | None] = mapped_column(String(160))
    for_sale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    stock_levels: Mapped[list["StockLevel"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="StockLevel.branch_id"
    )


class StockLevel(Base, UUIDPKMixin, TimestampMixin):
    """Saldo de UM produto em UMA unidade — nunca um total "global" por
    produto (ver docstring do módulo). Criada sob demanda (primeira
    movimentação ou primeira contagem de inventário daquele produto
    naquela unidade), sempre nascendo em zero — nenhuma quantidade
    aparece do nada sem passar por `StockMovement` (ver
    `services/stock.py::_lock_or_create_level`)."""

    __tablename__ = "stock_levels"
    __table_args__ = (
        UniqueConstraint("product_id", "branch_id", name="uq_stock_levels_product_id_branch_id"),
        CheckConstraint("quantity_on_hand >= 0", name="ck_stock_levels_quantity_not_negative"),
        CheckConstraint("minimum_quantity >= 0", name="ck_stock_levels_minimum_not_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    quantity_on_hand: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, server_default="0")
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, server_default="0")

    product: Mapped["Product"] = relationship(back_populates="stock_levels")
