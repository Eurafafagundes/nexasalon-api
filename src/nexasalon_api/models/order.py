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
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import (
    BenefitType,
    CardBrand,
    CommissionStatus,
    CommissionType,
    OrderProductItemKind,
    OrderStatus,
    PaymentFeeStatus,
    PaymentMethod,
    pg_enum,
)


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
    # Observação operacional da comanda (item "Comanda — Observação +
    # Auditoria") — NUNCA reaproveita `updated_at` (mexe com pagamento/
    # produto/status, não serve pra "quando a observação foi editada por
    # último"). Auditoria DEDICADA, mesmo padrão de snapshot de
    # `OrderProductItem.product_name`: `observation_updated_by_name` é
    # capturado no momento da edição, não um join a `users` (usuário
    # removido depois continua aparecendo com o nome que tinha). Editável
    # com a comanda OPEN ou CLOSED (única exceção à regra "só edita
    # aberta" deste módulo — ver `services/orders.py::update_observation`),
    # nunca reabre nem afeta o estado financeiro.
    observation: Mapped[str | None] = mapped_column(Text)
    observation_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    observation_updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    observation_updated_by_name: Mapped[str | None] = mapped_column(String(160))
    # Concorrência otimista da observação — contador explícito (nasce em
    # 0, "nunca editada" é um valor real e comparável, nunca NULL). Um
    # timestamp NULL inicial não distinguia "nunca editada" de "sem
    # controle de versão", deixando passar duas primeiras edições
    # concorrentes sem 409 (auditoria "última correção pré-push", item
    # 2) — ver `services/orders.py::update_observation`. Incrementado em
    # 1 a cada edição bem-sucedida; nunca usado pra nada além disso
    # (não é histórico, não é ordenação de exibição).
    observation_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

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
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_not_negative"),
        # Etapa C4 — reforça no banco (não só na service layer) que um
        # item só pode ser linkado a um settlement se a comissão foi de
        # fato CALCULADA — `not_configured` e histórico
        # (`commission_status IS NULL`) nunca entram numa liquidação.
        CheckConstraint(
            "commission_settlement_id IS NULL OR commission_status = 'calculated'",
            name="commission_settlement_id_requires_calculated",
        ),
        # Etapa "Benefício por Item" (migration 0052) — os dois campos
        # vêm juntos (NULL+NULL = sem benefício, ou ambos preenchidos)
        # e o valor do benefício nunca pode exceder o valor econômico
        # do item (`price`) — nunca "cobrar" um benefício maior que o
        # próprio serviço.
        CheckConstraint(
            "(benefit_type IS NULL AND benefit_amount IS NULL) OR "
            "(benefit_type IS NOT NULL AND benefit_amount IS NOT NULL "
            "AND benefit_amount >= 0 AND benefit_amount <= price)",
            name="benefit_requires_type_and_amount_within_price",
        ),
    )

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

    # --- Etapa "Benefício por Item" (migration 0052) ---
    # `price` acima NUNCA é alterado pra representar um benefício —
    # continua sendo só o valor econômico/comercial do item (base de
    # Faturamento e de comissão, sempre — ver `services/orders.py::
    # close_order`, que resolve comissão com `price=item.price`
    # intocado). O valor efetivamente cobrado do cliente é DERIVADO,
    # nunca persistido: `price - (benefit_amount or 0)` (ver
    # `services/order_totals.py::item_charged_amount`). NULLABLE sem
    # backfill, mesmo raciocínio dos snapshots de comissão/taxa acima —
    # `OrderItem`s existentes ficam com os dois campos NULL pra sempre
    # (== "sem benefício", comportamento idêntico ao atual).
    benefit_type: Mapped[BenefitType | None] = mapped_column(pg_enum(BenefitType, "benefit_type"))
    benefit_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # --- Etapa C2 — Comissão por serviço vendido (migration 0036) ---
    # Todas NULLABLE de propósito, sem backfill (mesmo raciocínio do
    # snapshot de taxa de pagamento, Etapa N3) — resolvidas UMA VEZ no
    # fechamento da comanda (`services/orders.py::close_order`/
    # `close_orders_consolidated` -> `services/commissions.py::
    # resolve_commission`) e nunca recalculadas depois: editar a
    # comissão em `ProfessionalService` NUNCA altera um `OrderItem` já
    # fechado. `commission_status` é o diferenciador ESTRUTURADO entre
    # "comissão calculada" e "sem regra configurada" — nunca depender
    # só da nulidade dos snapshots (ver `models/enums.py::CommissionStatus`).
    commission_type_snapshot: Mapped[CommissionType | None] = mapped_column(
        pg_enum(CommissionType, "commission_type")
    )
    commission_value_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    commission_amount_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    commission_status: Mapped[CommissionStatus | None] = mapped_column(
        pg_enum(CommissionStatus, "commission_status")
    )
    # --- Etapa C4 — Fechamento/Pagamento de Comissão (migration 0038) ---
    # `NULL` = "A pagar" (comissão calculada, ainda não liquidada);
    # preenchido = "Pago", travado no momento em que o settlement é
    # criado (`services/commissions.py::create_settlement`) — nunca
    # editado depois disso (ver `models/commission.py::CommissionSettlement`).
    # RESTRICT: um settlement nunca pode ser apagado por baixo enquanto
    # itens ainda apontam pra ele (não que exista rota de delete hoje).
    commission_settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_settlements.id", ondelete="RESTRICT")
    )

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderProductItem(Base, UUIDPKMixin, TimestampMixin):
    """Uma linha de PRODUTO dentro da comanda — ver docstring do módulo
    pro raciocínio de por que isto é uma tabela separada de `OrderItem`.
    `quantity`/`unit_price` são editáveis (auditado, ver
    `services/orders.py::update_product_item`) enquanto a comanda
    estiver `OPEN`; congelados dali em diante.

    `item_type` (migration 0039, item "Comanda → Consumo de estoque")
    distingue produto VENDIDO à cliente (`SALE`, default — comportamento
    original desta tabela, inalterado) de produto CONSUMIDO
    internamente durante o serviço (`CONSUMPTION` — ex.: cabelo usado
    numa progressiva). `unit_price` continua NOT NULL nos dois casos:
    consumo sem cobrança separada usa `0`, consumo cobrado usa um valor
    explícito — o total da comanda (`services/order_totals.py`) soma
    `quantity * unit_price` de TODAS as linhas sem nenhum branch por
    tipo. O que muda por tipo é só o `StockMovementReason` usado no
    fechamento (`SALE` vs `INTERNAL_USE` — ver
    `services/orders.py::close_order`); a baixa em si, a idempotência
    (`stock_movement_id`) e o congelamento pós-fechamento são
    IDÊNTICOS para os dois tipos."""

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
    item_type: Mapped[OrderProductItemKind] = mapped_column(
        pg_enum(OrderProductItemKind, "order_product_item_kind"),
        nullable=False,
        server_default=OrderProductItemKind.SALE.value,
    )
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
    # `amount > 0` sempre — Payment representa dinheiro que de fato
    # mudou de mão; um valor 0 não corresponde a nenhum evento real
    # (nem Pix, nem Fidelidade — "Cartão Fidelidade" é só mais um
    # PaymentMethod, sem semântica de resgate/desconto própria, ver
    # docstring de `PaymentMethod.LOYALTY_CARD`). Uma comanda com total
    # 0 (cortesia/gratuita) fecha com `payments=[]` — nunca um Payment
    # artificial de R$0 só pra "existir" (ver `OrderClose.payments`).
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

    # --- Etapa N3 — Taxas de Pagamento (migration 0034) ---
    # Todas NULLABLE de propósito, sem backfill (item explícito
    # "não fazer NOT NULL sobre dados existentes" / "não inventar taxas
    # históricas") — pagamentos criados ANTES desta coluna existir têm
    # os 5 campos abaixo sempre `NULL`, e continuam válidos assim pra
    # sempre (ver `PaymentFeeStatus` pro raciocínio completo do porquê
    # `fee_status=NULL` não pode ser confundido com "taxa zero").
    #
    # `payment_fee_rule_id` é só REFERÊNCIA (auditoria/rastreio de qual
    # regra foi usada) — a fonte de verdade do valor realmente cobrado é
    # sempre o SNAPSHOT (`fee_percent_snapshot`/`fee_amount_snapshot`/
    # `net_amount_snapshot`), nunca a regra ao vivo: editar/desativar a
    # regra depois NUNCA recalcula um pagamento já criado.
    payment_fee_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_fee_rules.id", ondelete="SET NULL")
    )
    fee_percent_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    fee_amount_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    net_amount_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    fee_status: Mapped[PaymentFeeStatus | None] = mapped_column(pg_enum(PaymentFeeStatus, "payment_fee_status"))

    # --- Reabertura de comanda (migration 0044) ---
    # NUNCA apagamos/editamos o pagamento em si (mesma filosofia de
    # ledger append-only já usada em `StockMovement`/`CashMovement`) —
    # `reversed_at` preenchido é o marcador de "este lançamento não
    # conta mais" (faturamento/extrato/comissão já páram de contar
    # sozinhos por filtrarem `Order.status == CLOSED`; o Caixa, que lê
    # `Payment` diretamente por `cash_register_id` sem olhar pro status
    # da Order, é quem PRECISA deste campo pra não continuar somando um
    # pagamento revertido no resumo ao vivo — ver
    # `services/cash_register.py::build_summary`). `NULL` (nunca
    # backfilled) = pagamento ativo, comportamento de sempre; mesmo
    # padrão "nullable, sem valor inventado pro histórico" já usado por
    # `fee_status`. `reversed_by_name` é snapshot (mesmo motivo de
    # `created_by_name`) — quem reverteu continua identificável mesmo
    # se o usuário for removido depois.
    reversed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reversed_by_name: Mapped[str | None] = mapped_column(String(255))

    order: Mapped["Order"] = relationship(back_populates="payments")


class PaymentFeeRule(Base, UUIDPKMixin, TimestampMixin):
    """Etapa N3 — Taxas de Pagamento (Configurações > Taxas de
    Pagamento): cada organização cadastra a taxa percentual cobrada
    pela adquirente/máquina por (forma, bandeira, parcelas). Débito,
    crédito e Pix fazem sentido aqui — Dinheiro nunca tem incidência de
    taxa nesta etapa (ver `services/payment_fees.py`).

    Pix (migration 0050/0051) usa `card_brand=CardBrand.NOT_APPLICABLE`
    (nunca `NULL`) como sentinela — não tem bandeira de verdade, mas
    manter a coluna `NOT NULL` preserva a unicidade lógica
    `(organization, method, card_brand, installments)` simples SEM
    reabrir a mesma brecha de `NULL <> NULL` em `UNIQUE` já documentada
    abaixo pra `installments` (evita duas regras de Pix "duplicadas").
    O sentinela é só um detalhe interno: a API nunca aceita nem expõe
    `not_applicable` como bandeira (`schemas/payment_fee_rule.py`
    converte de/para `None` na borda).

    `installments` é sempre um inteiro explícito (nunca `NULL`) — pra
    débito e Pix é normalizado pra `1` (mesmo raciocínio de "parcelas deve ser
    tratado coerentemente como 1"), o que também mantém a unicidade
    lógica `(organization, method, card_brand, installments)` simples
    (Postgres trata `NULL <> NULL` em `UNIQUE`, o que abriria brecha
    pra duas regras de débito "duplicadas" se o campo pudesse ser nulo).

    Editar `fee_percent`/`is_active` aqui NUNCA recalcula pagamentos já
    fechados — o valor realmente cobrado fica congelado como snapshot em
    `Payment` no momento da venda (ver `services/payment_fees.py::resolve_fee`)."""

    __tablename__ = "payment_fee_rules"
    __table_args__ = (
        UniqueConstraint("organization_id", "method", "card_brand", "installments"),
        CheckConstraint("method IN ('debit', 'credit', 'pix')", name="method_is_card_or_pix"),
        CheckConstraint(
            "(method IN ('debit', 'credit') AND card_brand <> 'not_applicable') OR "
            "(method = 'pix' AND card_brand = 'not_applicable')",
            name="card_brand_matches_method",
        ),
        CheckConstraint("installments >= 1", name="installments_positive"),
        CheckConstraint("fee_percent >= 0", name="fee_percent_not_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"), nullable=False)
    card_brand: Mapped[CardBrand] = mapped_column(pg_enum(CardBrand, "card_brand"), nullable=False)
    installments: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
