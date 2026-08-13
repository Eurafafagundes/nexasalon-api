import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from nexasalon_api.models.enums import OrganizationStatus


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    document: str | None
    email: str | None
    phone: str | None
    timezone: str
    status: OrganizationStatus
    business_type: str | None
    created_at: datetime
    updated_at: datetime
