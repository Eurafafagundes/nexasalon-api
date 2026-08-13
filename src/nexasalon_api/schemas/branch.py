import uuid
from datetime import datetime, time

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ALLOWED_SLOT_MINUTES = frozenset({15, 30})


class BranchBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=120)
    address_line: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=2)
    zip_code: str | None = Field(default=None, max_length=16)
    phone: str | None = Field(default=None, max_length=32)
    timezone: str | None = None

    agenda_view_start: time = Field(
        default=time(7, 0),
        description=(
            "Início da janela de horas exibida na Agenda principal desta unidade. "
            "É SÓ apresentação da grade — não é WorkingHours, não define disponibilidade real."
        ),
    )
    agenda_view_end: time = Field(
        default=time(21, 0),
        description="Fim da janela de horas exibida na Agenda principal desta unidade (mesma ressalva de agenda_view_start).",
    )
    agenda_slot_minutes: int = Field(
        default=30,
        description="Granularidade (em minutos) das linhas da grade da Agenda principal desta unidade. Aceita 15 ou 30.",
    )

    @model_validator(mode="after")
    def _validate_agenda_view(self) -> "BranchBase":
        if self.agenda_slot_minutes not in _ALLOWED_SLOT_MINUTES:
            raise ValueError("agenda_slot_minutes deve ser 15 ou 30.")
        if self.agenda_view_start >= self.agenda_view_end:
            raise ValueError("agenda_view_start deve ser anterior a agenda_view_end.")
        return self


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
