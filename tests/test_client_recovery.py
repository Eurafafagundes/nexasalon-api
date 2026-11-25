"""Testes da Central de Recuperação de Clientes (`services/client_recovery.py`).

Direto no service layer via `SessionLocal`, construindo Appointment/Order/
OrderItem diretamente pelo ORM — MESMO motivo de `test_dashboard.py`:
estes testes precisam de "dias sem retorno" controlados (`Order.closed_at`
no passado), e `close_order` real sempre usa `datetime.now()`."""
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, ValidationDomainError
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.cash_register import CashRegister
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    BenefitType,
    CashRegisterStatus,
    OrderStatus,
    RecoveryContactChannel,
    RecoveryContactResult,
    RecoveryCycleStatus,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.order import Order, OrderItem
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import Service
from nexasalon_api.repositories import client_recovery_repo
from nexasalon_api.schemas.order import OrderClose, PaymentCreate
from nexasalon_api.services import client_recovery as recovery_service
from nexasalon_api.services import orders as orders_service

_NOW = datetime.now(timezone.utc)


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org recuperação", slug=f"org-rec-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=frozenset({"clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team"})) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@example.com", name="Recepcionista Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="RECEPTIONIST", permissions=permissions,
    )


def _branch(session, org_id) -> Branch:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _client(session, org_id, name="Cliente") -> Client:
    c = Client(organization_id=org_id, name=name, phone="61999990000")
    session.add(c)
    session.flush()
    return c


def _professional(session, org_id, branch_id, name="Profissional") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Serviço", price=Decimal("100.00")) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=60, default_price=price)
    session.add(s)
    session.flush()
    return s


_order_number_counter = 0


def _next_order_number() -> int:
    global _order_number_counter
    _order_number_counter += 1
    return _order_number_counter


def _appointment(
    session, org_id, branch_id, client_id, professional_id, service_id, *,
    start_at: datetime, status=AppointmentStatus.SCHEDULED, price=Decimal("100.00"),
) -> Appointment:
    appt = Appointment(organization_id=org_id, branch_id=branch_id, client_id=client_id, status=status)
    session.add(appt)
    session.flush()
    item = AppointmentItem(
        organization_id=org_id, appointment_id=appt.id, service_id=service_id, professional_id=professional_id,
        start_at=start_at, end_at=start_at + timedelta(minutes=60), duration_minutes=60, price=price,
    )
    session.add(item)
    session.flush()
    session.refresh(appt)
    return appt


def _closed_order(
    session, org_id, branch_id, client_id, appointment_id, *, closed_at: datetime, items: list[dict],
) -> Order:
    order = Order(
        organization_id=org_id, order_number=_next_order_number(), appointment_id=appointment_id,
        branch_id=branch_id, client_id=client_id, status=OrderStatus.CLOSED, closed_at=closed_at,
        created_at=closed_at,
    )
    session.add(order)
    session.flush()
    for it in items:
        session.add(
            OrderItem(
                organization_id=org_id, order_id=order.id, service_id=it["service_id"],
                professional_id=it["professional_id"], duration_minutes=60, price=it["price"],
                service_name=it.get("service_name", "Serviço"), professional_name=it.get("professional_name", "Profissional"),
                benefit_type=it.get("benefit_type"), benefit_amount=it.get("benefit_amount"),
            )
        )
    session.flush()
    return order


def _visit(session, org_id, branch_id, client_id, professional_id, service_id, *, closed_at, price=Decimal("100.00"), benefit_amount=None, service_name="Serviço", professional_name="Profissional") -> Order:
    """Atalho: 1 agendamento FINISHED + 1 comanda fechada com 1 item."""
    appt = _appointment(
        session, org_id, branch_id, client_id, professional_id, service_id,
        start_at=closed_at - timedelta(hours=1), status=AppointmentStatus.PAID, price=price,
    )
    return _closed_order(
        session, org_id, branch_id, client_id, appt.id, closed_at=closed_at,
        items=[{
            "service_id": service_id, "professional_id": professional_id, "price": price,
            "service_name": service_name, "professional_name": professional_name,
            "benefit_type": BenefitType.LOYALTY if benefit_amount else None, "benefit_amount": benefit_amount,
        }],
    )


# ---------------------------------------------------------------------------
# Elegibilidade / listagem
# ---------------------------------------------------------------------------


def test_cliente_sem_nenhum_atendimento_nunca_aparece_como_sem_retorno(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _client(session, org_id, "Cliente Novo")

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=0)
    assert result.items == []


def test_cliente_elegivel_aparece_em_pendentes_com_dias_corretos(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id, "Maria")
    closed_at = _NOW - timedelta(days=65)
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=closed_at, price=Decimal("150.00"))

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert len(result.items) == 1
    row = result.items[0]
    assert row.client.id == client.id
    assert row.days_since_last_visit == 65
    assert row.total_spent == Decimal("150.00")
    assert row.cycle is None


def test_cliente_abaixo_do_limiar_de_dias_nao_aparece(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id)
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=10))

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert result.items == []


def test_cliente_com_agendamento_futuro_valido_nunca_aparece_em_pendentes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id)
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=65))
    _appointment(
        session, org_id, branch.id, client.id, prof.id, svc.id,
        start_at=_NOW + timedelta(days=3), status=AppointmentStatus.SCHEDULED,
    )

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert result.items == []


def test_cliente_com_agendamento_futuro_cancelado_continua_elegivel(org_session):
    """Agendamento futuro CANCELLED nunca bloqueia elegibilidade — só
    status operacionais abertos (`UPCOMING_APPOINTMENT_STATUSES`) contam."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id)
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=65))
    _appointment(
        session, org_id, branch.id, client.id, prof.id, svc.id,
        start_at=_NOW + timedelta(days=3), status=AppointmentStatus.CANCELLED,
    )

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert len(result.items) == 1


def test_total_gasto_usa_valor_liquido_de_beneficio_nunca_o_bruto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id)
    # Visita 1: R$200, metade em benefício -> gasto líquido R$100.
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=70), price=Decimal("200.00"), benefit_amount=Decimal("100.00"))
    # Visita 2: R$80 cheio, sem benefício.
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=65), price=Decimal("80.00"))

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert len(result.items) == 1
    assert result.items[0].total_spent == Decimal("180.00")


def test_total_gasto_ignora_comandas_abertas_e_canceladas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id)
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=65), price=Decimal("50.00"))
    # Comanda CANCELLED de outro atendimento — nunca deve somar.
    appt2 = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW - timedelta(days=30), status=AppointmentStatus.CANCELLED)
    order2 = Order(
        organization_id=org_id, order_number=_next_order_number(), appointment_id=appt2.id, branch_id=branch.id,
        client_id=client.id, status=OrderStatus.CANCELLED,
    )
    session.add(order2)
    session.flush()
    session.add(OrderItem(organization_id=org_id, order_id=order2.id, service_id=svc.id, professional_id=prof.id, duration_minutes=60, price=Decimal("999.00"), service_name="X", professional_name="Y"))
    session.flush()

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert result.items[0].total_spent == Decimal("50.00")


def test_ultimo_procedimento_com_multiplos_servicos_lista_todos_os_nomes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof_a = _professional(session, org_id, branch.id, "Ianka")
    prof_b = _professional(session, org_id, branch.id, "Ingrid")
    svc_a = _service(session, org_id, "Corte")
    svc_b = _service(session, org_id, "Escova")
    client = _client(session, org_id)
    closed_at = _NOW - timedelta(days=65)
    appt = _appointment(session, org_id, branch.id, client.id, prof_a.id, svc_a.id, start_at=closed_at - timedelta(hours=2), status=AppointmentStatus.PAID)
    order = _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=closed_at,
        items=[
            {"service_id": svc_a.id, "professional_id": prof_a.id, "price": Decimal(100), "service_name": "Corte", "professional_name": "Ianka"},
            {"service_id": svc_b.id, "professional_id": prof_b.id, "price": Decimal(80), "service_name": "Escova", "professional_name": "Ingrid"},
        ],
    )
    assert order is not None

    result = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    row = result.items[0]
    assert row.last_service_names == ["Corte", "Escova"]
    assert row.last_professional_names == ["Ianka", "Ingrid"]


def test_isolamento_multiempresa_nunca_mistura_clientes(org_session):
    session, org_a = org_session
    actor_a = _actor(session, org_a)
    branch_a = _branch(session, org_a)
    prof_a = _professional(session, org_a, branch_a.id)
    svc_a = _service(session, org_a)
    client_a = _client(session, org_a, "Cliente A")
    _visit(session, org_a, branch_a.id, client_a.id, prof_a.id, svc_a.id, closed_at=_NOW - timedelta(days=65))

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_b)
    branch_b = _branch(session, org_b)
    prof_b = _professional(session, org_b, branch_b.id)
    svc_b = _service(session, org_b)
    client_b = _client(session, org_b, "Cliente B")
    _visit(session, org_b, branch_b.id, client_b.id, prof_b.id, svc_b.id, closed_at=_NOW - timedelta(days=65))

    # volta pro contexto de RLS da org A antes de consultar — mesma
    # mecânica de `get_db` numa request real (ver test_dashboard.py).
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
    result_a = recovery_service.list_recovery_queue(session, actor_a, subtab="pending", min_days=60)
    assert [r.client.id for r in result_a.items] == [client_a.id]

    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    result_b = recovery_service.list_recovery_queue(session, actor_b, subtab="pending", min_days=60)
    assert [r.client.id for r in result_b.items] == [client_b.id]


# ---------------------------------------------------------------------------
# Registrar contato / ciclo / transições
# ---------------------------------------------------------------------------


def _eligible_client(session, org_id, days=65):
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    client = _client(session, org_id)
    _visit(session, org_id, branch.id, client.id, prof.id, svc.id, closed_at=_NOW - timedelta(days=days))
    return client, branch, prof, svc


def test_registrar_contato_cria_ciclo_na_primeira_acao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    assert client_recovery_repo.get_active_for_client(session, org_id, client.id) is None
    cycle = recovery_service.log_contact(
        session, actor, client.id,
        contact_channel=RecoveryContactChannel.WHATSAPP, contact_result=RecoveryContactResult.AWAITING_RESPONSE,
        notes="Chamei no WhatsApp", next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
    )
    assert cycle.status == RecoveryCycleStatus.IN_PROGRESS
    assert cycle.assigned_user_id == actor.user_id
    events = client_recovery_repo.list_events_for_cycle(session, org_id, cycle.id)
    assert [e.event_type.value for e in events] == ["created", "contact_logged"]


def test_registrar_contato_sem_data_quando_resultado_exige_falha(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    with pytest.raises(ValidationDomainError):
        recovery_service.log_contact(
            session, actor, client.id,
            contact_channel=RecoveryContactChannel.CALL, contact_result=RecoveryContactResult.CALL_LATER,
            notes=None, next_contact_at=None, linked_appointment_id=None,
        )


def test_registrar_contato_com_data_no_passado_falha(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    with pytest.raises(ValidationDomainError):
        recovery_service.log_contact(
            session, actor, client.id,
            contact_channel=RecoveryContactChannel.CALL, contact_result=RecoveryContactResult.CALL_LATER,
            notes=None, next_contact_at=_NOW - timedelta(days=1), linked_appointment_id=None,
        )


def test_retorno_agendado_exige_agendamento_real_vinculado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    with pytest.raises(ValidationDomainError):
        recovery_service.log_contact(
            session, actor, client.id,
            contact_channel=RecoveryContactChannel.WHATSAPP, contact_result=RecoveryContactResult.RETURN_SCHEDULED,
            notes=None, next_contact_at=None, linked_appointment_id=None,
        )


def test_retorno_agendado_com_agendamento_valido_vira_scheduled_e_some_de_pendentes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, branch, prof, svc = _eligible_client(session, org_id)
    future_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=5))

    cycle = recovery_service.log_contact(
        session, actor, client.id,
        contact_channel=RecoveryContactChannel.WHATSAPP, contact_result=RecoveryContactResult.RETURN_SCHEDULED,
        notes=None, next_contact_at=None, linked_appointment_id=future_appt.id,
    )
    assert cycle.status == RecoveryCycleStatus.SCHEDULED
    assert cycle.linked_appointment_id == future_appt.id

    # Agendado mas ainda não realizado = Acompanhamento (item V3: a ação
    # "Ver agendamento" vive na aba Acompanhamento, nunca Concluídos —
    # só é "concluído" quando o ciclo de fato se encerra).
    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert pending.items == []
    in_progress = recovery_service.list_recovery_queue(session, actor, subtab="in_progress")
    assert [r.client.id for r in in_progress.items] == [client.id]
    concluded = recovery_service.list_recovery_queue(session, actor, subtab="concluded")
    assert concluded.items == []


def test_nao_permite_dois_ciclos_ativos_para_o_mesmo_cliente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes=None,
        next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
    )
    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes=None,
        next_contact_at=_NOW + timedelta(days=5), linked_appointment_id=None,
    )
    cycles = client_recovery_repo.list_all_for_clients(session, org_id, [client.id])
    assert len(cycles) == 1


def test_concorrencia_real_entre_duas_recepcionistas_criando_o_primeiro_ciclo(org_session):
    """Corrida real com DUAS conexões/threads de verdade (não só duas
    chamadas sequenciais na mesma sessão, que nunca exercitam o
    `IntegrityError`) — auditoria pós-V3: `get_active_for_client_for_
    update` com `FOR UPDATE` não trava nada quando não existe NENHUMA
    linha pra travar, então duas recepcionistas registrando o PRIMEIRO
    contato do MESMO cliente ao mesmo tempo passam as duas por
    `existing is None`. A thread B é deliberadamente forçada a tentar o
    próprio INSERT enquanto a thread A já deu `flush` mas ainda não
    comitou — o Postgres bloqueia o INSERT de B no índice único parcial
    até A comitar, e só então B descobre a violação de verdade. Prova
    que o tratamento em `log_contact` funciona sob concorrência real
    (nunca 2 ciclos, nunca uma excessão não tratada, nunca perde o
    contato de quem "perde" a corrida)."""
    session, org_id = org_session
    client, *_ = _eligible_client(session, org_id)
    # Captura o id ANTES do commit (nunca depois): `commit()` expira os
    # atributos do objeto, e um reacesso posterior obrigaria a sessão a
    # reconectar/reler a linha — sob pool de conexões real (suíte
    # completa, muita concorrência), essa releitura pode cair numa
    # conexão física diferente, sem o `app.current_org_id` da RLS
    # setado, e o SELECT simplesmente não encontra a linha
    # (`ObjectDeletedError`, nunca um delete de verdade). Cada worker
    # usa só este id puro — nunca o objeto ORM, que não é thread-safe.
    client_id = client.id
    session.commit()  # visível pras outras conexões/threads (MVCC)

    results: dict[str, object] = {}
    errors: dict[str, Exception] = {}
    a_flushed = threading.Event()
    a_may_commit = threading.Event()

    def worker_a() -> None:
        try:
            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
                actor_a = _actor(s, org_id)
                cycle = recovery_service.log_contact(
                    s, actor_a, client_id, contact_channel=None,
                    contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes="recepcionista A",
                    next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
                )
                results["a_cycle_id"] = cycle.id
                a_flushed.set()
                a_may_commit.wait(timeout=10)
                s.commit()
        except Exception as exc:  # pragma: no cover - diagnóstico se travar/falhar
            errors["a"] = exc
            a_flushed.set()

    def worker_b() -> None:
        try:
            assert a_flushed.wait(timeout=10), "thread A nunca sinalizou o flush — timeout"
            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
                actor_b = _actor(s, org_id)
                # Libera o commit da A só depois de dar tempo da B já
                # estar bloqueada tentando o próprio INSERT no lock da
                # linha (ainda não comitada) da A.
                threading.Timer(0.3, a_may_commit.set).start()
                cycle = recovery_service.log_contact(
                    s, actor_b, client_id, contact_channel=None,
                    contact_result=RecoveryContactResult.NOT_INTERESTED, notes="recepcionista B",
                    next_contact_at=None, linked_appointment_id=None,
                )
                results["b_cycle_id"] = cycle.id
                s.commit()
        except Exception as exc:
            errors["b"] = exc
            a_may_commit.set()

    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=20)
    t_b.join(timeout=20)

    assert not errors, f"erro inesperado numa das threads (nunca deveria propagar): {errors}"
    assert results.get("a_cycle_id") is not None
    assert results.get("b_cycle_id") is not None
    assert results["a_cycle_id"] == results["b_cycle_id"]  # MESMO ciclo, nunca duplicado

    # Verificação final numa sessão NOVA (nunca reaproveitando `session`
    # depois do commit acima) — garante o contexto de RLS correto
    # independentemente de qual conexão física o pool devolveria.
    with SessionLocal() as verify_session:
        verify_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        cycles = client_recovery_repo.list_all_for_clients(verify_session, org_id, [client_id])
        assert len(cycles) == 1
        events = client_recovery_repo.list_events_for_cycle(verify_session, org_id, cycles[0].id)
    # As duas recepcionistas tiveram o próprio contato registrado de
    # verdade — quem "perdeu" a corrida nunca foi descartada em
    # silêncio nem precisou tentar de novo manualmente.
    notes_recorded = {e.notes for e in events if e.notes}
    assert notes_recorded == {"recepcionista A", "recepcionista B"}


def test_sem_interesse_aplica_pausa_e_bloqueia_reentrada_ate_o_prazo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.NOT_INTERESTED, notes=None,
        next_contact_at=None, linked_appointment_id=None,
    )
    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert pending.items == []

    cycles = client_recovery_repo.list_all_for_clients(session, org_id, [client.id])
    assert cycles[0].suppressed_until > _NOW + timedelta(days=59)  # carência atual: 60 dias


def test_nao_deseja_contato_bloqueia_whatsapp_e_reentrada_permanentemente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.OPTED_OUT, notes=None,
        next_contact_at=None, linked_appointment_id=None,
    )
    session.refresh(client)
    assert client.commercial_contact_opt_out_at is not None
    assert recovery_service.build_whatsapp_href_for_client(session, actor, client) is None

    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert pending.items == []


def test_agendamento_espontaneo_nunca_cria_ciclo_nem_credito(org_session):
    """Cliente agenda sozinho (sem passar por 'Agendar retorno') — some
    de Pendentes pelo FILTRO de elegibilidade, mas nenhum ciclo nasce."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, branch, prof, svc = _eligible_client(session, org_id)
    _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=2))

    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert pending.items == []
    assert client_recovery_repo.list_all_for_clients(session, org_id, [client.id]) == []


# ---------------------------------------------------------------------------
# Reentrada automática por cancelamento/falta do retorno vinculado
# ---------------------------------------------------------------------------


def test_cancelar_agendamento_de_retorno_vinculado_reabre_o_ciclo_automaticamente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, branch, prof, svc = _eligible_client(session, org_id)
    future_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=5))

    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )
    assert cycle.status == RecoveryCycleStatus.SCHEDULED

    recovery_service.reopen_cycle_if_linked_appointment_cancelled(session, org_id, future_appt.id)

    refreshed = client_recovery_repo.get(session, org_id, cycle.id)
    assert refreshed.status == RecoveryCycleStatus.PENDING
    assert refreshed.linked_appointment_id is None

    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert [r.client.id for r in pending.items] == [client.id]


def test_cancelar_retorno_com_outro_agendamento_futuro_mantem_ciclo_e_atualiza_vinculo(org_session):
    """Lacuna corrigida nesta rodada: se o cliente já tem OUTRO
    agendamento futuro válido (ex.: a recepção remarcou antes de
    cancelar o antigo), o ciclo nunca deve voltar pra Pendentes — só
    atualiza o vínculo pro agendamento novo, permanece Acompanhamento."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, branch, prof, svc = _eligible_client(session, org_id)
    old_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=5))
    other_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=10))

    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=old_appt.id,
    )
    assert cycle.status == RecoveryCycleStatus.SCHEDULED

    recovery_service.reopen_cycle_if_linked_appointment_cancelled(session, org_id, old_appt.id)

    refreshed = client_recovery_repo.get(session, org_id, cycle.id)
    assert refreshed.status == RecoveryCycleStatus.SCHEDULED  # nunca volta a pendente
    assert refreshed.linked_appointment_id == other_appt.id  # vínculo atualizado, nunca ciclo novo

    cycles = client_recovery_repo.list_all_for_clients(session, org_id, [client.id])
    assert len(cycles) == 1  # nunca cria ciclo duplicado

    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert pending.items == []
    in_progress = recovery_service.list_recovery_queue(session, actor, subtab="in_progress")
    assert [r.client.id for r in in_progress.items] == [client.id]


def test_reentrada_automatica_esta_realmente_conectada_ao_cancelamento_real(org_session):
    """Integração real (não só a função isolada): `services/appointments.py
    ::cancel_appointment` precisa disparar o hook de verdade."""
    from nexasalon_api.services import appointments as appointments_service

    session, org_id = org_session
    actor = _actor(session, org_id, permissions=frozenset({
        "clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team",
        "agenda.edit", "agenda.view_all",
    }))
    client, branch, prof, svc = _eligible_client(session, org_id)
    future_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=5))

    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )

    appointments_service.cancel_appointment(session, actor, future_appt.id)

    cycles = client_recovery_repo.list_all_for_clients(session, org_id, [client.id])
    assert len(cycles) == 1
    assert cycles[0].status == RecoveryCycleStatus.PENDING


def test_retorno_atendido_vira_recuperado_quando_comanda_fecha(org_session):
    """Integração real com `services/orders.py::close_order`."""
    session, org_id = org_session
    actor = _actor(session, org_id, permissions=frozenset({
        "clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team",
        "orders.manage", "finance.manage",
    }))
    client, branch, prof, svc = _eligible_client(session, org_id)
    future_appt = _appointment(
        session, org_id, branch.id, client.id, prof.id, svc.id,
        start_at=_NOW + timedelta(hours=1), price=Decimal("90.00"),
    )

    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )

    cash_register = CashRegister(
        organization_id=org_id, branch_id=branch.id, opened_by=actor.user_id, opened_by_name="Teste",
        initial_amount=Decimal(0), status=CashRegisterStatus.OPEN,
    )
    session.add(cash_register)
    session.flush()

    order = orders_service.create_order(session, actor, future_appt.id)
    orders_service.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method="pix", amount=Decimal("90.00"), cash_register_id=cash_register.id)]),
    )

    refreshed = client_recovery_repo.get(session, org_id, cycle.id)
    assert refreshed.status == RecoveryCycleStatus.RECOVERED
    assert refreshed.closed_at is not None


# ---------------------------------------------------------------------------
# Fluxos V3: "Aceitou retornar" sem concluir agendamento, no-show,
# reagendamento, canal opcional
# ---------------------------------------------------------------------------


def test_aceitou_retornar_sem_concluir_agendamento_preserva_intencao(org_session):
    """"Aceitou retornar" só registra `interested` (`in_progress`, sem
    `next_contact_at`) — se o usuário desistir de criar o agendamento
    (nunca chama `link_return_appointment`), o ciclo PERMANECE assim:
    nunca vira `scheduled`/`recovered` sozinho, nunca é perdido."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=None,
        contact_result=RecoveryContactResult.INTERESTED, notes="Quer voltar, vai escolher o dia depois.",
        next_contact_at=None, linked_appointment_id=None,
    )
    assert cycle.status == RecoveryCycleStatus.IN_PROGRESS
    assert cycle.next_contact_at is None

    in_progress = recovery_service.list_recovery_queue(session, actor, subtab="in_progress")
    assert [r.client.id for r in in_progress.items] == [client.id]
    row = in_progress.items[0]
    # Distinção que o frontend usa pra escolher "Agendar retorno" (aceitou,
    # sem data) vs. "Novo contato" (aguardando resposta, com data).
    assert row.cycle.next_contact_at is None

    refreshed = client_recovery_repo.get(session, org_id, cycle.id)
    assert refreshed.status == RecoveryCycleStatus.IN_PROGRESS  # nunca promovido sozinho


def test_no_show_do_retorno_vinculado_reabre_sem_resetar_ultima_visita(org_session):
    """Falta no retorno agendado: reabre o ciclo (mesma lógica de
    cancelamento) mas NUNCA mexe em `last_visit_at`/dias sem retorno —
    esses continuam derivados da última comanda FECHADA de verdade."""
    from nexasalon_api.services import appointments as appointments_service

    session, org_id = org_session
    actor = _actor(session, org_id, permissions=frozenset({
        "clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team",
        "agenda.edit", "agenda.view_all",
    }))
    client, branch, prof, svc = _eligible_client(session, org_id, days=65)
    future_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(hours=1))

    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )

    appointments_service.update_status(session, actor, future_appt.id, AppointmentStatus.NO_SHOW)

    cycles = client_recovery_repo.list_all_for_clients(session, org_id, [client.id])
    assert len(cycles) == 1
    assert cycles[0].status == RecoveryCycleStatus.PENDING

    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert len(pending.items) == 1
    # A falta no retorno NUNCA conta como visita real — dias/última visita
    # continuam vindo da comanda fechada ORIGINAL (`_eligible_client`).
    assert pending.items[0].days_since_last_visit == 65


def test_reagendamento_preserva_o_mesmo_ciclo(org_session):
    """Reagendar (mudar horário/profissional do MESMO agendamento, via
    `update_appointment_item` — nunca cancela+recria) nunca aciona o
    hook de reentrada nem duplica/perde o ciclo vinculado."""
    from nexasalon_api.schemas.appointment import AppointmentItemUpdate
    from nexasalon_api.services import appointments as appointments_service

    session, org_id = org_session
    actor = _actor(session, org_id, permissions=frozenset({
        "clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team",
        "agenda.edit", "agenda.view_all",
    }))
    client, branch, prof, svc = _eligible_client(session, org_id)
    future_appt = _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_NOW + timedelta(days=5))

    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )
    item_id = future_appt.items[0].id

    appointments_service.update_appointment_item(
        session, actor, future_appt.id, item_id,
        AppointmentItemUpdate(start_at=_NOW + timedelta(days=8), force_overlap=True),
    )

    refreshed = client_recovery_repo.get(session, org_id, cycle.id)
    assert refreshed.status == RecoveryCycleStatus.SCHEDULED
    assert refreshed.linked_appointment_id == future_appt.id  # mesmo agendamento, mesmo ciclo
    assert refreshed.version == cycle.version  # hook de reentrada nunca disparou


def test_canal_de_contato_omitido_nunca_inventa_valor(org_session):
    """A UI simplificada de 3 cartões nunca envia `contact_channel` —
    confirma que o service aceita `None` sem erro e nunca escreve um
    canal "chutado" no evento/histórico."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)

    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=None,
        contact_result=RecoveryContactResult.NOT_INTERESTED, notes=None,
        next_contact_at=None, linked_appointment_id=None,
    )
    events = client_recovery_repo.list_events_for_cycle(session, org_id, cycle.id)
    contact_logged = next(e for e in events if e.event_type.value == "contact_logged")
    assert contact_logged.contact_channel is None


def test_mudanca_de_filtro_de_dias_nunca_altera_ou_fecha_ciclos(org_session):
    """Trocar o filtro de "sem retorno há pelo menos" é só um corte de
    leitura — nunca deve mutar/fechar o ciclo nem seu `version`."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id, days=65)
    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=None,
        contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes=None,
        next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
    )
    version_before = cycle.version

    for min_days in (30, 60, 90, 120, 0):
        recovery_service.list_recovery_queue(session, actor, subtab="in_progress", min_days=min_days)

    refreshed = client_recovery_repo.get(session, org_id, cycle.id)
    assert refreshed.version == version_before
    assert refreshed.status == RecoveryCycleStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Concorrência / RBAC
# ---------------------------------------------------------------------------


def test_reatribuicao_com_versao_divergente_da_409(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)
    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes=None,
        next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
    )

    with pytest.raises(ConflictError):
        recovery_service.reassign_cycle(session, actor, client.id, new_assigned_user_id=None, expected_version=99)


def test_sem_view_team_so_enxerga_ciclos_atribuidos_a_si_ou_sem_responsavel(org_session):
    session, org_id = org_session
    owner = _actor(session, org_id)
    restricted = _actor(session, org_id, permissions=frozenset({"clients.recovery.view", "clients.recovery.manage"}))

    client_a, *_ = _eligible_client(session, org_id)
    client_b, *_ = _eligible_client(session, org_id)

    recovery_service.log_contact(
        session, owner, client_a.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes=None,
        next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
    )
    recovery_service.log_contact(
        session, restricted, client_b.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.AWAITING_RESPONSE, notes=None,
        next_contact_at=_NOW + timedelta(days=3), linked_appointment_id=None,
    )

    visible = recovery_service.list_recovery_queue(session, restricted, subtab="in_progress")
    assert [r.client.id for r in visible.items] == [client_b.id]


def test_acompanhamento_vencido_volta_para_pendentes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    client, *_ = _eligible_client(session, org_id)
    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.CALL_LATER, notes=None,
        next_contact_at=_NOW + timedelta(hours=1), linked_appointment_id=None,
    )
    # Força o vencimento de fato (o service valida "não no passado" na
    # criação; simula a passagem do tempo direto no banco).
    cycle = client_recovery_repo.get_active_for_client(session, org_id, client.id)
    cycle.next_contact_at = _NOW - timedelta(hours=1)
    session.flush()

    in_progress = recovery_service.list_recovery_queue(session, actor, subtab="in_progress")
    assert in_progress.items == []
    pending = recovery_service.list_recovery_queue(session, actor, subtab="pending", min_days=60)
    assert len(pending.items) == 1
    assert pending.items[0].is_overdue is True


# ---------------------------------------------------------------------------
# Indicadores históricos (Recuperados/Sem interesse/Concluídos)
# ---------------------------------------------------------------------------


def _recover_with_order(session, actor, org_id, *, price=Decimal("120.00"), benefit=None, days_ago_closed=0):
    """Fluxo completo e real (via service layer, não ORM solto):
    cliente elegível -> agendamento de retorno -> `log_contact`
    (RETURN_SCHEDULED) -> comanda criada/fechada -> hook vira o ciclo
    `recovered`. Devolve `(client, cycle, order)`."""
    client, branch, prof, svc = _eligible_client(session, org_id)
    future_appt = _appointment(
        session, org_id, branch.id, client.id, prof.id, svc.id,
        start_at=_NOW + timedelta(hours=2), price=price,
    )
    cycle = recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )
    cash_register = CashRegister(
        organization_id=org_id, branch_id=branch.id, opened_by=actor.user_id, opened_by_name="Teste",
        initial_amount=Decimal(0), status=CashRegisterStatus.OPEN,
    )
    session.add(cash_register)
    session.flush()
    order = orders_service.create_order(session, actor, future_appt.id)
    # `orders_service.create_order` (serviço real) calcula o próprio
    # `order_number` direto do banco (`max(...)+1`), sem olhar pro
    # contador global `_order_number_counter` deste arquivo de teste —
    # ressincroniza aqui pra uma PRÓXIMA chamada de `_closed_order`/
    # `_eligible_client` (mesmo org/sessão) nunca reutilizar um número
    # que o serviço real já gravou.
    global _order_number_counter
    _order_number_counter = max(_order_number_counter, order.order_number)
    if benefit is not None:
        item = order.items[0]
        item.benefit_type = BenefitType.LOYALTY
        item.benefit_amount = benefit
        session.flush()
    charged = price - (benefit or Decimal(0))
    orders_service.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method="pix", amount=charged, cash_register_id=cash_register.id)] if charged > 0 else []),
    )
    if days_ago_closed:
        order.closed_at = _NOW - timedelta(days=days_ago_closed)
        session.flush()
    cycle = client_recovery_repo.get(session, org_id, cycle.id)
    return client, cycle, order


_RECOVERY_PERMS = frozenset({
    "clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team",
    "orders.manage", "finance.manage", "orders.reopen", "orders.cancel",
})


def test_concluded_count_soma_todos_os_status_terminais_do_periodo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions=_RECOVERY_PERMS)
    _recover_with_order(session, actor, org_id, price=Decimal("50.00"))  # recovered

    client2, *_ = _eligible_client(session, org_id)
    recovery_service.log_contact(
        session, actor, client2.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.NOT_INTERESTED, notes=None,
        next_contact_at=None, linked_appointment_id=None,
    )  # lost

    client3, *_ = _eligible_client(session, org_id)
    recovery_service.log_contact(
        session, actor, client3.id, contact_channel=RecoveryContactChannel.CALL,
        contact_result=RecoveryContactResult.INVALID_PHONE, notes=None,
        next_contact_at=None, linked_appointment_id=None,
    )  # invalid_phone

    summary = recovery_service.get_recovery_summary(
        session, actor, date_from=_NOW - timedelta(days=1), date_to=_NOW + timedelta(days=1),
    )
    assert summary.recovered_count == 1
    assert summary.not_interested_count == 1
    assert summary.concluded_count == 3  # recovered + lost + invalid_phone


def test_escopo_de_equipe_aplica_igual_a_recuperados(org_session):
    """`recovered_count` sempre respeita `view_team`, igual a
    Pendentes/Acompanhamento — nunca uma população diferente lado a lado
    no mesmo conjunto de indicadores."""
    session, org_id = org_session
    owner = _actor(session, org_id, permissions=_RECOVERY_PERMS)
    restricted = _actor(session, org_id, permissions=frozenset({
        "clients.recovery.view", "clients.recovery.manage", "orders.manage", "finance.manage",
    }))
    _recover_with_order(session, owner, org_id, price=Decimal("90.00"))
    _recover_with_order(session, restricted, org_id, price=Decimal("40.00"))

    summary_restricted = recovery_service.get_recovery_summary(
        session, restricted, date_from=_NOW - timedelta(days=1), date_to=_NOW + timedelta(days=1),
    )
    assert summary_restricted.recovered_count == 1


def test_mesmo_cliente_recuperado_2x_no_periodo_conta_1x_no_indicador(org_session):
    """Item explícito do pedido: 2 ciclos `recovered` do MESMO cliente no
    mesmo período contam 1x em `recovered_count` (clientes distintos),
    mas 2x em `concluded_count` (ciclos) — ambos os ciclos continuam
    visíveis no histórico."""
    session, org_id = org_session
    actor = _actor(session, org_id, permissions=_RECOVERY_PERMS)
    client, cycle1, _order1 = _recover_with_order(session, actor, org_id, price=Decimal("90.00"))

    # Cliente é recuperado uma SEGUNDA vez dentro do mesmo período —
    # ciclo NOVO (o primeiro já é terminal/`recovered`), nunca reabre o
    # ciclo anterior.
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    future_appt = _appointment(
        session, org_id, branch.id, client.id, prof.id, svc.id,
        start_at=_NOW + timedelta(hours=3), price=Decimal("90.00"),
    )
    recovery_service.log_contact(
        session, actor, client.id, contact_channel=RecoveryContactChannel.WHATSAPP,
        contact_result=RecoveryContactResult.RETURN_SCHEDULED, notes=None,
        next_contact_at=None, linked_appointment_id=future_appt.id,
    )
    cash_register = CashRegister(
        organization_id=org_id, branch_id=branch.id, opened_by=actor.user_id, opened_by_name="Teste",
        initial_amount=Decimal(0), status=CashRegisterStatus.OPEN,
    )
    session.add(cash_register)
    session.flush()
    order2 = orders_service.create_order(session, actor, future_appt.id)
    orders_service.close_order(
        session, actor, order2.id,
        OrderClose(payments=[PaymentCreate(method="pix", amount=Decimal("90.00"), cash_register_id=cash_register.id)]),
    )

    summary = recovery_service.get_recovery_summary(
        session, actor, date_from=_NOW - timedelta(days=1), date_to=_NOW + timedelta(days=1),
    )
    assert summary.recovered_count == 1  # cliente distinto, uma única vez
    assert summary.concluded_count == 2  # os 2 ciclos continuam contando aqui

    cycles = client_recovery_repo.list_all_for_clients(session, org_id, [client.id])
    assert len(cycles) == 2
    assert {c.status for c in cycles} == {RecoveryCycleStatus.RECOVERED}
    assert cycle1.id in {c.id for c in cycles}  # ciclo antigo nunca é sobrescrito/apagado


def test_mascaramento_financeiro_oculta_total_spent_sem_permissao(org_session):
    """Mesmo contrato de `recovered_revenue` quando existia: o SERVIÇO
    sempre calcula o valor real; é a ROTA quem decide devolver `None`
    sem `finance.view` (nunca o frontend)."""
    from nexasalon_api.api.v1 import client_recovery as recovery_api

    session, org_id = org_session
    actor = _actor(session, org_id, permissions=frozenset({
        "clients.recovery.view", "clients.recovery.manage", "clients.recovery.view_team",
    }))
    _eligible_client(session, org_id)

    result = recovery_api.list_recovery_queue(
        subtab="pending", min_days=60, max_days=None, page=1, page_size=50, session=session, actor=actor,
    )
    assert result.items[0].total_spent is None  # sem finance.view


# ---------------------------------------------------------------------------
# Filtros por procedimento/profissional (aplicados antes da paginação)
# ---------------------------------------------------------------------------


def test_filtro_por_procedimento_atua_sobre_toda_populacao_antes_de_paginar(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof_a = _professional(session, org_id, branch.id, "Ianka")
    prof_b = _professional(session, org_id, branch.id, "Ingrid")
    svc_corte = _service(session, org_id, "Corte")
    svc_escova = _service(session, org_id, "Escova")
    client_a = _client(session, org_id, "Cliente Corte")
    client_b = _client(session, org_id, "Cliente Escova")
    _visit(session, org_id, branch.id, client_a.id, prof_a.id, svc_corte.id, closed_at=_NOW - timedelta(days=65), service_name="Corte")
    _visit(session, org_id, branch.id, client_b.id, prof_b.id, svc_escova.id, closed_at=_NOW - timedelta(days=65), service_name="Escova")

    result = recovery_service.list_recovery_queue(
        session, actor, subtab="pending", min_days=60, last_procedure_service_id=svc_corte.id, page=1, page_size=1,
    )
    # `total` reflete a população FILTRADA inteira, mesmo com page_size=1
    # (prova que o filtro roda antes do corte de paginação, nunca depois).
    assert result.total == 1
    assert result.items[0].client.id == client_a.id


def test_filtro_por_profissional_atua_sobre_toda_populacao_antes_de_paginar(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof_a = _professional(session, org_id, branch.id, "Ianka")
    prof_b = _professional(session, org_id, branch.id, "Ingrid")
    svc = _service(session, org_id)
    client_a = _client(session, org_id, "Cliente Ianka")
    client_b = _client(session, org_id, "Cliente Ingrid")
    _visit(session, org_id, branch.id, client_a.id, prof_a.id, svc.id, closed_at=_NOW - timedelta(days=65), professional_name="Ianka")
    _visit(session, org_id, branch.id, client_b.id, prof_b.id, svc.id, closed_at=_NOW - timedelta(days=66), professional_name="Ingrid")

    result = recovery_service.list_recovery_queue(
        session, actor, subtab="pending", min_days=60, last_professional_id=prof_b.id, page=1, page_size=1,
    )
    assert result.total == 1
    assert result.items[0].client.id == client_b.id
