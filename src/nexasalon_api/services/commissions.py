"""Etapa C2 — Comissão por serviço vendido. Fonte ÚNICA da fórmula de
comissão, usada tanto por `close_order` quanto por
`close_orders_consolidated` (`services/orders.py`) — nunca duas
implementações da mesma conta.

Regra central (mesmo raciocínio de `services/payment_fees.py`, Etapa
N3): "sem regra configurada NÃO significa comissão zero". Dois estados
possíveis, sempre gravados de forma EXPLÍCITA em
`OrderItem.commission_status` no momento do fechamento — nunca
inferido só pela nulidade dos snapshots (ver
`models/enums.py::CommissionStatus` pro raciocínio completo):

  1. CALCULATED — existe uma `ProfessionalService` ATIVA com
     `commission_type`/`commission_value` configurados pra este par
     (profissional, serviço): percentual/valor/comissão REAIS,
     congelados como snapshot.
  2. NOT_CONFIGURED — vínculo ausente, inativo, ou sem comissão
     configurada: os 3 snapshots ficam `None` de propósito — o item
     continua válido, só a comissão fica desconhecida (nunca R$0,00,
     nunca inventada).

A base do percentual é `OrderItem.price` — o valor efetivamente
vendido daquele item (já reflete qualquer edição manual de preço feita
enquanto a comanda estava aberta; não existe conceito de desconto
separado no domínio, ver `services/order_totals.py`)."""
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.models.enums import CommissionStatus, CommissionType
from nexasalon_api.repositories import professional_service_repo

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class CommissionResolution:
    commission_type_snapshot: CommissionType | None
    commission_value_snapshot: Decimal | None
    commission_amount_snapshot: Decimal | None
    commission_status: CommissionStatus


def resolve_commission(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    service_id: uuid.UUID,
    price: Decimal,
) -> CommissionResolution:
    """Resolvida uma vez, NO FECHAMENTO da comanda — nunca recalculada
    depois. `price` é sempre o valor DESTE `OrderItem` individual (item
    explícito "1 OrderItem = 1 serviço + 1 profissional + 1 valor" —
    nunca a soma da comanda inteira, o que duplicaria comissão entre
    profissionais diferentes na mesma comanda)."""
    link = professional_service_repo.get_for_pair(session, organization_id, professional_id, service_id)
    if link is None or not link.is_active or link.commission_type is None:
        return CommissionResolution(
            commission_type_snapshot=None,
            commission_value_snapshot=None,
            commission_amount_snapshot=None,
            commission_status=CommissionStatus.NOT_CONFIGURED,
        )

    # Decimal em toda a conta (nunca float) — mesma convenção do
    # projeto (`.quantize(Decimal("0.01"))`, contexto decimal padrão
    # do processo, ROUND_HALF_EVEN). `commission_value` vem tipado
    # `float | None` na ORM (coluna `Numeric`, mesmo padrão de
    # `ProfessionalService.price_override`), por isso o `Decimal(str(...))`
    # defensivo — ver `services/availability.py::effective_duration_and_price`.
    commission_value = Decimal(str(link.commission_value))
    if link.commission_type == CommissionType.PERCENTAGE:
        commission_amount = (price * commission_value / Decimal("100")).quantize(_CENTS)
    else:
        commission_amount = commission_value.quantize(_CENTS)

    return CommissionResolution(
        commission_type_snapshot=link.commission_type,
        commission_value_snapshot=commission_value,
        commission_amount_snapshot=commission_amount,
        commission_status=CommissionStatus.CALCULATED,
    )
