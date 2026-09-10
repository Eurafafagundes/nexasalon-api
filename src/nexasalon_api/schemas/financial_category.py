import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from nexasalon_api.models.enums import ExpenseNature


class FinancialCategoryBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    nature: ExpenseNature
    display_order: int = Field(default=0, ge=0, le=32767)


class FinancialCategoryCreate(FinancialCategoryBase):
    pass


class FinancialCategoryUpdate(FinancialCategoryBase):
    pass


class FinancialCategoryRead(FinancialCategoryBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
