"""Schemas da Comanda/Pagamento — ver `models/order.py` para o
raciocínio de domínio (3 camadas de preço, `Payment` como lista)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import CardBrand, OrderStatus, PaymentMethod
from nexasalon_api.models.order import Order
from nexasalon_api.models.organization import Organization

_CARD_METHODS = frozenset({PaymentMethod.DEBIT, PaymentMethod.CREDIT})


class OrderCreate(BaseModel):
    appointment_id: uuid.UUID


class OrderCancel(BaseModel):
    """`POST /orders/{id}/cancel` (Etapa F, item "Cancelar Comanda").

    Etapa I supersede deliberado: o pedido original tratava `reason`
    como opcional; a rodada "Comandas relacionadas e fechamento
    consolidado" pede explicitamente "motivo obrigatório" pro
    cancelamento lógico — a partir daqui `reason` é required (nunca
    apagamos a linha, só viramos `CANCELLED`; o motivo entra no
    `AuditLog` junto com quem/quando, já cobertos por
    `AuditLog.user_id`/`created_at`)."""

    reason: str = Field(min_length=3, max_length=255)


class OrderItemUpdate(BaseModel):
    """Campos editáveis de uma linha de SERVIÇO da comanda — `price`
    (já existia) e `duration_minutes` (Etapa C — item "edição auditada
    de valor/duração"). Nunca reescreve `AppointmentItem.price`/
    `.duration_minutes` nem `Service.default_price`/`.duration_minutes`
    (ver `services/orders.py`) — a edição fica inteiramente contida
    neste snapshot da comanda, então nunca reescreve silenciosamente
    nenhum histórico consolidado que leia do Appointment/catálogo
    original. Os dois campos são opcionais, mas pelo menos um precisa
    vir preenchido (payload `{}` não faz sentido); cada campo alterado
    gera sua PRÓPRIA linha de auditoria (ver
    `services/orders.py::update_order_item`)."""

    price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)

    @model_validator(mode="after")
    def _check_at_least_one_field(self) -> "OrderItemUpdate":
        if self.price is None and self.duration_minutes is None:
            raise ValueError("Informe price e/ou duration_minutes para editar.")
        return self


# Nome anterior, mantido como alias só pra não quebrar nenhum import
# direto da classe que já exista fora deste módulo — o contrato HTTP
# (`PATCH /orders/{id}/items/{item_id}` aceitando `{"price": ...}`)
# nunca mudou, `duration_minutes` é aditivo.
OrderItemPriceUpdate = OrderItemUpdate


class PaymentCreate(BaseModel):
    method: PaymentMethod
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    # Obrigatório sempre (item "Caixa Diário") — pagamento nunca é
    # criado sem um caixa aberto explicitamente selecionado. Validado
    # no service (`services/cash_register.py::assert_register_open_and_in_org`),
    # não só aqui: o schema só garante que o campo veio, não que aquele
    # caixa específico existe/está aberto.
    cash_register_id: uuid.UUID
    # Obrigatório só pra débito/crédito (bandeira) — validado abaixo,
    # mesmo padrão de consistência condicional do `ScheduleBlockCreate`.
    card_brand: CardBrand | None = None
    # Preparado pro futuro (parcelas de crédito) — só aceito quando
    # method=credit, nunca usado pra calcular nada nesta versão.
    installments: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_card_brand(self) -> "PaymentCreate":
        needs_brand = self.method in _CARD_METHODS
        if needs_brand and self.card_brand is None:
            raise ValueError("card_brand é obrigatório para pagamento em débito ou crédito.")
        if not needs_brand and self.card_brand is not None:
            raise ValueError("card_brand só é aceito para pagamento em débito ou crédito.")
        return self

    @model_validator(mode="after")
    def _check_installments(self) -> "PaymentCreate":
        if self.installments is not None and self.method != PaymentMethod.CREDIT:
            raise ValueError("installments só é aceito para pagamento em crédito.")
        return self


class OrderProductItemCreate(BaseModel):
    """`unit_price` NUNCA vem do payload — é sempre resolvido a partir
    de `Product.sale_price` no momento da criação (ver
    `services/orders.py::add_product_item`), exatamente como
    `OrderItem.price` nasce igual ao `AppointmentItem.price`. Editar o
    valor depois é uma ação separada e auditada
    (`OrderProductItemUpdate`)."""

    product_id: uuid.UUID
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)


class OrderProductItemUpdate(BaseModel):
    """`quantity`/`unit_price` editáveis (auditado), mesmo espírito de
    `OrderItemUpdate` — pelo menos um precisa vir preenchido."""

    quantity: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=3)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def _check_at_least_one_field(self) -> "OrderProductItemUpdate":
        if self.quantity is None and self.unit_price is None:
            raise ValueError("Informe quantity e/ou unit_price para editar.")
        return self


class OrderProductItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    # Preenchido só depois do fechamento — ver docstring de
    # `models/order.py::OrderProductItem`. Nunca inclui custo (o custo
    # do produto mora só em `Product.cost_price`/`StockMovement.unit_cost`,
    # já protegidos por `inventory.view_cost` — esta linha nunca duplica
    # nem vaza esse dado por um caminho novo).
    stock_movement_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class OrderClose(BaseModel):
    """`payments` é uma LISTA — mesmo com a UI desta primeira versão só
    criando um lançamento por fechamento, o domínio já suporta
    pagamento misto (ex.: parte Pix + parte Crédito) sem precisar de
    outra migration depois."""

    payments: list[PaymentCreate] = Field(min_length=1)


class ConsolidatedOrderClose(BaseModel):
    """`POST /orders/{order_id}/close-consolidated` (Etapa I, item
    "Fechamento consolidado") — `order_ids` é a lista COMPLETA das
    comandas a fechar juntas (a da URL pode ou não estar incluída
    explicitamente; o service garante que ela participa). `payments` é
    uma lista ÚNICA que cobre o TOTAL somado de todas as comandas — o
    pagamento pode ser dividido normalmente entre Pix/cartão/dinheiro
    etc.; o service distribui cada valor entre as comandas (ver
    `services/orders.py::close_orders_consolidated`)."""

    order_ids: list[uuid.UUID] = Field(min_length=1)
    payments: list[PaymentCreate] = Field(min_length=1)


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    appointment_item_id: uuid.UUID | None
    service_id: uuid.UUID
    professional_id: uuid.UUID
    duration_minutes: int
    price: Decimal
    service_name: str
    professional_name: str
    created_at: datetime
    updated_at: datetime


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    cash_register_id: uuid.UUID
    method: PaymentMethod
    card_brand: CardBrand | None
    installments: int | None
    amount: Decimal
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime


class OrderRead(BaseModel):
    """`subtotal`/`total` são CALCULADOS (soma de `items[].price` +
    `product_items[].quantity * .unit_price`), nunca colunas persistidas
    — evita a comanda ficar com um total dessincronizado depois de uma
    edição de preço/quantidade. Hoje `total == subtotal` (sem desconto:
    nenhuma estrutura de desconto existia no domínio antes desta
    rodada, e criar uma do zero ficou fora do escopo pedido — "sem
    inventar Financeiro completo"). Os dois campos já existem separados
    pra não quebrar o schema quando desconto for modelado depois.

    Etapa C: `items` (serviço) e `product_items` (produto) são DUAS
    listas separadas — item explícito "separar claramente item de
    serviço e item de produto", nunca uma lista polimórfica única. O
    total soma as duas UMA vez cada (nunca duas contas de faturamento
    diferentes — ver `close_order`)."""

    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    organization_id: uuid.UUID
    order_number: int
    appointment_id: uuid.UUID
    branch_id: uuid.UUID
    client_id: uuid.UUID
    status: OrderStatus
    subtotal: Decimal
    total: Decimal
    items: list[OrderItemRead]
    product_items: list[OrderProductItemRead]
    payments: list[PaymentRead]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    created_by: uuid.UUID | None
    closed_by: uuid.UUID | None

    @classmethod
    def from_order(cls, order: Order) -> "OrderRead":
        services_total = sum((item.price for item in order.items), Decimal("0"))
        products_total = sum(
            (item.quantity * item.unit_price for item in order.product_items), Decimal("0")
        )
        subtotal = services_total + products_total
        return cls(
            id=order.id,
            organization_id=order.organization_id,
            order_number=order.order_number,
            appointment_id=order.appointment_id,
            branch_id=order.branch_id,
            client_id=order.client_id,
            status=order.status,
            subtotal=subtotal,
            total=subtotal,
            items=[OrderItemRead.model_validate(item) for item in order.items],
            product_items=[OrderProductItemRead.model_validate(item) for item in order.product_items],
            payments=[PaymentRead.model_validate(payment) for payment in order.payments],
            created_at=order.created_at,
            updated_at=order.updated_at,
            closed_at=order.closed_at,
            created_by=order.created_by,
            closed_by=order.closed_by,
        )


class ClientOrderPaymentSummary(BaseModel):
    """Um lançamento de pagamento dentro de `ClientOrderSummary` —
    `amount` vem `None` quando o ator não tem `finance.view` (ver
    docstring de `ClientOrderSummary`)."""

    model_config = ConfigDict(from_attributes=False)

    method: PaymentMethod
    amount: Decimal | None


class ClientOrderSummary(BaseModel):
    """Linha da aba "Comandas" da Ficha 360° do cliente (Etapa J) —
    versão enxuta de `OrderRead`, sem os IDs internos que a ficha não
    precisa. `total`/`payments[].amount` vêm `None` quando o ator não
    tem `finance.view` — mesmo padrão de `ProductRead`/
    `ProductReadWithCost` (`schemas/product.py`): o backend decide,
    nunca o frontend só esconde visualmente (ver
    `services/clients.py::get_client_profile`)."""

    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    order_number: int
    status: OrderStatus
    service_names: list[str]
    product_names: list[str]
    total: Decimal | None
    payments: list[ClientOrderPaymentSummary]
    created_at: datetime
    closed_at: datetime | None

    @classmethod
    def from_order(cls, order: Order, *, can_view_finance: bool) -> "ClientOrderSummary":
        # `dict.fromkeys` preserva a ordem de primeira aparição e
        # deduplica — mesmo espírito do `[...new Set(...)]` já usado no
        # frontend (`client-detail-drawer.tsx::HistoricoTab`).
        service_names = list(dict.fromkeys(item.service_name for item in order.items))
        product_names = list(dict.fromkeys(item.product_name for item in order.product_items))
        total = None
        if can_view_finance:
            services_total = sum((item.price for item in order.items), Decimal("0"))
            products_total = sum(
                (item.quantity * item.unit_price for item in order.product_items), Decimal("0")
            )
            total = services_total + products_total
        payments = [
            ClientOrderPaymentSummary(method=p.method, amount=p.amount if can_view_finance else None)
            for p in order.payments
        ]
        return cls(
            id=order.id,
            order_number=order.order_number,
            status=order.status,
            service_names=service_names,
            product_names=product_names,
            total=total,
            payments=payments,
            created_at=order.created_at,
            closed_at=order.closed_at,
        )


class OrderRelatedRead(BaseModel):
    """`GET /orders/{order_id}/related` (Etapa I, item "Comandas
    relacionadas") — `related` NUNCA inclui a própria comanda
    consultada; `total_today` = `len(related) + 1`, pronto pro texto
    discreto "Esta cliente possui N comandas hoje" no frontend."""

    model_config = ConfigDict(from_attributes=False)

    client_name: str
    total_today: int
    related: list[OrderRead]


# --- Comprovante de Atendimento (Etapa D) -----------------------------
#
# NÃO é nota fiscal / NFS-e — item explícito do pedido ("nunca chamar
# de NF"). É um documento não fiscal, construído inteiramente a partir
# dos SNAPSHOTS já existentes de `OrderItem`/`OrderProductItem`/
# `Payment` (preço/nome/profissional/forma de pagamento no momento da
# venda) — NUNCA consulta o catálogo atual (`Service.default_price`/
# `Product.sale_price`) pra reconstruir uma venda antiga. Se o catálogo
# mudar amanhã, um comprovante de uma comanda fechada hoje continua
# mostrando exatamente os valores que ela tinha no fechamento.
#
# Deliberadamente NÃO inclui: `Client.notes` (observações internas do
# estabelecimento — nunca aparecem no comprovante), custo de estoque
# (`Product.cost_price`/`StockMovement.unit_cost`), `created_by`/
# `created_by_name` de pagamento nem qualquer outro dado administrativo
# interno — só o que o cliente final pode legitimamente ver.
#
# Nenhuma permission nova: reusa `orders.view` (mesma permissão que já
# controla ver a Comanda) tanto pra visualizar quanto pra
# emitir/imprimir o comprovante — item do pedido "respeitar as
# permissões existentes", sem inventar uma chave nova sem necessidade
# comprovada (mesmo raciocínio já aplicado à CPF/unicidade na Etapa C.1).


class ReceiptClient(BaseModel):
    name: str
    phone: str | None
    email: str | None


class ReceiptEstablishment(BaseModel):
    """Todos os campos opcionais — "quando disponível" (item explícito
    do pedido). Um estabelecimento sem CNPJ/logo/endereço ainda emite
    comprovante normalmente, só com esses campos ausentes."""

    name: str
    legal_name: str | None
    document: str | None
    logo_url: str | None
    phone: str | None
    whatsapp: str | None
    email: str | None
    cep: str | None
    state: str | None
    city: str | None
    neighborhood: str | None
    address_line: str | None
    address_number: str | None
    complement: str | None


class ReceiptItem(BaseModel):
    kind: str  # "service" | "product"
    name: str
    professional_name: str | None  # só serviços
    quantity: Decimal
    unit_price: Decimal
    total: Decimal


class ReceiptPayment(BaseModel):
    method: PaymentMethod
    card_brand: CardBrand | None
    installments: int | None
    amount: Decimal


class OrderReceiptRead(BaseModel):
    """Ver docstring da seção acima — construído 100% de snapshots já
    persistidos, nunca do catálogo vivo."""

    model_config = ConfigDict(from_attributes=False)

    order_number: int
    closed_at: datetime | None
    client: ReceiptClient
    establishment: ReceiptEstablishment
    items: list[ReceiptItem]
    payments: list[ReceiptPayment]
    subtotal: Decimal
    total: Decimal

    @classmethod
    def build(cls, order: Order, client: Client, organization: Organization) -> "OrderReceiptRead":
        service_items = [
            ReceiptItem(
                kind="service",
                name=item.service_name,
                professional_name=item.professional_name,
                quantity=Decimal("1"),
                unit_price=item.price,
                total=item.price,
            )
            for item in order.items
        ]
        product_items = [
            ReceiptItem(
                kind="product",
                name=item.product_name,
                professional_name=None,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total=item.quantity * item.unit_price,
            )
            for item in order.product_items
        ]
        items = service_items + product_items
        subtotal = sum((i.total for i in items), Decimal("0"))
        return cls(
            order_number=order.order_number,
            closed_at=order.closed_at,
            client=ReceiptClient(name=client.name, phone=client.phone, email=client.email),
            establishment=ReceiptEstablishment(
                name=organization.name,
                legal_name=organization.legal_name,
                document=organization.document,
                logo_url=organization.logo_url,
                phone=organization.phone,
                whatsapp=organization.whatsapp,
                email=organization.email,
                cep=organization.cep,
                state=organization.state.value if organization.state else None,
                city=organization.city,
                neighborhood=organization.neighborhood,
                address_line=organization.address_line,
                address_number=organization.address_number,
                complement=organization.complement,
            ),
            items=items,
            payments=[
                ReceiptPayment(
                    method=p.method, card_brand=p.card_brand, installments=p.installments, amount=p.amount
                )
                for p in order.payments
            ],
            subtotal=subtotal,
            total=subtotal,
        )
