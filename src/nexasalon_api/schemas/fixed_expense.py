import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexasalon_api.models.enums import FixedExpenseRecurrence


class FixedExpenseFields(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    financial_category_id: uuid.UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    recurrence: FixedExpenseRecurrence
    due_day: int = Field(ge=1, le=31)
    start_month: date
    end_month: date | None = None
    branch_id: uuid.UUID
    is_active: bool = True

    @field_validator("start_month", "end_month")
    @classmethod
    def normalize_month(cls, value: date | None) -> date | None:
        return value.replace(day=1) if value else None


class FixedExpenseCreate(FixedExpenseFields):
    pass


class FixedExpenseUpdate(FixedExpenseFields):
    effective_from: date

    @field_validator("effective_from")
    @classmethod
    def normalize_effective_month(cls, value: date) -> date:
        return value.replace(day=1)


class FixedExpenseStatusUpdate(BaseModel):
    is_active: bool
    effective_from: date

    @field_validator("effective_from")
    @classmethod
    def normalize_effective_month(cls, value: date) -> date:
        return value.replace(day=1)


class FixedExpenseRead(FixedExpenseFields):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime
    category: str


class FixedExpenseProvisionRow(BaseModel):
    fixed_expense_id: uuid.UUID
    name: str
    category: str
    financial_category_id: uuid.UUID
    branch_id: uuid.UUID
    competence_month: date
    due_date: date
    amount: Decimal


class FixedExpenseSummary(BaseModel):
    competence_month: date
    monthly_average: Decimal
    active_count: int
