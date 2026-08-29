"""Testes de `core/units.py` — utilitário único de conversão de
unidade (item "Não quero `* 1000`/`/ 1000` espalhado... Criar
utilitário único de conversão. Não espalhar. Aceitar apenas unidades
compatíveis."). Puramente unitário, sem banco."""
from decimal import Decimal

import pytest

from nexasalon_api.core import units
from nexasalon_api.models.enums import ProductUnit


def test_1kg_igual_a_1000g():
    assert units.convert_quantity(Decimal("1"), ProductUnit.KG, ProductUnit.GRAM) == Decimal("1000")


def test_meio_kg_igual_a_500g():
    assert units.convert_quantity(Decimal("0.5"), ProductUnit.KG, ProductUnit.GRAM) == Decimal("500")


def test_75g_convertido_para_kg():
    assert units.convert_quantity(Decimal("75"), ProductUnit.GRAM, ProductUnit.KG) == Decimal("0.075")


def test_mesma_unidade_retorna_valor_intocado():
    assert units.convert_quantity(Decimal("42.5"), ProductUnit.GRAM, ProductUnit.GRAM) == Decimal("42.5")


def test_litro_para_ml():
    assert units.convert_quantity(Decimal("1"), ProductUnit.LITER, ProductUnit.ML) == Decimal("1000")


def test_kg_para_ml_e_incompativel():
    with pytest.raises(ValueError):
        units.convert_quantity(Decimal("1"), ProductUnit.KG, ProductUnit.ML)


def test_grama_para_unidade_e_incompativel():
    with pytest.raises(ValueError):
        units.convert_quantity(Decimal("1"), ProductUnit.GRAM, ProductUnit.UNIT)


def test_is_convertible():
    assert units.is_convertible(ProductUnit.GRAM, ProductUnit.KG) is True
    assert units.is_convertible(ProductUnit.GRAM, ProductUnit.ML) is False


def test_to_grams_a_partir_de_kg():
    assert units.to_grams(Decimal("2.35"), ProductUnit.KG) == Decimal("2350")


def test_to_grams_rejeita_unidade_nao_peso():
    with pytest.raises(ValueError):
        units.to_grams(Decimal("1"), ProductUnit.UNIT)


def test_sem_float_drift_em_conversoes_encadeadas():
    """1000g -> kg -> g precisa voltar EXATO (Decimal, nunca float)."""
    grams = Decimal("1000")
    kg = units.convert_quantity(grams, ProductUnit.GRAM, ProductUnit.KG)
    back_to_grams = units.convert_quantity(kg, ProductUnit.KG, ProductUnit.GRAM)
    assert back_to_grams == grams
