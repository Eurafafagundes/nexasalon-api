from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from nexasalon_api.core.normalize import normalize_phone
from nexasalon_api.models.enums import BrazilianState
from nexasalon_api.schemas.auth import TokenPairRead

BusinessType = Literal[
    "salao",
    "mega_hair",
    "barbearia",
    "estetica",
    "nail_designer",
    "cilios_sobrancelhas",
    "outro",
]


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: str = Field(min_length=8, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    business_name: str = Field(min_length=2, max_length=255)
    business_type: BusinessType
    business_phone: str = Field(min_length=8, max_length=32)
    city: str = Field(min_length=2, max_length=120)
    state: BrazilianState

    @field_validator("full_name", "business_name", "city")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Este campo é obrigatório.")
        return normalized

    @field_validator("phone", "business_phone")
    @classmethod
    def normalize_and_validate_phone(cls, value: str) -> str:
        normalized = normalize_phone(value)
        if len(normalized) not in (10, 11):
            raise ValueError("Informe um telefone brasileiro válido com DDD.")
        return normalized


class SignupResponse(BaseModel):
    tokens: TokenPairRead
