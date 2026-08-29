import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AppointmentCustomStatusBase(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color_hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    sort_order: int = Field(default=0, ge=0, le=32767)


class AppointmentCustomStatusCreate(AppointmentCustomStatusBase):
    pass


class AppointmentCustomStatusUpdate(AppointmentCustomStatusBase):
    pass


class AppointmentCustomStatusRead(AppointmentCustomStatusBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
