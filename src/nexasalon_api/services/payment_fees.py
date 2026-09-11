"""Etapa N3 (+ N3.1, Pix) — Taxas de Pagamento. Fonte ÚNICA da fórmula
de taxa/líquido, usada tanto por `close_order` quanto por
`close_orders_consolidated` (`services/orders.py`) — nunca duas
implementações da mesma conta, e Pix passa pelo MESMO pipeline que
cartão (nunca um cálculo paralelo).

Regra central (item explícito do pedido): "cartão sem taxa NÃO
significa taxa zero". Estados possíveis, sempre gravados de forma
EXPLÍCITA em `Payment.fee_status` no momento da criação — nunca inferido
só pela nulidade dos snapshots (ver `models/enums.py::PaymentFeeStatus`
pro raciocínio completo):

  1. NOT_APPLICABLE — Dinheiro/outros não-cartão-e-não-Pix: taxa =
     R$ 0,00, líquido = bruto, sempre, sem precisar de nenhuma regra
     cadastrada. Pix SEM nenhuma `PaymentFeeRule` ativa correspondente
     TAMBÉM cai aqui — item explícito do pedido "se não existir taxa de
     Pix configurada, o comportamento deve continuar equivalente ao
     atual" (nunca UNCONFIGURED só por não ter sido configurado; Pix é
     opt-in, ao contrário de cartão).
  2. CALCULATED — débito/crédito/Pix com uma `PaymentFeeRule` ATIVA
     correspondente (organização + forma [+ bandeira/parcelas pra
     cartão] ): percentual/valor/líquido REAIS, congelados como
     snapshot.
  3. UNCONFIGURED — débito/crédito SEM regra correspondente: os 3
     snapshots ficam `None` de propósito — o pagamento continua válido,
     só o líquido fica desconhecido (nunca 0%, nunca o bruto disfarçado
     de líquido). Pix NUNCA fica UNCONFIGURED (ver item 1)."""
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.models.enums import CardBrand, PaymentFeeStatus, PaymentMethod
from nexasalon_api.models.order import Payment
from nexasalon_api.repositories import payment_fee_rule_repo

_CARD_METHODS = frozenset({PaymentMethod.DEBIT, PaymentMethod.CREDIT})

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class FeeResolution:
    payment_fee_rule_id: uuid.UUID | None
    fee_percent_snapshot: Decimal | None
    fee_amount_snapshot: Decimal | None
    net_amount_snapshot: Decimal | None
    fee_status: PaymentFeeStatus


def resolve_fee(
    session: Session,
    organization_id: uuid.UUID,
    *,
    method: PaymentMethod,
    card_brand: CardBrand | None,
    installments: int | None,
    amount: Decimal,
) -> FeeResolution:
    """Resolvida uma vez, NO MOMENTO DA CRIAÇÃO do `Payment` — nunca
    recalculada depois. `amount` é sempre o valor DESTE `Payment`
    individual (item explícito "a taxa deve ser calculada
    exclusivamente sobre o valor do Payment, nunca sobre o total da
    comanda" — pagamento dividido: cada lançamento resolve sua própria
    regra e tem seus próprios snapshots)."""
    if method == PaymentMethod.PIX:
        # Pix é opt-in: SEM regra ativa, o comportamento é IDÊNTICO ao
        # de antes desta feature (NOT_APPLICABLE, nunca UNCONFIGURED —
        # item explícito do pedido). `card_brand`/`installments` nunca
        # entram na busca pra Pix (sempre o sentinela interno, 1x — ver
        # `services/payment_fee_rules.py::_stored_card_brand`).
        rule = payment_fee_rule_repo.find_matching(
            session, organization_id, method=method, card_brand=CardBrand.NOT_APPLICABLE, installments=1
        )
        if rule is None:
            return FeeResolution(
                payment_fee_rule_id=None,
                fee_percent_snapshot=None,
                fee_amount_snapshot=None,
                net_amount_snapshot=None,
                fee_status=PaymentFeeStatus.NOT_APPLICABLE,
            )
    elif method not in _CARD_METHODS:
        return FeeResolution(
            payment_fee_rule_id=None,
            fee_percent_snapshot=None,
            fee_amount_snapshot=None,
            net_amount_snapshot=None,
            fee_status=PaymentFeeStatus.NOT_APPLICABLE,
        )
    else:
        # Débito: parcelas sempre 1 (coerente com o cadastro de regras —
        # ver `models/order.py::PaymentFeeRule`). Crédito sem parcelas
        # informadas = à vista (1x).
        normalized_installments = 1 if method == PaymentMethod.DEBIT else (installments or 1)

        rule = payment_fee_rule_repo.find_matching(
            session, organization_id, method=method, card_brand=card_brand, installments=normalized_installments
        )
        if rule is None:
            return FeeResolution(
                payment_fee_rule_id=None,
                fee_percent_snapshot=None,
                fee_amount_snapshot=None,
                net_amount_snapshot=None,
                fee_status=PaymentFeeStatus.UNCONFIGURED,
            )

    # Decimal em toda a conta (nunca float) — arredondamento pra 2 casas
    # segue a MESMA convenção já adotada no projeto (`.quantize(Decimal("0.01"))`,
    # sem `rounding=` explícito — usa o contexto decimal padrão do
    # processo, ROUND_HALF_EVEN, ver `services/dashboard.py`).
    fee_amount = (amount * rule.fee_percent / Decimal("100")).quantize(_CENTS)
    net_amount = amount - fee_amount

    return FeeResolution(
        payment_fee_rule_id=rule.id,
        fee_percent_snapshot=rule.fee_percent,
        fee_amount_snapshot=fee_amount,
        net_amount_snapshot=net_amount,
        fee_status=PaymentFeeStatus.CALCULATED,
    )


def derive_fee_status(payment: Payment) -> PaymentFeeStatus:
    """Leitura (Extrato) — para pagamentos criados DEPOIS da migration
    0034 (ou DEPOIS de 0050/0051, no caso de Pix), `payment.fee_status`
    já vem preenchido explicitamente (usar direto). Para pagamentos
    HISTÓRICOS (`fee_status IS NULL` — antes da coluna existir, ou Pix
    de antes da Etapa N3.1 existir), deriva só o suficiente pra não
    classificar errado: método de cartão sem informação de taxa é
    "desconhecida" (nunca 0%); Pix/Dinheiro continuam sem incidência
    mesmo sem o dado explícito — eram literalmente sempre sem taxa
    nessas competências, então `NOT_APPLICABLE` é o valor correto, não
    uma aproximação."""
    if payment.fee_status is not None:
        return payment.fee_status
    if payment.method in _CARD_METHODS:
        return PaymentFeeStatus.UNCONFIGURED
    return PaymentFeeStatus.NOT_APPLICABLE


@dataclass(frozen=True)
class PaymentFeeBreakdown:
    fee_status: PaymentFeeStatus
    fee_amount: Decimal | None
    net_amount: Decimal | None


def breakdown_for_display(payment: Payment) -> PaymentFeeBreakdown:
    """NOT_APPLICABLE é sempre computável (nunca ambíguo — Pix/Dinheiro
    literalmente nunca tiveram taxa), então preenche `fee_amount`/
    `net_amount` mesmo pra pagamentos ANTIGOS que nunca tiveram esses
    snapshots gravados. CALCULATED usa exclusivamente o snapshot
    congelado (nunca recalcula a partir da regra ao vivo). UNCONFIGURED
    NUNCA inventa um valor — fica `None`, explicitamente desconhecido."""
    status = derive_fee_status(payment)
    if status == PaymentFeeStatus.NOT_APPLICABLE:
        return PaymentFeeBreakdown(fee_status=status, fee_amount=Decimal("0"), net_amount=payment.amount)
    if status == PaymentFeeStatus.CALCULATED:
        return PaymentFeeBreakdown(
            fee_status=status, fee_amount=payment.fee_amount_snapshot, net_amount=payment.net_amount_snapshot
        )
    return PaymentFeeBreakdown(fee_status=PaymentFeeStatus.UNCONFIGURED, fee_amount=None, net_amount=None)
