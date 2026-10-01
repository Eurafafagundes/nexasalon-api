"""Schemas do Caixa Diário — ver `models/cash_register.py` e
`services/cash_register.py` para o raciocínio de domínio."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.enums import (
    CashMovementType,
    CashRegisterStatus,
    PaymentMethod,
)
from nexasalon_api.schemas.order import PaymentRead
from nexasalon_api.services.cash_register import movement_competence


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
    # Painel "Resultado disponível" (Dashboard): categoria ESTRUTURADA
    # (ver `models/finance.py::FinancialCategory`), opcional — escolhida
    # só no momento da criação, nunca preenchida depois (ledger
    # append-only). `category` (texto livre) continua aceito por
    # compatibilidade; os dois podem coexistir.
    financial_category_id: uuid.UUID | None = None
    fixed_expense_id: uuid.UUID | None = None
    method: PaymentMethod = PaymentMethod.CASH
    # "Data da despesa" — quando o lançamento REALMENTE ocorreu, distinta
    # de `created_at` (quando foi registrado no sistema). Opcional:
    # `None` (padrão, fluxo normal do dia) = competência efetiva cai em
    # `created_at`, comportamento idêntico a antes deste campo existir.
    # Só enviado quando o usuário escolhe explicitamente uma data
    # diferente de hoje — ver `services/cash_register.py::
    # register_movement` (valida não-futuro e resolve o fuso da
    # unidade). Mesmo padrão de `OrderClose.sale_date`.
    competence_date: date | None = None

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
    financial_category_id: uuid.UUID | None
    fixed_expense_id: uuid.UUID | None
    method: PaymentMethod
    created_by: uuid.UUID
    created_by_name: str
    created_at: datetime
    # `competence_override` é o valor CRU salvo (`None` na esmagadora
    # maioria dos lançamentos); `competence` é a "Data da despesa"
    # EFETIVA já resolvida (`services/cash_register.py::
    # movement_competence` — mesmo `COALESCE` usado por Extrato/
    # Dashboard/Resultado disponível), pra o frontend nunca precisar
    # reimplementar esse fallback sozinho. `created_at` continua
    # exposto, intocado, para auditoria/rastreabilidade técnica.
    competence_override: datetime | None
    competence: datetime

    @classmethod
    def from_model(cls, movement: CashMovement) -> "CashMovementRead":
        return cls(
            id=movement.id,
            cash_register_id=movement.cash_register_id,
            type=movement.type,
            amount=movement.amount,
            description=movement.description,
            category=movement.category,
            financial_category_id=movement.financial_category_id,
            fixed_expense_id=movement.fixed_expense_id,
            method=movement.method,
            created_by=movement.created_by,
            created_by_name=movement.created_by_name,
            created_at=movement.created_at,
            competence_override=movement.competence_override,
            competence=movement_competence(movement),
        )


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
