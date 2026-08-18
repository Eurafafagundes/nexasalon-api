from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from nexasalon_api.models.enums import AppointmentStatus


class AppointmentStatusStyleUpdate(BaseModel):
    """Corpo do `PUT /appointment-status-styles/{status_code}` — REPLACE
    completo da personalização deste status (não PATCH parcial): o
    frontend sempre envia os dois campos com o valor EFETIVO atual
    (customizado ou de fábrica) que o usuário está editando no
    formulário, então `None` aqui significa "resetar este campo pro
    padrão de fábrica", nunca "não mudar" — evita ambiguidade sobre o
    que um campo omitido deveria fazer.

    `color_hex` usa o mesmo padrão de `ProfessionalBase.agenda_color`
    (schemas/professional.py): `#RRGGBB`, reforçado por um CHECK no
    banco (ver `models/appointment_status_style.py`)."""

    label: str | None = Field(default=None, min_length=1, max_length=40)
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class AppointmentStatusStyleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status_code: AppointmentStatus
    label: str | None
    color_hex: str | None
    updated_at: datetime
