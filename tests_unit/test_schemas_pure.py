"""Validações PURAS de schema (Pydantic) desta rodada — sem sessão, sem
banco, sem `ActorContext`. Extraído/movido de `tests/test_order_
consumption.py` e `tests/test_appointment_custom_status.py` (auditoria
"testes sem banco") pra rodar isolado de `tests/conftest.py`
(`pgserver`) — ver docstring de `tests_unit/test_units.py` pro
raciocínio completo de por que este diretório existe."""
import uuid
from decimal import Decimal

import pytest

from nexasalon_api.models.enums import OrderProductItemKind
from nexasalon_api.schemas.appointment_custom_status import AppointmentCustomStatusCreate
from nexasalon_api.schemas.order import OrderConsumptionCorrection, OrderProductItemCreate


def test_unit_price_no_payload_e_rejeitado_para_item_type_sale():
    """Venda (`item_type=SALE`, o default) nunca aceita `unit_price` no
    payload — o preço sempre vem do catálogo (`Product.sale_price`) no
    momento da adição, nunca do cliente."""
    with pytest.raises(ValueError):
        OrderProductItemCreate(
            product_id=uuid.uuid4(), quantity=Decimal("1"),
            item_type=OrderProductItemKind.SALE, unit_price=Decimal("10.00"),
        )


def test_unit_price_e_aceito_para_item_type_consumption():
    data = OrderProductItemCreate(
        product_id=uuid.uuid4(), quantity=Decimal("180"),
        item_type=OrderProductItemKind.CONSUMPTION, unit_price=Decimal("5.00"),
    )
    assert data.unit_price == Decimal("5.00")


def test_item_type_default_e_sale():
    data = OrderProductItemCreate(product_id=uuid.uuid4(), quantity=Decimal("1"))
    assert data.item_type == OrderProductItemKind.SALE


def test_quantity_delta_zero_e_rejeitado_pelo_schema():
    """Correção de consumo nunca aceita delta zero — não existe
    "correção" que não corrige nada."""
    with pytest.raises(ValueError):
        OrderConsumptionCorrection(quantity_delta=Decimal("0"), reason="Motivo qualquer")


def test_quantity_delta_positivo_e_negativo_sao_aceitos():
    positive = OrderConsumptionCorrection(quantity_delta=Decimal("20"), reason="Consumiu mais")
    negative = OrderConsumptionCorrection(quantity_delta=Decimal("-20"), reason="Consumiu menos")
    assert positive.quantity_delta == Decimal("20")
    assert negative.quantity_delta == Decimal("-20")


def test_cor_invalida_e_rejeitada_pelo_schema():
    """`color_hex` do status personalizado precisa ser `#RRGGBB` —
    validado no schema, antes de qualquer acesso ao banco."""
    with pytest.raises(ValueError):
        AppointmentCustomStatusCreate(name="Retorno", color_hex="roxo")


def test_cor_valida_e_aceita():
    data = AppointmentCustomStatusCreate(name="Retorno", color_hex="#8B5CF6")
    assert data.color_hex == "#8B5CF6"


def test_nome_vazio_e_rejeitado():
    with pytest.raises(ValueError):
        AppointmentCustomStatusCreate(name="", color_hex="#8B5CF6")
