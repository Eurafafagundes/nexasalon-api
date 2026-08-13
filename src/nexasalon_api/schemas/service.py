import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.enums import CommissionType


class ServiceBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "LEGADO/compatibilidade — texto livre. Prefira `category_id`. "
            "Mantido só para quem ainda depende do texto livre anterior a `ServiceCategory`."
        ),
    )
    category_id: uuid.UUID | None = Field(
        default=None,
        description="CAMINHO PREFERENCIAL de categorização — FK para uma `ServiceCategory` da mesma organização.",
    )
    description: str | None = None
    default_duration_minutes: int = Field(gt=0)
    default_price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    allow_online_booking: bool = True
    display_order: int = Field(default=0, ge=0, le=32767)

    # Preparação de domínio para sinal/depósito — apenas armazenados e
    # validados aqui; NENHUMA lógica de pagamento/sinal está implementada
    # nesta etapa (ver docstring do model `Service`).
    requires_deposit: bool = Field(
        default=False,
        description="Preparação de domínio (Etapa 3B-prep) — nenhuma lógica de pagamento/sinal implementada ainda.",
    )
    deposit_type: CommissionType | None = Field(
        default=None, description="Preparação de domínio — ver `requires_deposit`."
    )
    deposit_value: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=10,
        decimal_places=2,
        description="Preparação de domínio — ver `requires_deposit`.",
    )

    # Preparação para buffer/intervalo antes-depois do atendimento — o
    # motor de disponibilidade (`services/availability.py`) e a
    # checagem de conflito (`services/appointments.py`) AINDA NÃO leem
    # estes campos.
    buffer_before_minutes: int = Field(
        default=0,
        ge=0,
        le=1440,
        description="Preparação de domínio (Etapa 3B-prep) — ainda não aplicado pelo motor de disponibilidade/conflito.",
    )
    buffer_after_minutes: int = Field(
        default=0,
        ge=0,
        le=1440,
        description="Preparação de domínio (Etapa 3B-prep) — ainda não aplicado pelo motor de disponibilidade/conflito.",
    )

    @model_validator(mode="after")
    def _check_deposit(self) -> "ServiceBase":
        if self.requires_deposit and (self.deposit_type is None or self.deposit_value is None):
            raise ValueError("requires_deposit=true exige deposit_type e deposit_value preenchidos.")
        if self.deposit_type == CommissionType.PERCENTAGE and self.deposit_value is not None:
            if self.deposit_value > 100:
                raise ValueError("deposit_value percentual não pode passar de 100.")
        if (self.deposit_type is None) != (self.deposit_value is None):
            raise ValueError("deposit_type e deposit_value devem ser preenchidos juntos.")
        return self


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
