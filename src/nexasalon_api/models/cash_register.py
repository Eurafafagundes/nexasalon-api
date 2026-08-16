"""Caixa Diário (`CashRegister`) e suas movimentações manuais
(`CashMovement`) — item "Implementar Caixa Diário / Financeiro".

Fluxo: Comanda -> Pagamento -> Caixa -> Fechamento diário. Cada
`Payment` (ver `models/order.py`) fica vinculado a UM `CashRegister`
aberto (`Payment.cash_register_id`, obrigatório) — é ele quem já
funciona como "movimentação de entrada" no resumo do caixa; esta
tabela aqui (`CashMovement`) guarda só o que NÃO tem outra origem:
sangria, suprimento e (reservado pro futuro) estorno.

Auditoria (item "preparar para auditoria"): todo campo de
responsável/usuário guarda TANTO a FK (`opened_by`, `closed_by`,
`created_by`) QUANTO um snapshot do nome (`opened_by_name`,
`closed_by_name`, `created_by_name`) capturado no momento da operação
— preserva o histórico mesmo se o usuário renomear a própria conta
depois. Nunca usar só uma string livre como "responsável".
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import CashMovementType, CashRegisterStatus, PaymentMethod, pg_enum


class CashRegister(Base, UUIDPKMixin, TimestampMixin):
    """Um caixa aberto por um usuário (`opened_by`) — nunca um texto
    livre de "responsável".

    Mudança de regra (rodada "evolução funcional" — Clientes/
    Financeiro/Caixa): a 0014 original permitia só UM caixa aberto por
    USUÁRIO (mas vários por organização, um por responsável). O pedido
    desta rodada é explícito — "uma unidade pode ter apenas um caixa
    aberto por vez" — o que é uma regra DIFERENTE (por `branch_id`, não
    por usuário). Adotada aqui: `branch_id` é NULLABLE no banco (não dá
    pra virar NOT NULL numa tabela que já tem linha em staging sem
    backfill, e não existe "unidade certa" pra inferir de um caixa
    histórico antigo — ver migration 0015), mas a API passa a EXIGIR
    `branch_id` em toda abertura nova, e `open_register` passa a checar
    "já existe caixa aberto NESTA unidade" em vez de "...deste
    usuário" (ver `services/cash_register.py`)."""

    __tablename__ = "cash_registers"
    __table_args__ = (
        CheckConstraint(
            "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)",
            name="closed_at_matches_status",
        ),
        CheckConstraint("initial_amount >= 0", name="initial_amount_not_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT")
    )
    opened_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    opened_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    initial_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Observação da ABERTURA — distinta de `closing_notes` (fechamento).
    opening_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CashRegisterStatus] = mapped_column(
        pg_enum(CashRegisterStatus, "cash_register_status"),
        nullable=False,
        server_default=CashRegisterStatus.OPEN.value,
        index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_by_name: Mapped[str | None] = mapped_column(String(255))
    closing_notes: Mapped[str | None] = mapped_column(Text)
    # Snapshot calculado no momento do FECHAMENTO (ver
    # `services/cash_register.py::build_summary`) — saldo físico
    # esperado (`initial_amount` + dinheiro recebido + suprimentos -
    # sangrias). Guardado (não só recalculado on-the-fly depois) porque
    # é exatamente o número usado pra calcular `difference`, e um caixa
    # fechado nunca recebe novas movimentações — não há risco de ficar
    # desatualizado.
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # Valor físico contado manualmente no fechamento (opcional — item
    # "se possível, permitir informar valor físico contado").
    counted_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # `counted_amount - expected_amount`, guardado pra não depender de
    # recalcular no frontend/relatório depois.
    difference: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    movements: Mapped[list["CashMovement"]] = relationship(
        back_populates="cash_register", cascade="all, delete-orphan", order_by="CashMovement.created_at"
    )


class CashMovement(Base, UUIDPKMixin, TimestampMixin):
    """Lançamento manual dentro de um caixa aberto — Entrada (`SUPPLY`)
    ou Despesa (`WITHDRAWAL`) na linguagem da tela ("+ Entrada"/
    "+ Despesa", item 21/22), ou (reservado) estorno. NUNCA editado/
    apagado depois de criado (sem endpoint de update/delete) —
    correção futura, se necessária, é um novo lançamento de estorno com
    trilha própria, não uma reescrita silenciosa deste registro.

    `method`/`category` (rodada "evolução funcional"): uma Entrada/
    Despesa pode acontecer em qualquer forma de pagamento, não só
    dinheiro (ex.: despesa paga via Pix) — item "faturamento não é a
    mesma coisa que dinheiro", generalizado pra movimentação manual
    também. Só `method=CASH` afeta o saldo físico esperado (ver
    `services/cash_register.py::build_summary`); as demais formas ainda
    entram na soma de entradas/saídas "não-venda" pro Extrato, mas não
    mexem no dinheiro físico do caixa. `category` é texto livre
    simples (item "categorias simples/configuráveis, sem sistema
    contábil complexo") — sem tabela de categorias nesta rodada."""

    __tablename__ = "cash_movements"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    cash_register_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_registers.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[CashMovementType] = mapped_column(pg_enum(CashMovementType, "cash_movement_type"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(120))
    method: Mapped["PaymentMethod"] = mapped_column(  # noqa: F821 — importado abaixo
        pg_enum(PaymentMethod, "payment_method"),
        nullable=False,
        server_default=PaymentMethod.CASH.value,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False)

    cash_register: Mapped["CashRegister"] = relationship(back_populates="movements")
