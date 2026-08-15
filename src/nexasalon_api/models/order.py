"""Comanda (`Order`) e Pagamento (`Payment`) — primeira versão funcional
do fluxo Atendimento -> Comanda -> Pagamento -> Pago.

Escopo deliberadamente pequeno (ver docstring do módulo de migration
`0013`): isto NÃO é o Financeiro completo (sem contas a pagar/receber,
DRE, fluxo de caixa, conciliação, TEF, comissão, fiscal). É só o
suficiente pra: abrir uma comanda a partir de um Appointment, editar o
preço de cada linha (sem tocar no catálogo nem no snapshot original do
AppointmentItem), e registrar o(s) pagamento(s) que fecham a comanda.

Três camadas de preço, cada uma imutável pela camada seguinte:
  1. `Service.default_price`      — catálogo, vivo, muda com reajustes.
  2. `AppointmentItem.price`      — snapshot no momento da reserva.
  3. `OrderItem.price`            — snapshot da COMANDA, começa igual ao
                                     item 2 mas é editável (lápis na UI)
                                     independentemente dos outros dois.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import CardBrand, OrderStatus, PaymentMethod, pg_enum


class Order(Base, UUIDPKMixin, TimestampMixin):
    """Comanda — 1:1 com um `Appointment` (uma reserva vira, no máximo,
    uma comanda). `client_id`/`branch_id` são denormalizados a partir do
    Appointment no momento da criação só pra permitir uma futura tela de
    histórico do cliente sem precisar sempre juntar com `appointments`
    — mesmo espírito de `AppointmentItem.price` (snapshot, não
    recalculado depois)."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)",
            name="closed_at_matches_status",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"), nullable=False, server_default=OrderStatus.OPEN.value, index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.created_at"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Payment.created_at"
    )


class OrderItem(Base, UUIDPKMixin, TimestampMixin):
    """Uma linha de serviço dentro da comanda — copiada de um
    `AppointmentItem` na criação (`service_id`/`professional_id`/
    `duration_minutes`/`price` começam idênticos ao item de origem).
    `price` é o ÚNICO campo editável depois disso (via
    `PATCH /orders/{id}/items/{item_id}`) — editar aqui nunca escreve de
    volta em `AppointmentItem.price` nem em `Service.default_price`."""

    __tablename__ = "order_items"
    __table_args__ = (CheckConstraint("price >= 0", name="price_not_negative"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT, não CASCADE: um AppointmentItem não deve poder ser
    # apagado silenciosamente por baixo de uma comanda que já o
    # referencia (agendamentos não expõem delete de item avulso hoje,
    # mas a trava fica correta mesmo se isso mudar depois).
    appointment_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointment_items.id", ondelete="RESTRICT")
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="RESTRICT"), nullable=False
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")


class Payment(Base, UUIDPKMixin, TimestampMixin):
    """Um lançamento de pagamento dentro da comanda — lista (`Payment[]`),
    não um único método/valor na própria `Order`: já preparado pra
    pagamento misto (ex.: R$200 Pix + R$180 Crédito) sem precisar de
    outra migration depois. Nesta primeira versão a UI só cria um
    lançamento por fechamento, mas o domínio já suporta vários."""

    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"), nullable=False)
    # Só preenchido quando method=debit/credit (validado no schema Pydantic).
    card_brand: Mapped[CardBrand | None] = mapped_column(pg_enum(CardBrand, "card_brand"))
    # Preparado pro futuro (parcelas de crédito) sem lógica de fato
    # implementada ainda — só guarda o número informado, nunca usado
    # pra calcular nada nesta versão.
    installments: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    order: Mapped["Order"] = relationship(back_populates="payments")
