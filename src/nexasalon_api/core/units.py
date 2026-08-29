"""Conversão de unidade de medida — utilitário ÚNICO (item "Não quero
`* 1000`/`/ 1000` espalhado pelo frontend/backend inteiro. Criar
utilitário único de conversão.") pra Estoque por Peso/Volume.

Escopo deliberado: só converte DENTRO do mesmo grupo (peso↔peso,
volume↔volume) — nunca `kg → ml` nem `gram → unidade` (item explícito
"Aceitar apenas unidades compatíveis"). `Product.unit` continua sendo o
enum já existente (`gram`/`kg`/`ml`/`liter`/...) sem NENHUMA migration
de dado: este módulo só existe pra permitir que o usuário DIGITE em
qualquer unidade compatível (ex.: "1000 g" numa comanda de um produto
cadastrado em `kg`) e o valor seja convertido pra unidade do produto
ANTES de chegar em `StockMovementCreate`/`OrderProductItemCreate` — a
persistência em si nunca muda de unidade.

Espelhado no frontend em `src/lib/quantity.ts` (mesmos fatores,
mesmo agrupamento) — qualquer mudança aqui precisa da mudança
equivalente lá."""
from decimal import Decimal

from nexasalon_api.models.enums import ProductUnit

# Fator de conversão de CADA unidade pra sua unidade-base de grupo
# (grama pra peso, mililitro pra volume) — nunca o inverso escrito à
# mão em outro lugar.
_WEIGHT_BASE_FACTORS: dict[ProductUnit, Decimal] = {
    ProductUnit.GRAM: Decimal("1"),
    ProductUnit.KG: Decimal("1000"),
}
_VOLUME_BASE_FACTORS: dict[ProductUnit, Decimal] = {
    ProductUnit.ML: Decimal("1"),
    ProductUnit.LITER: Decimal("1000"),
}

WEIGHT_UNITS = frozenset(_WEIGHT_BASE_FACTORS)
VOLUME_UNITS = frozenset(_VOLUME_BASE_FACTORS)

_GROUPS = (_WEIGHT_BASE_FACTORS, _VOLUME_BASE_FACTORS)


def _group_for(unit: ProductUnit) -> dict[ProductUnit, Decimal] | None:
    for group in _GROUPS:
        if unit in group:
            return group
    return None


def is_convertible(from_unit: ProductUnit, to_unit: ProductUnit) -> bool:
    group = _group_for(from_unit)
    return group is not None and to_unit in group


def convert_quantity(value: Decimal, from_unit: ProductUnit, to_unit: ProductUnit) -> Decimal:
    """Converte `value` de `from_unit` pra `to_unit` — só aceita as duas
    dentro do MESMO grupo (peso ou volume). Levanta `ValueError` pra
    qualquer combinação incompatível (`kg → ml`, `gram → unit` etc.) —
    quem chama decide o que fazer com isso (na API, isso vira um 422/400
    através do fluxo normal de validação do Pydantic/service layer)."""
    if from_unit == to_unit:
        return value
    group = _group_for(from_unit)
    if group is None or to_unit not in group:
        raise ValueError(f"Não é possível converter '{from_unit.value}' para '{to_unit.value}'.")
    base_value = value * group[from_unit]
    return base_value / group[to_unit]


def to_grams(value: Decimal, unit: ProductUnit) -> Decimal:
    """Atalho pra formatação/exibição — sempre em gramas, independente
    de o produto estar cadastrado em `gram` ou `kg` (ver `formatWeight`
    equivalente no frontend, que decide g vs kg pelo valor em gramas,
    nunca pela unidade cadastrada do produto)."""
    if unit not in _WEIGHT_BASE_FACTORS:
        raise ValueError(f"'{unit.value}' não é uma unidade de peso.")
    return convert_quantity(value, unit, ProductUnit.GRAM)
