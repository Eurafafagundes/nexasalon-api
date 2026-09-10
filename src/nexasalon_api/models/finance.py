import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import ExpenseNature, FixedExpenseRecurrence, pg_enum


class OrganizationTaxRate(Base, UUIDPKMixin, TimestampMixin):
    """Alíquota de imposto provisionada, versionada por COMPETÊNCIA
    mensal — painel "Resultado disponível" (Dashboard). Regra
    inegociável: alterar a alíquota atual nunca recalcula uma
    competência já fechada no passado.

    Estratégia escolhida: "configuração efetiva por vigência" via
    linhas ESPARSAS (não um snapshot obrigatório todo mês) — só existe
    uma linha quando a alíquota MUDA. Resolver a alíquota vigente numa
    competência qualquer é sempre:

        SELECT tax_rate FROM organization_tax_rates
        WHERE organization_id = :org AND competence_month <= :m
        ORDER BY competence_month DESC LIMIT 1

    ("último valor conhecido até a competência", mesmo raciocínio de
    herança pedido no produto: Setembro sem linha própria usa a de
    Agosto). Isso é mais simples que gerar uma linha por mês
    automaticamente e não tem risco de "buraco" no histórico.

    `competence_month` é SEMPRE o primeiro dia do mês (nunca outro dia)
    — normalizado na camada de serviço antes de gravar, nunca confiado
    à UI. Editar uma linha JÁ EXISTENTE (mesma competência) é permitido
    (é literalmente "eu me enganei ao configurar este mês"), mas a API
    exige confirmação explícita quando a competência editada já está no
    passado (ver `services/tax_rates.py::set_rate`) — nunca uma edição
    silenciosa de período fechado."""

    __tablename__ = "organization_tax_rates"
    __table_args__ = (
        UniqueConstraint("organization_id", "competence_month"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_between_0_and_100"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    competence_month: Mapped[date] = mapped_column(Date, nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_name: Mapped[str | None] = mapped_column(String(255))


class FinancialCategory(Base, UUIDPKMixin, TimestampMixin):
    """Categoria de lançamento financeiro manual (`CashMovement`),
    criada livremente por organização — mesmo raciocínio de
    `ServiceCategory`/`AppointmentCustomStatus`: nenhuma categoria fixa
    no código, o salão define as suas.

    `nature` (FIXED/VARIABLE) é o que alimenta as linhas "(-) Custos
    variáveis"/"(-) Despesas fixas" do painel "Resultado disponível" —
    a classificação pertence à CATEGORIA, não a cada lançamento
    individual: reclassificar uma categoria já reclassifica toda
    movimentação futura vinculada a ela.

    `CashMovement.category` (texto livre, ver `models/cash_register.py`)
    é mantido por compatibilidade — um lançamento antigo, criado antes
    desta tabela existir, nunca é retroativamente associado a uma
    categoria estruturada (ledger append-only, nunca editado depois de
    criado); fica para sempre "não classificado" nas somas de Custos
    variáveis/Despesas fixas, exposto separadamente."""

    __tablename__ = "financial_categories"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    nature: Mapped[ExpenseNature] = mapped_column(pg_enum(ExpenseNature, "expense_nature"), nullable=False)
    display_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class FixedExpense(Base, UUIDPKMixin, TimestampMixin):
    """Identidade estável de um compromisso recorrente; seus dados mutáveis vivem em versões."""

    __tablename__ = "fixed_expenses"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_name: Mapped[str | None] = mapped_column(String(255))
    versions: Mapped[list["FixedExpenseVersion"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", order_by="FixedExpenseVersion.effective_from"
    )


class FixedExpenseVersion(Base, UUIDPKMixin, TimestampMixin):
    """Versão imutável por competência; `effective_to` é exclusivo."""

    __tablename__ = "fixed_expense_versions"
    __table_args__ = (
        UniqueConstraint("fixed_expense_id", "effective_from"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("due_day >= 1 AND due_day <= 31", name="due_day_between_1_and_31"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="effective_range_valid"),
        CheckConstraint("end_month IS NULL OR end_month >= start_month", name="end_not_before_start"),
        Index(
            "ix_fixed_expense_versions_period",
            "organization_id",
            "branch_id",
            "effective_from",
            "effective_to",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fixed_expense_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fixed_expenses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    financial_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    category_name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    recurrence: Mapped[FixedExpenseRecurrence] = mapped_column(
        pg_enum(FixedExpenseRecurrence, "fixed_expense_recurrence"), nullable=False
    )
    due_day: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_month: Mapped[date] = mapped_column(Date, nullable=False)
    end_month: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    expense: Mapped["FixedExpense"] = relationship(back_populates="versions")
