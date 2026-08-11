import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ServiceBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=120)
    description: str | None = None
    default_duration_minutes: int = Field(gt=0)
    default_price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class ServiceCreate(ServiceBase):
    pass


class ServiceUpdate(ServiceBase):
    pass


class ServiceRead(ServiceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
