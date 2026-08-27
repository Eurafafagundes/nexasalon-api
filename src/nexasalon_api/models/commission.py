"""Etapa C4 — Fechamento/Pagamento de Comissão + Ajustes Auditáveis
(migration 0038). Duas tabelas novas, RLS direta por `organization_id`
(mesmo padrão de `PaymentFeeRule`, `models/order.py`).

`CommissionSettlement` é o registro IMUTÁVEL de "esta comissão foi
paga" — nasce pronto (o ato de criar JÁ É a confirmação de pagamento,
"Registrar pagamento"/"Fechar e marcar como pago", nunca um rascunho
separado). Nenhuma rota de edição/exclusão existe nem é prevista:
`production_total`/`commission_total` são somas CONGELADAS no momento
da criação (dos snapshots de `OrderItem` + ajustes incluídos, ver
`services/commissions.py::create_settlement`) — nunca recalculadas
relendo o período depois. Qualquer correção posterior é sempre um
`CommissionAdjustment` novo, nunca uma edição deste registro.

`OrderItem.commission_settlement_id` (ver `models/order.py`) é quem de
fato "prende" um item a um settlement — não existe FK inversa aqui. O
CHECK `ck_order_items_commission_settlement_id_requires_calculated`
garante, no próprio banco (não só na service layer), que um item só
pode ser linkado se `commission_status=calculated`: `not_configured` e
histórico (`commission_status IS NULL`) nunca entram numa liquidação.

`CommissionAdjustment` é uma correção manual auditável — nunca altera
o snapshot original de `OrderItem` nem o total de um settlement já
criado. Nasce solta (`commission_settlement_id IS NULL`, "pendente") e
pode ser incluída numa liquidação futura (`create_settlement`,
parâmetro `adjustment_ids`) — a coluna também aceita um ajuste já
nascer preso a um settlement específico (não usado por nenhum fluxo
desta etapa, mas mantém o modelo simples sem um terceiro estado).
`order_item_id` é opcional — um ajuste pode ser genérico (bônus/
correção sem vínculo a uma venda específica) ou apontar pra um
`OrderItem` pontual, sempre validado como sendo do MESMO profissional
e organização (`services/commissions.py::create_adjustment`)."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin


class CommissionSettlement(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "commission_settlements"
    __table_args__ = (CheckConstraint("production_total >= 0", name="production_total_not_negative"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    production_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Soma dos `commission_amount_snapshot` dos itens incluídos + o
    # `amount` de qualquer ajuste incluído — pode ser diferente da soma
    # "pura" dos itens quando um ajuste negativo participa da
    # liquidação (nunca reescrito depois, ver docstring do módulo).
    commission_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Snapshot do nome de quem registrou o pagamento (mesmo padrão de
    # `Payment.created_by_name`) — "Pago por [nome]" continua correto
    # mesmo se o usuário for removido depois.
    created_by_name: Mapped[str | None] = mapped_column(String(255))
    paid_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CommissionAdjustment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "commission_adjustments"
    __table_args__ = (CheckConstraint("amount <> 0", name="amount_not_zero"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="RESTRICT"), nullable=False
    )
    # RESTRICT — mesmo raciocínio de `OrderItem.service_id`: um
    # OrderItem referenciado por um ajuste nunca pode ser apagado por
    # baixo (não que exista rota de delete de OrderItem hoje).
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_items.id", ondelete="RESTRICT")
    )
    commission_settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_settlements.id", ondelete="RESTRICT")
    )
    # Positivo (bônus) ou negativo (correção/desconto) — nunca zero
    # (CHECK acima), validado de novo na service layer com mensagem
    # amigável antes de bater no banco.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_name: Mapped[str | None] = mapped_column(String(255))
