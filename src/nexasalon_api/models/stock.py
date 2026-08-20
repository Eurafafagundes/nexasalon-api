"""Movimentações de estoque (`StockMovement`), transferência entre
unidades (`StockTransfer`) e inventário (`InventoryCount`/
`InventoryCountItem`) — Etapa B.

`StockMovement` é o ledger de auditoria: TODA mudança de
`StockLevel.quantity_on_hand` nasce de uma linha aqui, nunca de um
UPDATE direto de quantidade (mesma filosofia append-only de
`CashMovement` — sem endpoint de update/delete; correção é uma nova
movimentação, nunca uma reescrita). `order_id` é um FK reservado,
nullable, pra Etapa C (venda debitando estoque a partir do fechamento
de Comanda) — não usado por nenhum fluxo desta etapa ainda."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import InventoryCountStatus, StockMovementDirection, StockMovementReason, pg_enum


class StockMovement(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "stock_movements"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_stock_movements_quantity_positive"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[StockMovementDirection] = mapped_column(
        pg_enum(StockMovementDirection, "stock_movement_direction"), nullable=False
    )
    reason: Mapped[StockMovementReason] = mapped_column(
        pg_enum(StockMovementReason, "stock_movement_reason"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    observation: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Reservado — preenchido pela Etapa C (débito de estoque no
    # fechamento de Comanda). Nenhum fluxo desta etapa grava aqui.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    # Preenchido só quando a movimentação nasce de uma StockTransfer —
    # permite achar rapidamente o par (-N/+N) que formam UMA
    # transferência (ver `services/stock.py::create_transfer`).
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_transfers.id", ondelete="SET NULL")
    )
    # Preenchido só quando a movimentação nasce do fechamento de um
    # InventoryCount (reason=INVENTORY_COUNT) — mesma lógica de
    # `transfer_id`, aponta pro inventário que a gerou.
    inventory_count_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_counts.id", ondelete="SET NULL")
    )


class StockTransfer(Base, UUIDPKMixin, TimestampMixin):
    """Transferência entre unidades — origem/destino/produto/quantidade.
    Não é, em si, uma movimentação: gera duas (`TRANSFER_OUT` na
    origem, `TRANSFER_IN` no destino — ver `services/stock.py`), e
    NUNCA cria lançamento financeiro (item explícito "sem lançamento
    financeiro fictício" — este módulo nem importa `CashMovement`)."""

    __tablename__ = "stock_transfers"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_stock_transfers_quantity_positive"),
        CheckConstraint(
            "origin_branch_id != destination_branch_id", name="ck_stock_transfers_distinct_branches"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    origin_branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    destination_branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    observation: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False)


class InventoryCount(Base, UUIDPKMixin, TimestampMixin):
    """Contagem de inventário de UMA unidade. Nasce `OPEN` com um item
    por produto ativo já visível naquela unidade (snapshot de
    `StockLevel.quantity_on_hand` em `InventoryCountItem.system_quantity`
    no momento da abertura — contagem do sistema NUNCA é recalculada
    depois, mesmo que outra movimentação aconteça enquanto a contagem
    está aberta; ver docstring de `services/inventory_counts.py` sobre
    como isso é tratado no fechamento). Fechar exige TODOS os itens
    contados (`counted_quantity` preenchido) — nunca aplica diferença
    de item não contado (item "nunca sobrescrever silenciosamente")."""

    __tablename__ = "inventory_counts"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[InventoryCountStatus] = mapped_column(
        pg_enum(InventoryCountStatus, "inventory_count_status"),
        nullable=False,
        server_default=InventoryCountStatus.OPEN.value,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_by_name: Mapped[str | None] = mapped_column(String(255))

    items: Mapped[list["InventoryCountItem"]] = relationship(
        back_populates="inventory_count", cascade="all, delete-orphan", order_by="InventoryCountItem.created_at"
    )


class InventoryCountItem(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "inventory_count_items"
    __table_args__ = (
        CheckConstraint("system_quantity >= 0", name="ck_inventory_count_items_system_not_negative"),
        CheckConstraint(
            "counted_quantity IS NULL OR counted_quantity >= 0",
            name="ck_inventory_count_items_counted_not_negative",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_count_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_counts.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    system_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    counted_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    inventory_count: Mapped["InventoryCount"] = relationship(back_populates="items")
