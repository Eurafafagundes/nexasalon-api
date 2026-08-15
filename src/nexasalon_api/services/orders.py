"""Comanda/Pagamento — primeira versão funcional (item 3 da rodada
"Agenda visual/status, jornada por profissional e Comanda").

Fluxo: Appointment finalizado -> `create_order` copia os itens do
Appointment pra dentro da comanda (preço editável por linha, sem tocar
no catálogo nem no snapshot original) -> `update_item_price` edita uma
linha (auditado) -> `close_order` registra o(s) pagamento(s) e PROMOVE
o Appointment pra `paid` automaticamente, reaproveitando a MESMA
`appointment_state_machine.next_status` que já validava essa transição
manualmente desde a rodada anterior — por isso `close_order` já herda
de graça a regra "só dá pra pagar um agendamento `finished`" sem
precisar duplicá-la aqui.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import AppointmentStatus, AuditAction, OrderStatus
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import appointment_repo, audit_log_repo, order_item_repo, order_repo, payment_repo
from nexasalon_api.schemas.order import OrderClose, OrderItemPriceUpdate
from nexasalon_api.services import appointments as appointments_service


def _get_order_or_404(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = order_repo.get(session, organization_id, order_id)
    if order is None:
        raise NotFoundError("Comanda não encontrada.")
    return order


def _reload(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    session.flush()
    order = order_repo.get(session, organization_id, order_id)
    assert order is not None
    return order


def create_order(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Order:
    organization_id = actor.organization_id
    appointment = appointment_repo.get(session, organization_id, appointment_id)
    if appointment is None:
        raise NotFoundError("Agendamento não encontrado.")
    if order_repo.get_by_appointment(session, organization_id, appointment_id) is not None:
        raise ConflictError("Este agendamento já tem uma comanda.")
    if not appointment.items:
        raise ValidationDomainError("Agendamento sem nenhum serviço — não é possível abrir comanda.")

    order = order_repo.create(
        session,
        organization_id,
        appointment_id=appointment.id,
        branch_id=appointment.branch_id,
        client_id=appointment.client_id,
        created_by=actor.user_id,
    )
    for item in appointment.items:
        # Copia o snapshot do AppointmentItem 1:1 na abertura — a partir
        # daqui os dois vivem independentes (editar o preço da comanda
        # não altera o item original do agendamento, nem vice-versa).
        order_item_repo.create(
            session,
            organization_id,
            order_id=order.id,
            appointment_item_id=item.id,
            service_id=item.service_id,
            professional_id=item.professional_id,
            duration_minutes=item.duration_minutes,
            price=item.price,
        )
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.CREATE,
        new_values={"appointment_id": str(appointment_id), "items_count": len(appointment.items)},
    )
    return _reload(session, organization_id, order.id)


def get_order(session: Session, actor: ActorContext, order_id: uuid.UUID) -> Order:
    return _get_order_or_404(session, actor.organization_id, order_id)


def get_order_by_appointment(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Order | None:
    return order_repo.get_by_appointment(session, actor.organization_id, appointment_id)


def update_item_price(
    session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID, data: OrderItemPriceUpdate
) -> Order:
    organization_id = actor.organization_id
    order = _get_order_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível editar o preço de uma comanda aberta.")
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Item da comanda não encontrado.")

    old_price = item.price
    item.price = data.price
    session.flush()

    # Auditoria da edição manual de preço (item explícito da rodada:
    # "preço anterior, preço novo, usuário, data/hora" — os dois
    # últimos já vêm de graça de `AuditLog.user_id`/`created_at`).
    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order_item",
        entity_id=item.id,
        action=AuditAction.UPDATE,
        old_values={"price": str(old_price)},
        new_values={"price": str(data.price), "change_type": "manual_price_edit", "order_id": str(order_id)},
    )
    return _reload(session, organization_id, order_id)


def close_order(session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderClose) -> Order:
    organization_id = actor.organization_id
    order = _get_order_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ConflictError("Esta comanda já está fechada.")

    total = sum((item.price for item in order.items), Decimal("0"))
    paid_total = sum((p.amount for p in data.payments), Decimal("0"))
    if paid_total < total:
        raise ValidationDomainError(
            f"Valor pago (R$ {paid_total}) é menor que o total da comanda (R$ {total})."
        )

    for payment_in in data.payments:
        payment_repo.create(
            session,
            organization_id,
            order_id=order.id,
            method=payment_in.method,
            card_brand=payment_in.card_brand,
            installments=payment_in.installments,
            amount=payment_in.amount,
            created_by=actor.user_id,
        )

    # Promove o Appointment ANTES de marcar a comanda como fechada: se a
    # transição falhar (ex.: agendamento ainda não está `finished` —
    # `next_status` recusa `finished -> paid` fora dessa origem), a
    # comanda não fica fechada/com pagamento registrado sem o
    # agendamento ter virado `paid` — evita os dois ficarem
    # inconsistentes entre si. O usuário nunca precisa marcar "Pago"
    # manualmente à parte: fechar a comanda já faz isso.
    appointments_service.update_status(session, actor, order.appointment_id, AppointmentStatus.PAID)

    order.status = OrderStatus.CLOSED
    order.closed_at = datetime.now(timezone.utc)
    order.closed_by = actor.user_id
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.UPDATE,
        old_values={"status": "open"},
        new_values={"status": "closed", "change_type": "close_order", "paid_total": str(paid_total)},
    )
    return _reload(session, organization_id, order_id)
