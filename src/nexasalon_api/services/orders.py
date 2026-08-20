"""Comanda/Pagamento — primeira versão funcional (item 3 da rodada
"Agenda visual/status, jornada por profissional e Comanda").

Fluxo: `create_order` copia os itens do Appointment pra dentro da
comanda (preço editável por linha, sem tocar no catálogo nem no
snapshot original) -> `update_item_price` edita uma linha (auditado) ->
`close_order` registra o(s) pagamento(s) e PROMOVE o Appointment pra
`paid` automaticamente via `appointments_service.mark_paid`.

Rodada "status livre + Comanda desacoplada da sequência": a Comanda
pode ser aberta e fechada em QUALQUER status operacional do Appointment
(não exige ter passado por `finished` antes — `mark_paid` só recusa se
o Appointment já está `paid` ou `cancelled`). "Pago" nunca é um clique
manual na Agenda: só existe como consequência de `close_order`.

`close_order` agora também exige, por lançamento de pagamento, um
`cash_register_id` de um caixa ABERTO da mesma organização (item
"Caixa Diário" — pagamento nunca é registrado sem caixa selecionado,
reaproveitando `services/cash_register.py::assert_register_open_and_in_org`,
nunca abre um caixa sozinho).

Etapa C (Estoque ↔ Comanda) acrescenta, sem alterar nada do que já
existia acima:

  - `add_product_item`/`remove_product_item`/`update_product_item` —
    produto na comanda ABERTA. Remover antes do fechamento nunca gera
    baixa (não existe movimentação pra desfazer — nenhuma foi criada
    ainda, a baixa só acontece no fechamento).
  - `update_order_item` (renomeado de `update_item_price`, mesmo
    contrato HTTP) — agora também aceita editar `duration_minutes`,
    cada campo auditado independentemente.
  - `close_order` passa a: (a) somar produtos no total da comanda,
    nunca duas contas de faturamento diferentes; (b) travar a linha da
    comanda (`order_repo.get_for_update`) ANTES de checar o status —
    torna um retry de fechamento seguro (a segunda tentativa, seja
    sequencial ou concorrente, sempre enxerga `status=closed` já
    commitado e recusa com 409, nunca refaz a baixa); (c) baixar
    estoque de cada linha de produto (`stock_service.record_sale_movement`,
    reason=SALE, `order_id` preenchido) DENTRO da mesma transação da
    request — estoque insuficiente levanta `ValidationDomainError`,
    que aborta a transação inteira (nenhum pagamento fica "pendurado",
    a comanda continua `OPEN`, ver `api/deps.py::get_db`)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import AuditAction, OrderStatus
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import (
    appointment_repo,
    audit_log_repo,
    client_repo,
    order_item_repo,
    order_product_item_repo,
    order_repo,
    organization_repo,
    payment_repo,
    product_repo,
    professional_repo,
    service_repo,
    user_repo,
)
from nexasalon_api.schemas.order import (
    OrderClose,
    OrderItemUpdate,
    OrderProductItemCreate,
    OrderProductItemUpdate,
    OrderReceiptRead,
)
from nexasalon_api.services import appointments as appointments_service
from nexasalon_api.services import cash_register as cash_register_service
from nexasalon_api.services import stock as stock_service


def _get_order_or_404(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = order_repo.get(session, organization_id, order_id)
    if order is None:
        raise NotFoundError("Comanda não encontrada.")
    return order


def _get_order_for_update_or_404(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = order_repo.get_for_update(session, organization_id, order_id)
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
        # `service_name`/`professional_name` são capturados AGORA
        # (item "snapshot histórico") — nem `AppointmentItem` nem
        # `Service`/`Professional` guardam esse nome já congelado, e
        # ler o catálogo atual depois mudaria como uma venda antiga
        # aparece se o serviço for renomeado ou o profissional sair.
        service = service_repo.get(session, organization_id, item.service_id)
        professional = professional_repo.get(session, organization_id, item.professional_id)
        order_item_repo.create(
            session,
            organization_id,
            order_id=order.id,
            appointment_item_id=item.id,
            service_id=item.service_id,
            professional_id=item.professional_id,
            duration_minutes=item.duration_minutes,
            price=item.price,
            service_name=service.name if service is not None else "Serviço removido",
            professional_name=professional.name if professional is not None else "Profissional removido",
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


def list_orders(
    session: Session,
    actor: ActorContext,
    *,
    status: OrderStatus | None = None,
    client_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
    order_number: int | None = None,
    date_from=None,
    date_to=None,
) -> list[Order]:
    """Financeiro > Comandas (Abertas/Finalizadas, item 13/14)."""
    return order_repo.list_for_org(
        session, actor.organization_id, status=status, client_id=client_id, professional_id=professional_id,
        order_number=order_number, date_from=date_from, date_to=date_to,
    )


def get_order_by_appointment(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Order | None:
    return order_repo.get_by_appointment(session, actor.organization_id, appointment_id)


def get_order_receipt(session: Session, actor: ActorContext, order_id: uuid.UUID) -> OrderReceiptRead:
    """Comprovante de Atendimento (Etapa D) — ver docstring de
    `schemas/order.py::OrderReceiptRead` pro raciocínio completo
    (snapshot-only, nunca NF/NFS-e, sem observações internas). Só
    comandas FECHADAS emitem comprovante — item do pedido "em uma
    Comanda fechada/paga, disponibilizar 'Imprimir comprovante'"; uma
    comanda aberta ainda não tem pagamento registrado pra mostrar."""
    order = _get_order_or_404(session, actor.organization_id, order_id)
    if order.status != OrderStatus.CLOSED:
        raise ValidationDomainError("Comprovante só está disponível para comandas fechadas.")
    client = client_repo.get(session, actor.organization_id, order.client_id)
    if client is None:
        raise NotFoundError("Cliente da comanda não encontrado.")
    organization = organization_repo.get(session, actor.organization_id)
    if organization is None:
        raise NotFoundError("Organização não encontrada.")
    return OrderReceiptRead.build(order, client, organization)


def update_order_item(
    session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID, data: OrderItemUpdate
) -> Order:
    """Edita `price` e/ou `duration_minutes` de uma linha de SERVIÇO —
    cada campo alterado gera sua PRÓPRIA linha de auditoria. Nunca
    escreve de volta em `AppointmentItem`/`Service` (ver docstring do
    módulo) — "duração" aqui é só o snapshot local da comanda, então
    editar não reescreve retroativamente nenhum relatório/KPI que já
    tenha sido calculado em cima do `AppointmentItem` original."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível editar uma linha de comanda aberta.")
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Item da comanda não encontrado.")

    changes: dict[str, tuple[str, str]] = {}
    if data.price is not None and data.price != item.price:
        changes["price"] = (str(item.price), str(data.price))
        item.price = data.price
    if data.duration_minutes is not None and data.duration_minutes != item.duration_minutes:
        changes["duration_minutes"] = (str(item.duration_minutes), str(data.duration_minutes))
        item.duration_minutes = data.duration_minutes

    if not changes:
        return _reload(session, organization_id, order_id)

    session.flush()
    # Uma linha de AuditLog POR CAMPO alterado (item explícito "preço
    # anterior, preço novo, usuário, data/hora" — os dois últimos já
    # vêm de `AuditLog.user_id`/`created_at`) — nunca uma linha só
    # misturando os dois campos, pra o histórico deixar claro qual
    # mudança aconteceu quando o usuário edita só um dos dois.
    for field, (old_value, new_value) in changes.items():
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=actor.user_id,
            entity_type="order_item",
            entity_id=item.id,
            action=AuditAction.UPDATE,
            old_values={field: old_value},
            new_values={field: new_value, "change_type": f"manual_{field}_edit", "order_id": str(order_id)},
        )
    return _reload(session, organization_id, order_id)


# Nome anterior, mantido como alias — nenhuma rota nem teste externo
# precisa saber que foi renomeado.
update_item_price = update_order_item


def add_product_item(
    session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderProductItemCreate
) -> Order:
    """Adiciona um produto à comanda ABERTA. `unit_price` nasce sempre
    do catálogo (`Product.sale_price`), nunca do payload — mesma
    filosofia de `OrderItem.price` nascer de `AppointmentItem.price`.
    A baixa de estoque NÃO acontece aqui — só no fechamento (ver
    `close_order`); adicionar/remover um produto numa comanda aberta
    nunca move uma unidade sequer de `StockLevel`.

    Item "produto precisa pertencer/estar disponível na unidade da
    comanda": `Product` é catálogo de ORGANIZAÇÃO (não tem `branch_id`
    — só `StockLevel`, por linha, é por unidade, ver `models/product.py`),
    então não existe "produto da unidade X" pra bloquear aqui. A regra
    vira, na prática, "a disponibilidade é sempre checada contra a
    unidade da COMANDA, nunca outra": tanto aqui (produto só precisa
    existir/estar ativo na organização) quanto na baixa (`close_order`
    sempre chama `record_sale_movement` com `branch_id=order.branch_id`
    — nunca lê/decrementa `StockLevel` de nenhuma outra unidade). Não
    exigimos uma linha de `StockLevel` já existente pra esta unidade no
    momento de adicionar: `StockLevel` nasce sob demanda na primeira
    movimentação (Etapa B), então isso bloquearia a primeira venda
    legítima de um produto que a unidade ainda não tinha estocado."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível adicionar produto a uma comanda aberta.")

    product = product_repo.get(session, organization_id, data.product_id)
    if product is None:
        raise NotFoundError("Produto não encontrado.")
    if not product.is_active:
        raise ValidationDomainError("Produto inativo não pode ser vendido.")
    if not product.for_sale:
        raise ValidationDomainError("Este produto é de uso interno e não pode ser vendido em comanda.")
    if product.sale_price is None:
        raise ValidationDomainError("Produto sem preço de venda definido — defina um preço antes de vender.")

    item = order_product_item_repo.create(
        session,
        organization_id,
        order_id=order.id,
        product_id=product.id,
        product_name=product.name,
        quantity=data.quantity,
        unit_price=product.sale_price,
    )
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order_product_item",
        entity_id=item.id,
        action=AuditAction.CREATE,
        new_values={
            "order_id": str(order_id), "product_id": str(product.id),
            "quantity": str(data.quantity), "unit_price": str(product.sale_price),
        },
    )
    return _reload(session, organization_id, order_id)


def remove_product_item(session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID) -> Order:
    """Remove uma linha de produto ANTES do fechamento — nunca gera
    baixa de estoque (item explícito do pedido), porque nenhuma
    movimentação chegou a ser criada pra esta linha (baixa só
    acontece em `close_order`, e só pra linhas que sobreviverem até
    lá)."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível remover produto de uma comanda aberta.")
    item = next((i for i in order.product_items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Produto da comanda não encontrado.")
    # Sanidade estrutural: uma comanda OPEN nunca deveria ter uma linha
    # já com `stock_movement_id` preenchido (baixa só acontece no
    # fechamento, que promove a comanda pra CLOSED na mesma transação)
    # — mas a checagem custa nada e documenta a invariante explicitamente.
    assert item.stock_movement_id is None, "linha de comanda aberta não deveria ter baixa de estoque associada"

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order_product_item",
        entity_id=item.id,
        action=AuditAction.DELETE,
        old_values={
            "order_id": str(order_id), "product_id": str(item.product_id),
            "quantity": str(item.quantity), "unit_price": str(item.unit_price),
        },
    )
    order_product_item_repo.delete(session, item)
    return _reload(session, organization_id, order_id)


def update_product_item(
    session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID, data: OrderProductItemUpdate
) -> Order:
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível editar uma linha de comanda aberta.")
    item = next((i for i in order.product_items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Produto da comanda não encontrado.")

    changes: dict[str, tuple[str, str]] = {}
    if data.quantity is not None and data.quantity != item.quantity:
        changes["quantity"] = (str(item.quantity), str(data.quantity))
        item.quantity = data.quantity
    if data.unit_price is not None and data.unit_price != item.unit_price:
        changes["unit_price"] = (str(item.unit_price), str(data.unit_price))
        item.unit_price = data.unit_price

    if not changes:
        return _reload(session, organization_id, order_id)

    session.flush()
    for field, (old_value, new_value) in changes.items():
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=actor.user_id,
            entity_type="order_product_item",
            entity_id=item.id,
            action=AuditAction.UPDATE,
            old_values={field: old_value},
            new_values={field: new_value, "change_type": f"manual_{field}_edit", "order_id": str(order_id)},
        )
    return _reload(session, organization_id, order_id)


def close_order(session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderClose) -> Order:
    """Fecha a comanda: registra pagamento(s), baixa estoque de cada
    linha de produto e promove o Appointment pra `paid` — tudo dentro
    da MESMA transação da request (`api/deps.py::get_db` só commita no
    final; qualquer exceção aqui reverte tudo junto).

    Retry-safety (item explícito "retry não pode gerar duas saídas"):
    a linha da comanda é travada com `SELECT ... FOR UPDATE`
    (`order_repo.get_for_update`) ANTES de checar o status. Duas
    tentativas de fechamento da MESMA comanda — sequenciais ou
    concorrentes — nunca correm em paralelo de verdade: a segunda
    bloqueia até a primeira commitar, e então lê `status=closed` já
    persistido e recusa com 409, sem criar um segundo Payment nem uma
    segunda baixa de estoque. Isto é ortogonal ao lock de CONCORRÊNCIA
    de estoque em si (`stock_level_repo.lock_or_create`, `SELECT ...
    FOR UPDATE` na linha de `StockLevel`) — que é quem serializa duas
    comandas DIFERENTES disputando a última unidade do mesmo produto
    na mesma unidade (ver `services/stock.py`)."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ConflictError("Esta comanda já está fechada.")

    services_total = sum((item.price for item in order.items), Decimal("0"))
    products_total = sum((item.quantity * item.unit_price for item in order.product_items), Decimal("0"))
    total = services_total + products_total
    paid_total = sum((p.amount for p in data.payments), Decimal("0"))
    if paid_total < total:
        raise ValidationDomainError(
            f"Valor pago (R$ {paid_total}) é menor que o total da comanda (R$ {total})."
        )

    # Valida TODOS os caixas informados antes de criar qualquer
    # Payment — falha rápido (sem criar pagamento parcial) se algum
    # dos lançamentos apontar pra um caixa de outra organização, que
    # não existe, ou que já está fechado.
    for payment_in in data.payments:
        cash_register_service.assert_register_open_and_in_org(session, organization_id, payment_in.cash_register_id)

    # Baixa de estoque — ANTES de criar qualquer Payment (falha rápido:
    # se faltar estoque pra algum produto, a comanda nem chega a
    # registrar pagamento). `product_repo.get` pode devolver `None`
    # numa borda teórica (produto desativado depois de adicionado à
    # comanda continua vendável — só bloqueamos inativo no MOMENTO de
    # adicionar, ver `add_product_item` — então "sumir" só aconteceria
    # se o produto tivesse sido de fato apagado, o que o FK RESTRICT
    # impede enquanto existir referência; o `is not None` é defesa em
    # profundidade, não um caminho esperado).
    for product_item in order.product_items:
        if product_item.stock_movement_id is not None:
            # Defesa em profundidade — não deveria ser alcançável (o
            # lock da comanda já impede uma segunda passagem por aqui
            # pra qualquer retry), mas se acontecer, nunca baixa de novo.
            continue
        product = product_repo.get(session, organization_id, product_item.product_id)
        try:
            movement = stock_service.record_sale_movement(
                session,
                actor,
                product_id=product_item.product_id,
                branch_id=order.branch_id,
                quantity=product_item.quantity,
                order_id=order.id,
                unit_cost=product.cost_price if product is not None else None,
            )
        except ValidationDomainError as exc:
            raise ValidationDomainError(
                f"Estoque insuficiente para '{product_item.product_name}': {exc.message}"
            ) from exc
        product_item.stock_movement_id = movement.id
    session.flush()

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None

    for payment_in in data.payments:
        payment_repo.create(
            session,
            organization_id,
            order_id=order.id,
            cash_register_id=payment_in.cash_register_id,
            method=payment_in.method,
            card_brand=payment_in.card_brand,
            installments=payment_in.installments,
            amount=payment_in.amount,
            created_by=actor.user_id,
            created_by_name=actor_name,
        )

    # Promove o Appointment ANTES de marcar a comanda como fechada: se a
    # transição falhar (ex.: agendamento já `paid` ou `cancelled` — ver
    # `appointment_state_machine.mark_paid`), a comanda não fica
    # fechada/com pagamento registrado sem o agendamento ter virado
    # `paid` — evita os dois ficarem inconsistentes entre si. Item
    # "não condicione a Comanda a ter passado por todos os status":
    # `mark_paid` promove de QUALQUER status operacional direto pra
    # `paid`, não exige `finished` antes. O usuário nunca precisa
    # marcar "Pago" manualmente à parte: fechar a comanda já faz isso.
    appointments_service.mark_paid(session, actor, order.appointment_id)

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
        new_values={
            "status": "closed", "change_type": "close_order", "paid_total": str(paid_total),
            "services_total": str(services_total), "products_total": str(products_total),
        },
    )
    return _reload(session, organization_id, order_id)
