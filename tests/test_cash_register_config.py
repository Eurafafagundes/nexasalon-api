"""Testes de `Financeiro > Caixa > Configurações do Caixa` (Etapa H) —
os 8 toggles e as regras de negócio que eles ligam/desligam:
`services/cash_register_config.py` (CRUD da própria configuração) e
`services/cash_register.py::assert_operational_prerequisites`/
`open_register` (onde os toggles são de fato aplicados)."""
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, ForbiddenError, ValidationDomainError
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.cash_register_config import CashRegisterConfigUpdate
from nexasalon_api.services import appointments, cash_register, cash_register_config, orders

_TZ = timezone(timedelta(hours=-3))
_ALL_ON = dict(
    require_open_register_for_order=True,
    require_open_register_for_payment=True,
    require_open_register_for_appointment=False,
    block_if_previous_day_open=True,
    require_close_previous_before_opening_today=True,
    single_open_register_per_branch=True,
    allow_admin_open_close=True,
    allow_receptionist_open_close=True,
)


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org caixa config", slug=f"org-cxc-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, role_name="OWNER", name="Rafael") -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name=name)
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name=role_name,
        permissions=frozenset(
            {
                "finance.view", "finance.manage", "settings.manage",
                "agenda.view_all", "agenda.create", "agenda.edit",
            }
        ),
    )


def _branch(session, org_id) -> uuid.UUID:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b.id


def _set_config(session, actor, **overrides):
    values = {**_ALL_ON, **overrides}
    return cash_register_config.update_config(session, actor, CashRegisterConfigUpdate(**values))


def _make_stale(session, register_id):
    """Empurra a ABERTURA deste caixa pra 2 dias atrás (dia operacional
    anterior), e força o ORM a reler a coluna — simula "existe um caixa
    de dia anterior ainda aberto" sem depender do relógio real."""
    session.execute(
        text("UPDATE cash_registers SET created_at = created_at - interval '2 days' WHERE id = :id"),
        {"id": register_id},
    )
    session.flush()
    session.expire_all()


# ---------------------------------------------------------------------
# CRUD da configuração — defaults, upsert, quem alterou
# ---------------------------------------------------------------------


def test_organizacao_sem_configuracao_opera_nos_defaults_de_fabrica(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)

    config = cash_register_config.get_effective_config(session, org_id)

    assert config.require_open_register_for_order is True
    assert config.require_open_register_for_payment is True
    assert config.require_open_register_for_appointment is False
    assert config.block_if_previous_day_open is True
    assert config.require_close_previous_before_opening_today is True
    assert config.single_open_register_per_branch is True
    assert config.allow_admin_open_close is True
    assert config.allow_receptionist_open_close is True

    display = cash_register_config.get_config_for_display(session, actor)
    assert display.updated_at is None
    assert display.updated_by_name is None


def test_atualizar_configuracao_grava_e_registra_quem_alterou(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, name="Clara Admin")

    display = _set_config(session, actor, require_open_register_for_appointment=True)

    assert display.require_open_register_for_appointment is True
    assert display.updated_by_name == "Clara Admin"
    assert display.updated_at is not None

    # Persistiu de verdade — uma nova leitura efetiva reflete o valor gravado.
    config = cash_register_config.get_effective_config(session, org_id)
    assert config.require_open_register_for_appointment is True


# ---------------------------------------------------------------------
# "Exigir caixa aberto para criar Comanda" (padrão ON) / agendamento
# (padrão OFF)
# ---------------------------------------------------------------------


_APPT_START = datetime(2026, 8, 20, 9, 0, tzinfo=_TZ)  # 2026-08-20 é quinta


def _appointment_data(session, org_id, branch_id, start_at=_APPT_START):
    from nexasalon_api.models.client import Client
    from nexasalon_api.models.professional import Professional
    from nexasalon_api.models.service import ProfessionalService, Service

    client = Client(organization_id=org_id, name="Cliente")
    prof = Professional(organization_id=org_id, branch_id=branch_id, name="Profissional")
    session.add_all([client, prof])
    session.flush()
    service = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=Decimal("100"))
    session.add(service)
    session.flush()
    session.add(ProfessionalService(professional_id=prof.id, service_id=service.id))
    session.flush()
    from nexasalon_api.models.professional import WorkingHours

    session.add(WorkingHours(organization_id=org_id, professional_id=prof.id, weekday=4, start_time="09:00", end_time="20:00"))
    session.flush()
    return AppointmentCreate(
        branch_id=branch_id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=start_at)],
    )


def test_criar_comanda_sem_caixa_aberto_e_recusada_por_padrao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    data = _appointment_data(session, org_id, branch_id)
    appt = appointments.create_appointment(session, actor, data)
    from nexasalon_api.models.enums import AppointmentStatus

    appt.status = AppointmentStatus.FINISHED
    session.flush()

    with pytest.raises(ValidationDomainError, match="caixa"):
        orders.create_order(session, actor, appt.id)


def test_criar_agendamento_sem_caixa_aberto_funciona_por_padrao(org_session):
    """Padrão OFF — ao contrário de Comanda, agendar não exige caixa
    aberto a menos que a organização ligue o toggle."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    data = _appointment_data(session, org_id, branch_id)

    appt = appointments.create_appointment(session, actor, data)
    assert appt.id is not None


def test_criar_agendamento_com_toggle_ligado_exige_caixa_aberto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    _set_config(session, actor, require_open_register_for_appointment=True)
    data = _appointment_data(session, org_id, branch_id)

    with pytest.raises(ValidationDomainError, match="caixa"):
        appointments.create_appointment(session, actor, data)

    cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    data2 = _appointment_data(session, org_id, branch_id)
    appt = appointments.create_appointment(session, actor, data2)
    assert appt.id is not None


# ---------------------------------------------------------------------
# Caixa de dia anterior ainda aberto
# ---------------------------------------------------------------------


def test_bloqueia_criar_comanda_se_existir_caixa_de_dia_anterior_aberto(org_session):
    """Cria o agendamento via ORM direto (não pelo service) de
    propósito: `block_if_previous_day_open` também bloquearia a
    CRIAÇÃO do agendamento (é uma regra geral de "operações"), o que
    testamos à parte em `test_bloqueia_criar_agendamento_se_existir_caixa_de_dia_anterior_aberto`
    — aqui o alvo é especificamente o gate de Comanda."""
    from nexasalon_api.models.appointment import Appointment, AppointmentItem
    from nexasalon_api.models.client import Client
    from nexasalon_api.models.enums import AppointmentSource, AppointmentStatus
    from nexasalon_api.models.professional import Professional
    from nexasalon_api.models.service import Service

    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    stale = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    _make_stale(session, stale.id)

    client = Client(organization_id=org_id, name="Cliente")
    prof = Professional(organization_id=org_id, branch_id=branch_id, name="Profissional")
    session.add_all([client, prof])
    session.flush()
    service = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=Decimal("100"))
    session.add(service)
    session.flush()
    appt = Appointment(
        organization_id=org_id, branch_id=branch_id, client_id=client.id, source=AppointmentSource.INTERNAL,
        status=AppointmentStatus.FINISHED,
    )
    session.add(appt)
    session.flush()
    session.add(
        AppointmentItem(
            organization_id=org_id, appointment_id=appt.id, professional_id=prof.id, service_id=service.id,
            start_at=_APPT_START, end_at=_APPT_START + timedelta(hours=1), duration_minutes=60,
            price=Decimal("100.00"),
        )
    )
    session.flush()

    with pytest.raises(ConflictError, match="ainda aberto"):
        orders.create_order(session, actor, appt.id)


def test_bloqueia_criar_agendamento_se_existir_caixa_de_dia_anterior_aberto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    stale = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    _make_stale(session, stale.id)

    data = _appointment_data(session, org_id, branch_id)
    with pytest.raises(ConflictError, match="ainda aberto"):
        appointments.create_appointment(session, actor, data)


def test_exigir_fechamento_do_anterior_antes_de_abrir_o_de_hoje(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    # Toggle "um por unidade" OFF pra isolar exatamente a regra sob
    # teste (sem isso, a segunda abertura já falharia por outro
    # motivo — "já existe caixa aberto nesta unidade").
    _set_config(session, actor, single_open_register_per_branch=False)
    stale = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    _make_stale(session, stale.id)

    with pytest.raises(ConflictError, match="ainda aberto"):
        cash_register.open_register(session, actor, branch_id, Decimal("0"), None)


def test_desligando_os_dois_toggles_permite_operar_com_caixa_de_dia_anterior_aberto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    _set_config(
        session, actor,
        block_if_previous_day_open=False,
        require_close_previous_before_opening_today=False,
        single_open_register_per_branch=False,
    )
    stale = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    _make_stale(session, stale.id)

    # Abrir um novo caixa hoje não é mais bloqueado.
    new_register = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    assert new_register.id != stale.id


# ---------------------------------------------------------------------
# Apenas um caixa aberto por unidade — agora configurável
# ---------------------------------------------------------------------


def test_toggle_desligado_permite_mais_de_um_caixa_aberto_na_mesma_unidade(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    _set_config(session, actor, single_open_register_per_branch=False)

    r1 = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    r2 = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    assert r1.id != r2.id


def test_toggle_ligado_por_padrao_continua_recusando_dois_caixas_na_mesma_unidade(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    cash_register.open_register(session, actor, branch_id, Decimal("0"), None)

    with pytest.raises(ConflictError):
        cash_register.open_register(session, actor, branch_id, Decimal("0"), None)


def test_duas_aberturas_concorrentes_na_mesma_unidade_so_uma_vence(org_session):
    """Item explícito da Etapa H: 'garanta no backend/transação que
    duas requisições simultâneas não abram dois caixas quando a
    configuração proibir'. Mesmo padrão de
    `test_order_products.py::test_concorrencia_no_ultimo_item_de_estoque_...`
    — threads reais + `threading.Barrier`, cada uma com sua PRÓPRIA
    sessão/transação; o `pg_advisory_xact_lock` (não um `SELECT ... FOR
    UPDATE`, que não teria linha nenhuma pra travar antes da primeira
    inserção) é quem serializa as duas na mesma unidade."""
    session, org_id = org_session
    actor_setup = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    user_id, membership_id, role_id = actor_setup.user_id, actor_setup.membership_id, actor_setup.role_id
    session.commit()

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def _try_open(label):
        with SessionLocal() as s:
            s.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
            local_actor = ActorContext(
                organization_id=org_id, user_id=user_id, membership_id=membership_id, role_id=role_id,
                role_name="OWNER", permissions=frozenset({"finance.manage"}),
            )
            barrier.wait()
            try:
                cash_register.open_register(s, local_actor, branch_id, Decimal("0"), None)
                s.commit()
                results[label] = "ok"
            except ConflictError:
                s.rollback()
                results[label] = "conflict"

    t1 = threading.Thread(target=_try_open, args=("a",))
    t2 = threading.Thread(target=_try_open, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert set(results.values()) == {"ok", "conflict"}, results


# ---------------------------------------------------------------------
# Quem pode abrir/fechar caixa — Administrador / Recepção
# ---------------------------------------------------------------------


def test_proprietario_nunca_e_bloqueado_pelos_toggles_de_perfil(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, role_name="OWNER")
    branch_id = _branch(session, org_id)
    _set_config(session, actor, allow_admin_open_close=False, allow_receptionist_open_close=False)

    register = cash_register.open_register(session, actor, branch_id, Decimal("0"), None)
    cash_register.close_register(session, actor, register.id, None, None)


def test_administrador_bloqueado_quando_toggle_desligado(org_session):
    session, org_id = org_session
    owner = _actor(session, org_id, role_name="OWNER")
    admin = _actor(session, org_id, role_name="ADMIN")
    branch_id = _branch(session, org_id)
    _set_config(session, owner, allow_admin_open_close=False)

    with pytest.raises(ForbiddenError):
        cash_register.open_register(session, admin, branch_id, Decimal("0"), None)


def test_recepcao_bloqueada_quando_toggle_desligado_mas_liberada_por_padrao(org_session):
    session, org_id = org_session
    owner = _actor(session, org_id, role_name="OWNER")
    receptionist = _actor(session, org_id, role_name="RECEPTIONIST")
    branch_id = _branch(session, org_id)

    # Padrão (nenhuma config gravada ainda) — Recepção já pode.
    register = cash_register.open_register(session, receptionist, branch_id, Decimal("0"), None)
    cash_register.close_register(session, receptionist, register.id, None, None)

    _set_config(session, owner, allow_receptionist_open_close=False)
    branch_id_2 = _branch(session, org_id)
    with pytest.raises(ForbiddenError):
        cash_register.open_register(session, receptionist, branch_id_2, Decimal("0"), None)
