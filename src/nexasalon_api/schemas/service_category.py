import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServiceCategoryBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    display_order: int = Field(default=0, ge=0, le=32767)


class ServiceCategoryCreate(ServiceCategoryBase):
    pass


class ServiceCategoryUpdate(ServiceCategoryBase):
    pass


class ServiceCategoryRead(ServiceCategoryBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
