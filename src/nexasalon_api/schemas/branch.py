import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BranchBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=120)
    address_line: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=2)
    zip_code: str | None = Field(default=None, max_length=16)
    phone: str | None = Field(default=None, max_length=32)
    timezone: str | None = None


class BranchCreate(BranchBase):
    pass


class BranchUpdate(BranchBase):
    pass


class BranchRead(BranchBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
