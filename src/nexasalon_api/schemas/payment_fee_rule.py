"""Etapa N3 — Taxas de Pagamento (Configurações > Taxas de Pagamento).
Ver `models/order.py::PaymentFeeRule` pro raciocínio de domínio."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.enums import CardBrand, PaymentMethod

_CARD_METHODS = {PaymentMethod.DEBIT, PaymentMethod.CREDIT}


class PaymentFeeRuleBase(BaseModel):
    method: PaymentMethod
    card_brand: CardBrand
    # Sem limite superior próprio (item explícito "respeite o limite de
    # parcelas já suportado pelo fluxo atual — não invente um limite
    # diferente"): `PaymentCreate.installments` também só exige `ge=1`,
    # sem teto — mesma regra aqui.
    installments: int = Field(ge=1)
    fee_percent: Decimal = Field(ge=0, max_digits=5, decimal_places=2)

    @model_validator(mode="after")
    def _check_method_and_installments(self) -> "PaymentFeeRuleBase":
        if self.method not in _CARD_METHODS:
            raise ValueError("Taxas de pagamento só se aplicam a débito ou crédito.")
        if self.method == PaymentMethod.DEBIT and self.installments != 1:
            raise ValueError("Débito é sempre 1 parcela.")
        return self


class PaymentFeeRuleCreate(PaymentFeeRuleBase):
    pass


class PaymentFeeRuleUpdate(BaseModel):
    """Só `fee_percent` é editável — mudar forma/bandeira/parcelas de
    uma regra existente seria, na prática, uma regra DIFERENTE (a
    unicidade lógica é sobre esses 3 campos); crie outra regra em vez
    de "transformar" esta. Editar aqui NUNCA recalcula pagamentos já
    feitos com o percentual anterior (snapshot em `Payment` é
    imutável)."""

    fee_percent: Decimal = Field(ge=0, max_digits=5, decimal_places=2)


class PaymentFeeRuleRead(PaymentFeeRuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
