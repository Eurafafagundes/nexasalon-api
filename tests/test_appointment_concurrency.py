"""Concorrência real — duas transações SIMULTÂNEAS (threads, sessões e
conexões de banco independentes) tentando reservar o MESMO profissional
no MESMO horário. Diferente de `test_appointments.py` (que testa a
validação de aplicação em sequência, numa única sessão), este arquivo
prova que a ÚLTIMA barreira — o trigger `check_appointment_item_overlap`
com `pg_advisory_xact_lock` (migration 0004) — realmente serializa e
resolve a corrida, não só a checagem otimista da aplicação.

Mecânica: a Thread 1 insere um item, e antes de commitar, DORME
segurando a transação aberta (e o advisory lock) por um tempo. A
Thread 2 só tenta inserir depois que a Thread 1 já teria "passado" pela
checagem pré-insert — force a corrida de verdade: a Thread 2 fica
BLOQUEADA dentro do próprio `pg_advisory_xact_lock` até a Thread 1
commitar ou dar rollback, e só então enxerga (ou não) o conflito.
"""
import threading
import time
import uuid
from datetime import date, datetime, time as time_type, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, ForbiddenError
from nexasalon_api.models.client import Client
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import appointment_item_repo, appointment_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.services import appointments

_OUR_THURSDAY = 4
_TZ = timezone(timedelta(hours=-3))
_ALL_AGENDA_PERMS = frozenset(
    {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel", "agenda.force_overlap"}
)


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


@pytest.fixture()
def scenario():
    """Cria org/branch/professional/service/client numa transação
    própria e COMMITA de verdade (não só flush) — as duas threads do
    teste abrem sessões NOVAS e só enxergam dados já commitados."""
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org concorrência", slug=f"org-conc-{org_id.hex[:8]}"))
        session.flush()
        branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{org_id.hex[:8]}")
        session.add(branch)
        session.flush()
        prof = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional")
        session.add(prof)
        session.flush()
        service = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=100)
        session.add(service)
        session.flush()
        session.add(ProfessionalService(professional_id=prof.id, service_id=service.id))
        session.add(
            WorkingHours(
                organization_id=org_id, professional_id=prof.id, weekday=_OUR_THURSDAY,
                start_time=time_type(9, 0), end_time=time_type(18, 0),
            )
        )
        client = Client(organization_id=org_id, name="Cliente")
        session.add(client)
        session.flush()
        user = User(email=f"user-{org_id.hex[:8]}@nexasalon.local", name="Usuário")
        session.add(user)
        session.flush()
        # captura os IDs ANTES do commit — depois do commit os atributos
        # expiram (`expire_on_commit=True`, o padrão), e o refresh
        # implícito na próxima leitura poderia pegar uma conexão
        # DIFERENTE do pool, sem o `app.current_org_id` desta sessão.
        ids = {
            "org_id": org_id, "branch_id": branch.id, "professional_id": prof.id,
            "service_id": service.id, "client_id": client.id, "user_id": user.id,
        }
        session.commit()
        return ids


def _open_scoped_session(org_id):
    session = SessionLocal()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
    return session


def test_duas_transacoes_simultaneas_uma_ganha_outra_recebe_conflito(scenario):
    results = {}
    thread1_flushed = threading.Event()

    def _thread1():
        session = _open_scoped_session(scenario["org_id"])
        try:
            appt = appointment_repo.create(
                session, scenario["org_id"], branch_id=scenario["branch_id"], client_id=scenario["client_id"],
                notes=None, created_by=scenario["user_id"],
            )
            appointment_item_repo.create(
                session, scenario["org_id"], appointment_id=appt.id, service_id=scenario["service_id"],
                professional_id=scenario["professional_id"], start_at=_dt(14, 0), end_at=_dt(15, 0),
                duration_minutes=60, price=100,
            )
            # item já foi inserido e o advisory lock deste profissional já
            # foi adquirido nesta transação — segura tudo aberto de propósito.
            thread1_flushed.set()
            time.sleep(1.0)
            session.commit()
            results["thread1"] = "ok"
        except Exception as exc:  # pragma: no cover - só се algo inesperado falhar
            session.rollback()
            results["thread1"] = exc
        finally:
            session.close()

    def _thread2():
        thread1_flushed.wait(timeout=5)
        session = _open_scoped_session(scenario["org_id"])
        start = time.perf_counter()
        try:
            appt = appointment_repo.create(
                session, scenario["org_id"], branch_id=scenario["branch_id"], client_id=scenario["client_id"],
                notes=None, created_by=scenario["user_id"],
            )
            appointment_item_repo.create(
                session, scenario["org_id"], appointment_id=appt.id, service_id=scenario["service_id"],
                professional_id=scenario["professional_id"], start_at=_dt(14, 30), end_at=_dt(15, 30),
                duration_minutes=60, price=100,
            )
            session.commit()
            results["thread2"] = "ok"
        except Exception as exc:
            session.rollback()
            results["thread2"] = exc
        finally:
            results["thread2_elapsed"] = time.perf_counter() - start
            session.close()

    t1 = threading.Thread(target=_thread1)
    t2 = threading.Thread(target=_thread2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["thread1"] == "ok"
    assert isinstance(results["thread2"], IntegrityError), f"esperava conflito, veio: {results['thread2']!r}"
    assert "appointment_item_overlap" in str(results["thread2"].orig)
    # prova que a Thread 2 ficou de fato BLOQUEADA esperando a Thread 1
    # (pg_advisory_xact_lock) — não que ela só "chegou depois" por acaso.
    assert results["thread2_elapsed"] >= 0.8

    with SessionLocal() as check_session:
        check_session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(scenario["org_id"])}
        )
        items = appointment_item_repo.list_busy_for_professional_on_range(
            check_session, scenario["org_id"], professional_id=scenario["professional_id"],
            range_start=_dt(0), range_end=_dt(23, 59),
        )
        assert len(items) == 1
        assert items[0].start_at == _dt(14, 0)


def test_force_overlap_com_permissao_funciona_sob_concorrencia_real(scenario):
    """Mesma corrida, mas a Thread 2 seta `app.allow_overlap` (equivalente
    a um ator com `agenda.force_overlap`) — o trigger deve permitir o
    encaixe mesmo tendo esperado a Thread 1 pelo advisory lock."""
    results = {}
    thread1_flushed = threading.Event()

    def _thread1():
        session = _open_scoped_session(scenario["org_id"])
        try:
            appt = appointment_repo.create(
                session, scenario["org_id"], branch_id=scenario["branch_id"], client_id=scenario["client_id"],
                notes=None, created_by=scenario["user_id"],
            )
            appointment_item_repo.create(
                session, scenario["org_id"], appointment_id=appt.id, service_id=scenario["service_id"],
                professional_id=scenario["professional_id"], start_at=_dt(14, 0), end_at=_dt(15, 0),
                duration_minutes=60, price=100,
            )
            thread1_flushed.set()
            time.sleep(1.0)
            session.commit()
            results["thread1"] = "ok"
        except Exception as exc:
            session.rollback()
            results["thread1"] = exc
        finally:
            session.close()

    def _thread2():
        thread1_flushed.wait(timeout=5)
        session = _open_scoped_session(scenario["org_id"])
        try:
            session.execute(text("SET LOCAL app.allow_overlap = 'true'"))
            appt = appointment_repo.create(
                session, scenario["org_id"], branch_id=scenario["branch_id"], client_id=scenario["client_id"],
                notes=None, created_by=scenario["user_id"],
            )
            appointment_item_repo.create(
                session, scenario["org_id"], appointment_id=appt.id, service_id=scenario["service_id"],
                professional_id=scenario["professional_id"], start_at=_dt(14, 30), end_at=_dt(15, 30),
                duration_minutes=60, price=100,
            )
            session.commit()
            results["thread2"] = "ok"
        except Exception as exc:
            session.rollback()
            results["thread2"] = exc
        finally:
            session.close()

    t1 = threading.Thread(target=_thread1)
    t2 = threading.Thread(target=_thread2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["thread1"] == "ok"
    assert results["thread2"] == "ok"

    with SessionLocal() as check_session:
        check_session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(scenario["org_id"])}
        )
        items = appointment_item_repo.list_busy_for_professional_on_range(
            check_session, scenario["org_id"], professional_id=scenario["professional_id"],
            range_start=_dt(0), range_end=_dt(23, 59),
        )
        assert len(items) == 2


def test_force_overlap_sem_permissao_e_recusado_mesmo_sob_concorrencia(scenario):
    """Desta vez pela CAMADA DE SERVIÇO completa (`appointments.create_appointment`),
    com dois atores concorrentes: um dono normal e um SEM
    `agenda.force_overlap` mandando `force_overlap=true` mesmo assim —
    o pedido nunca deve funcionar, com ou sem corrida real de por trás.

    Ajuste pós-revisão: a checagem de permissão do force_overlap agora
    acontece ANTES de qualquer leitura/validação (ver
    `_resolve_force_overlap` em services/appointments.py), então a
    Thread 2 nem chega a competir pelo advisory lock — recebe
    ForbiddenError na hora. Mesmo assim o teste mantém a sincronização
    com a Thread 1 pra provar que isso vale mesmo com uma transação
    concorrente genuína em andamento (não é só "não tinha ninguém
    competindo")."""
    results = {}
    thread1_flushed = threading.Event()

    owner_actor = ActorContext(
        organization_id=scenario["org_id"], user_id=scenario["user_id"], membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Owner", permissions=_ALL_AGENDA_PERMS, professional_id=None,
    )
    restricted_actor = ActorContext(
        organization_id=scenario["org_id"], user_id=scenario["user_id"], membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Restrito",
        permissions=_ALL_AGENDA_PERMS - {"agenda.force_overlap"}, professional_id=None,
    )

    def _thread1():
        session = _open_scoped_session(scenario["org_id"])
        try:
            data = AppointmentCreate(
                branch_id=scenario["branch_id"], client_id=scenario["client_id"],
                items=[
                    AppointmentItemCreate(
                        professional_id=scenario["professional_id"], service_id=scenario["service_id"],
                        start_at=_dt(14, 0),
                    )
                ],
            )
            appointments.create_appointment(session, owner_actor, data)
            thread1_flushed.set()
            time.sleep(1.0)
            session.commit()
            results["thread1"] = "ok"
        except Exception as exc:
            session.rollback()
            results["thread1"] = exc
        finally:
            session.close()

    def _thread2():
        thread1_flushed.wait(timeout=5)
        session = _open_scoped_session(scenario["org_id"])
        try:
            data = AppointmentCreate(
                branch_id=scenario["branch_id"], client_id=scenario["client_id"], force_overlap=True,
                items=[
                    AppointmentItemCreate(
                        professional_id=scenario["professional_id"], service_id=scenario["service_id"],
                        start_at=_dt(14, 30),
                    )
                ],
            )
            appointments.create_appointment(session, restricted_actor, data)
            session.commit()
            results["thread2"] = "ok"
        except Exception as exc:
            session.rollback()
            results["thread2"] = exc
        finally:
            session.close()

    t1 = threading.Thread(target=_thread1)
    t2 = threading.Thread(target=_thread2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["thread1"] == "ok"
    # `force_overlap=true` sem a permission nunca funciona: 403 explícito
    # e imediato, mesmo com a Thread 1 segurando o advisory lock do MESMO
    # profissional em paralelo — a Thread 2 nem chega a competir por ele.
    assert isinstance(results["thread2"], ForbiddenError), (
        f"esperava ForbiddenError, veio: {results['thread2']!r}"
    )

    with SessionLocal() as check_session:
        check_session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(scenario["org_id"])}
        )
        items = appointment_item_repo.list_busy_for_professional_on_range(
            check_session, scenario["org_id"], professional_id=scenario["professional_id"],
            range_start=_dt(0), range_end=_dt(23, 59),
        )
        assert len(items) == 1
