import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.enums import ScheduleBlockScope, ScheduleBlockType


class ScheduleBlockCreate(BaseModel):
    """Bloqueio/exceção pontual — NÃO é a jornada normal (isso é
    `WorkingHours`). Escopo mutuamente exclusivo por linha, igual ao
    CheckConstraint `scope_consistency` do model: profissional exige
    `professional_id`; unidade exige `branch_id` (sem `professional_id`);
    organização não usa nenhum dos dois."""

    scope: ScheduleBlockScope
    professional_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    block_type: ScheduleBlockType
    title: str | None = Field(default=None, max_length=255)
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _check_scope_consistency(self) -> "ScheduleBlockCreate":
        if self.scope == ScheduleBlockScope.PROFESSIONAL:
            if self.professional_id is None:
                raise ValueError("professional_id é obrigatório quando scope=professional")
            if self.branch_id is not None:
                raise ValueError("branch_id deve ser nulo quando scope=professional")
        elif self.scope == ScheduleBlockScope.BRANCH:
            if self.branch_id is None:
                raise ValueError("branch_id é obrigatório quando scope=branch")
            if self.professional_id is not None:
                raise ValueError("professional_id deve ser nulo quando scope=branch")
        elif self.scope == ScheduleBlockScope.ORGANIZATION:
            if self.professional_id is not None or self.branch_id is not None:
                raise ValueError("scope=organization não aceita professional_id nem branch_id")
        return self

    @model_validator(mode="after")
    def _check_order(self) -> "ScheduleBlockCreate":
        if self.end_at <= self.start_at:
            raise ValueError("end_at deve ser maior que start_at")
        return self


class ScheduleBlockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    scope: ScheduleBlockScope
    professional_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    block_type: ScheduleBlockType
    title: str | None
    start_at: datetime
    end_at: datetime
    recurrence_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
