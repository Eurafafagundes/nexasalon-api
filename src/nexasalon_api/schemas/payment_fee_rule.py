"""Etapa N3 — Taxas de Pagamento (Configurações > Taxas de Pagamento).
Ver `models/order.py::PaymentFeeRule` pro raciocínio de domínio.

Pix (Etapa N3.1) entra nesta MESMA modelagem — nunca uma entidade
paralela. Na API, `card_brand` é `None` pra Pix (nunca aparece nem é
aceito um campo de bandeira pra essa modalidade); o sentinela
`CardBrand.NOT_APPLICABLE` usado internamente na linha do banco é
convertido de/para `None` bem na borda deste schema, então o resto do
sistema (frontend, `PaymentFeeRuleRead`) nunca precisa saber que ele
existe."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexasalon_api.models.enums import CardBrand, PaymentMethod

_CARD_METHODS = {PaymentMethod.DEBIT, PaymentMethod.CREDIT}
_FEE_METHODS = {PaymentMethod.DEBIT, PaymentMethod.CREDIT, PaymentMethod.PIX}


class PaymentFeeRuleBase(BaseModel):
    method: PaymentMethod
    # `None` pra Pix (sem bandeira); obrigatório pra débito/crédito — ver
    # `_check_method_and_fields`. Nunca aceita o sentinela
    # `CardBrand.NOT_APPLICABLE` diretamente da API.
    card_brand: CardBrand | None = None
    # Sem limite superior próprio (item explícito "respeite o limite de
    # parcelas já suportado pelo fluxo atual — não invente um limite
    # diferente"): `PaymentCreate.installments` também só exige `ge=1`,
    # sem teto — mesma regra aqui. Default 1 pra débito/Pix (sempre à
    # vista) poderem omitir o campo.
    installments: int = Field(default=1, ge=1)
    fee_percent: Decimal = Field(ge=0, max_digits=5, decimal_places=2)

    @field_validator("card_brand")
    @classmethod
    def _reject_internal_sentinel(cls, value: CardBrand | None) -> CardBrand | None:
        if value == CardBrand.NOT_APPLICABLE:
            raise ValueError("Bandeira inválida.")
        return value

    @model_validator(mode="after")
    def _check_method_and_fields(self) -> "PaymentFeeRuleBase":
        if self.method not in _FEE_METHODS:
            raise ValueError("Taxas de pagamento só se aplicam a débito, crédito ou Pix.")
        if self.method in _CARD_METHODS:
            if self.card_brand is None:
                raise ValueError("Bandeira é obrigatória para débito ou crédito.")
            if self.method == PaymentMethod.DEBIT and self.installments != 1:
                raise ValueError("Débito é sempre 1 parcela.")
        else:  # PIX
            if self.card_brand is not None:
                raise ValueError("Pix não aceita bandeira.")
            if self.installments != 1:
                raise ValueError("Pix é sempre à vista (1 parcela).")
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

    @field_validator("card_brand", mode="before")
    @classmethod
    def _hide_internal_sentinel(cls, value: object) -> object:
        """Lendo do ORM: o sentinela `not_applicable` (linhas de Pix)
        nunca é exposto pela API — vira `None`, o mesmo valor que a
        própria API já aceitava/exigia na criação."""
        if value == CardBrand.NOT_APPLICABLE or value == CardBrand.NOT_APPLICABLE.value:
            return None
        return value
