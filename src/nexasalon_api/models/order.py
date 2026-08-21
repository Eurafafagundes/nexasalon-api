"""Comanda (`Order`) e Pagamento (`Payment`) — primeira versão funcional
do fluxo Atendimento -> Comanda -> Pagamento -> Caixa -> Pago.

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

Etapa C (Estoque ↔ Comanda): `OrderProductItem` é uma linha de PRODUTO
dentro da comanda — deliberadamente uma tabela separada de `OrderItem`
(item explícito "separar claramente item de serviço e item de
produto"), nunca um `service_id`/`professional_id` nulável enxertado em
`OrderItem`. `unit_price` é snapshot de `Product.sale_price` no momento
em que o produto é adicionado à comanda (mesma filosofia de
`OrderItem.price`: editar aqui nunca escreve de volta no catálogo).
`stock_movement_id` fica `NULL` enquanto a comanda está aberta —
produto removido antes do fechamento nunca gerou baixa (não há
movimentação pra desfazer, porque nenhuma foi criada ainda); só é
preenchido no FECHAMENTO da comanda (`services/orders.py::close_order`
-> `services/stock.py::record_sale_movement`), e funciona como o
marcador de idempotência que impede uma segunda baixa pro mesmo item
(ver docstring de `close_order`)."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import CardBrand, OrderStatus, PaymentMethod, pg_enum


class Order(Base, UUIDPKMixin, TimestampMixin):
    """Comanda — 1:1 com um `Appointment` ATIVO (uma reserva tem, no
    máximo, uma comanda ABERTA ou FECHADA por vez — ver índice único
    parcial abaixo). `client_id`/`branch_id` são denormalizados a
    partir do Appointment no momento da criação só pra permitir uma
    futura tela de histórico do cliente sem precisar sempre juntar com
    `appointments` — mesmo espírito de `AppointmentItem.price`
    (snapshot, não recalculado depois)."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL) "
            "OR (status = 'cancelled' AND closed_at IS NULL)",
            name="closed_at_matches_status",
        ),
        UniqueConstraint("organization_id", "order_number", name="uq_orders_organization_order_number"),
        # Parcial (não um UniqueConstraint simples de coluna): ignora
        # comandas CANCELLED (migration 0024) — cancelar uma comanda
        # criada por engano precisa liberar o Appointment pra uma
        # comanda nova, sem deixar a linha cancelada "segurando" a
        # unicidade pra sempre. `services/orders.py::get_by_appointment`
        # já filtra `!= cancelled` no mesmo espírito.
        Index(
            "uq_orders_appointment_id_active", "appointment_id", unique=True,
            postgresql_where="status <> 'cancelled'",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # Número sequencial POR ORGANIZAÇÃO ("#1048" nas telas de Comandas/
    # Extrato/histórico do cliente) — nunca o `id` (UUID) exposto pra
    # recepção. Calculado em `order_repo.create` (MAX+1 por org); risco
    # pequeno de corrida sob abertura de comanda simultânea, aceito
    # nesta rodada (volume baixo) e documentado como limitação — ver
    # `services/orders.py`.
    order_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=False
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
    product_items: Mapped[list["OrderProductItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderProductItem.created_at"
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
    # Snapshot de NOME (item "snapshot histórico") — capturado na
    # criação da comanda, igual a `price`. Sem isso, renomear/inativar
    # um serviço ou profissional mudaria como uma venda ANTIGA aparece
    # no histórico do cliente/Extrato, o que quebraria a auditoria.
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)
    professional_name: Mapped[str] = mapped_column(String(255), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderProductItem(Base, UUIDPKMixin, TimestampMixin):
    """Uma linha de PRODUTO dentro da comanda — ver docstring do módulo
    pro raciocínio de por que isto é uma tabela separada de `OrderItem`.
    `quantity`/`unit_price` são editáveis (auditado, ver
    `services/orders.py::update_product_item`) enquanto a comanda
    estiver `OPEN`; congelados dali em diante."""

    __tablename__ = "order_product_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_product_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_product_items_unit_price_not_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT — mesmo raciocínio de `OrderItem.service_id`: um Produto
    # referenciado por uma linha de comanda (aberta OU fechada) nunca
    # pode ser apagado por baixo (desativar continua permitido).
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Snapshot de NOME — mesmo padrão de `OrderItem.service_name`.
    product_name: Mapped[str] = mapped_column(String(160), nullable=False)
    # NULL = ainda não baixou estoque (comanda aberta, ou item removido
    # antes do fechamento). Preenchido UMA vez, no fechamento — nunca
    # sobrescrito depois (ver docstring do módulo).
    stock_movement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_movements.id", ondelete="SET NULL")
    )

    order: Mapped["Order"] = relationship(back_populates="product_items")


class Payment(Base, UUIDPKMixin, TimestampMixin):
    """Um lançamento de pagamento dentro da comanda — lista (`Payment[]`),
    não um único método/valor na própria `Order`: já preparado pra
    pagamento misto (ex.: R$200 Pix + R$180 Crédito) sem precisar de
    outra migration depois. Nesta primeira versão a UI só cria um
    lançamento por fechamento, mas o domínio já suporta vários.

    `cash_register_id` é OBRIGATÓRIO (item "Caixa Diário" — pagamento
    de comanda sempre vinculado a um caixa aberto, nunca criado sem
    ação explícita de selecionar/abrir um). `created_by_name` é
    snapshot do usuário que REGISTROU o pagamento — pode ser diferente
    do responsável pelo caixa (`CashRegister.opened_by_name`); os dois
    são preservados separadamente pra auditoria (ver
    `models/cash_register.py`)."""

    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    cash_register_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_registers.id", ondelete="RESTRICT"), nullable=False
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
    created_by_name: Mapped[str | None] = mapped_column(String(255))

    order: Mapped["Order"] = relationship(back_populates="payments")
