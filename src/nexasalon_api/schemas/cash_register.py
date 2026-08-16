"""Schemas do Caixa Diário — ver `models/cash_register.py` e
`services/cash_register.py` para o raciocínio de domínio."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.enums import CashMovementType, CashRegisterStatus, PaymentMethod
from nexasalon_api.schemas.order import PaymentRead


class CashRegisterOpen(BaseModel):
    # Obrigatório (item "uma unidade pode ter apenas um caixa aberto
    # por vez") — ver docstring de `models/cash_register.py` sobre a
    # mudança de regra "por usuário" -> "por unidade".
    branch_id: uuid.UUID
    initial_amount: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    notes: str | None = None


class CashRegisterClose(BaseModel):
    """`counted_amount` é opcional (item "se possível, permitir informar
    valor físico contado") — sem ele, `difference` fica `None` (não dá
    pra calcular diferença sem uma contagem física)."""

    counted_amount: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    notes: str | None = None


class CashMovementCreate(BaseModel):
    """Entrada (`supply`) ou Despesa (`withdrawal`) — nunca `reversal`
    por aqui (reservado pro catálogo, sem endpoint de criação direta
    nesta versão). `method` default `cash` preserva o comportamento
    anterior (sangria/suprimento sempre em dinheiro); informar outro
    método é o caso novo desta rodada (ex.: despesa paga em Pix) — ver
    `services/cash_register.py::build_summary` pra como isso afeta (ou
    não) o saldo físico."""

    type: CashMovementType
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    description: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=120)
    method: PaymentMethod = PaymentMethod.CASH

    @model_validator(mode="after")
    def _check_type(self) -> "CashMovementCreate":
        if self.type not in (CashMovementType.WITHDRAWAL, CashMovementType.SUPPLY):
            raise ValueError("type deve ser 'withdrawal' (despesa) ou 'supply' (entrada).")
        return self


class CashMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cash_register_id: uuid.UUID
    type: CashMovementType
    amount: Decimal
    description: str
    category: str | None
    method: PaymentMethod
    created_by: uuid.UUID
    created_by_name: str
    created_at: datetime

    @classmethod
    def from_model(cls, movement: CashMovement) -> "CashMovementRead":
        return cls.model_validate(movement)


class CashRegisterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID | None
    opened_by: uuid.UUID
    opened_by_name: str
    initial_amount: Decimal
    opening_notes: str | None
    status: CashRegisterStatus
    closed_at: datetime | None
    closed_by: uuid.UUID | None
    closed_by_name: str | None
    closing_notes: str | None
    expected_amount: Decimal | None
    counted_amount: Decimal | None
    difference: Decimal | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, register: CashRegister) -> "CashRegisterRead":
        return cls.model_validate(register)


class PaymentMethodTotal(BaseModel):
    method: PaymentMethod
    total: Decimal
    count: int


class CashRegisterDetail(BaseModel):
    """Resumo completo de um caixa (item "Resumo do Caixa") — dados do
    caixa + totais por forma de pagamento + faturamento + saldo físico
    esperado + as movimentações "cruas" (pagamentos e sangria/suprimento)
    pra quem consome montar o histórico cronológico completo (abertura
    -> pagamentos/movimentos -> fechamento)."""

    cash_register: CashRegisterRead
    totals_by_method: list[PaymentMethodTotal]
    total_revenue: Decimal
    cash_payments_total: Decimal
    supplies_total: Decimal
    withdrawals_total: Decimal
    expected_cash_balance: Decimal
    orders_count: int
    average_ticket: Decimal
    total_entries: Decimal
    movements: list[CashMovementRead]
    # Pagamentos deste caixa — quem consome monta o histórico
    # cronológico combinando isto com `movements` e
    # `register.created_at`/`closed_at` (abertura/fechamento), sem o
    # backend precisar manter uma terceira tabela de "timeline".
    payments: list[PaymentRead]
