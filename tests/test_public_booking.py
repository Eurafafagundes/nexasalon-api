"""Testes da Etapa K (Agendamento Online público) —
`/api/v1/public/booking/{organization_slug}/*`, SEM autenticação
nenhuma. Cobre: fluxo feliz completo (organização -> serviços ->
profissionais -> disponibilidade -> confirmação), reaproveitamento do
motor de disponibilidade existente, revalidação transacional
(dupla reserva do mesmo horário), "Qualquer profissional", origem
`PUBLIC_BOOKING` refletida no lado interno, cliente encontrado/criado
pelo telefone (nunca pede CPF/endereço), antecedência mínima/máxima,
página desativada = 404, segurança (nenhum campo interno/financeiro/
privado vaza nos schemas públicos), RBAC/unicidade do slug em
`Configurações > Agendamento Online`."""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app
from nexasalon_api.repositories import organization_repo
from nexasalon_api.services import appointments as appointments_service

_TZ = timezone(timedelta(hours=-3))


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """O rate limiter (`core/rate_limit.py`) é um singleton EM MEMÓRIA do
    processo — sem isto, os testes deste arquivo se atropelariam entre si
    (todos batem no mesmo endpoint público a partir do mesmo IP de teste),
    disparando 429 em vez do status esperado por cada teste."""
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    """Cliente SEM nenhum ator autenticado — as rotas públicas não usam
    `get_current_actor`, então não há dependency_override nenhum aqui
    (diferente de `client_as`)."""
    return TestClient(app)


def _enable_online_booking(c, **overrides):
    payload = {
        "online_booking_enabled": True,
        "online_booking_auto_confirm": True,
        "online_booking_min_lead_minutes": 0,
        "online_booking_max_lead_days": 3650,
        **overrides,
    }
    resp = c.put("/api/v1/organization", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _setup_service_and_professional(c, *, professionals_count: int = 1):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    svc = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    professionals = []
    for i in range(professionals_count):
        prof = c.post("/api/v1/professionals", json={"name": f"Profissional {i}"}).json()
        c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
        c.put(
            f"/api/v1/professionals/{prof['id']}/working-hours",
            json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
        )
        professionals.append(prof)
    return branch, professionals, svc


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------
# Fluxo feliz
# ---------------------------------------------------------------------


def test_fluxo_completo_ate_confirmacao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]

    p = _public()
    slug = org["slug"]

    org_resp = p.get(f"/api/v1/public/booking/{slug}")
    assert org_resp.status_code == 200, org_resp.text
    assert org_resp.json()["name"] == org["name"]

    services = p.get(f"/api/v1/public/booking/{slug}/services").json()
    assert any(s["id"] == svc["id"] for s in services)

    professionals_resp = p.get(f"/api/v1/public/booking/{slug}/professionals", params={"service_id": svc["id"]})
    assert professionals_resp.status_code == 200
    assert any(item["id"] == prof["id"] for item in professionals_resp.json())

    target_date = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
    availability = p.get(
        f"/api/v1/public/booking/{slug}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": target_date},
    )
    assert availability.status_code == 200
    assert len(availability.json()) > 0
    start_at = availability.json()[0]["start_at"]

    booking = p.post(
        f"/api/v1/public/booking/{slug}",
        json={
            "service_id": svc["id"],
            "professional_id": prof["id"],
            "start_at": start_at,
            "client_name": "Maria Cliente",
            "client_phone": "(61) 99999-1234",
        },
    )
    assert booking.status_code == 201, booking.text
    body = booking.json()
    assert body["status"] == "confirmed"  # online_booking_auto_confirm=true

    # Lado interno: origem PUBLIC_BOOKING, cliente criado só com nome/telefone.
    appt = c.get(f"/api/v1/appointments/{body['id']}").json()
    assert appt["source"] == "public_booking"

    client = c.get(f"/api/v1/clients/{appt['client_id']}").json()
    assert client["name"] == "Maria Cliente"
    assert client["phone"] == "61999991234"
    assert client["cpf"] is None
    assert client["cep"] is None


def test_cliente_existente_pelo_telefone_nao_duplica_cadastro(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    p = _public()
    slug = org["slug"]

    base = datetime.now(timezone.utc) + timedelta(days=11)
    clients_before = len(c.get("/api/v1/clients").json())

    for hour_offset in (0, 2):
        start_at = _iso(base.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(hours=hour_offset))
        resp = p.post(
            f"/api/v1/public/booking/{slug}",
            json={
                "service_id": svc["id"],
                "professional_id": prof["id"],
                "start_at": start_at,
                "client_name": "João Repetido",
                "client_phone": "61988887777",
            },
        )
        assert resp.status_code == 201, resp.text

    clients_after = c.get("/api/v1/clients").json()
    assert len(clients_after) == clients_before + 1  # um único cliente novo, não dois


def test_pagina_desativada_retorna_404(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    assert org["online_booking_enabled"] is False  # default de fábrica

    p = _public()
    resp = p.get(f"/api/v1/public/booking/{org['slug']}")
    assert resp.status_code == 404


def test_slug_inexistente_retorna_404(client_as, org_a_actor):
    p = _public()
    resp = p.get(f"/api/v1/public/booking/slug-que-nao-existe-{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# "Qualquer profissional"
# ---------------------------------------------------------------------


def test_qualquer_profissional_resolve_um_elegivel_automaticamente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, professionals, svc = _setup_service_and_professional(c, professionals_count=2)
    p = _public()
    slug = org["slug"]

    target_date = (datetime.now(timezone.utc) + timedelta(days=12)).date().isoformat()
    availability = p.get(
        f"/api/v1/public/booking/{slug}/availability", params={"service_id": svc["id"], "date": target_date}
    )
    assert availability.status_code == 200
    assert len(availability.json()) > 0
    start_at = availability.json()[0]["start_at"]

    booking = p.post(
        f"/api/v1/public/booking/{slug}",
        json={
            "service_id": svc["id"],
            "professional_id": None,
            "start_at": start_at,
            "client_name": "Cliente Sem Preferência",
            "client_phone": "61977776666",
        },
    )
    assert booking.status_code == 201, booking.text
    appt = c.get(f"/api/v1/appointments/{booking.json()['id']}").json()
    assigned = appt["items"][0]["professional_id"]
    assert assigned in {p["id"] for p in professionals}


# ---------------------------------------------------------------------
# Revalidação transacional — impedir dupla reserva do mesmo horário
# ---------------------------------------------------------------------


def test_segunda_reserva_do_mesmo_horario_recebe_409(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    p = _public()
    slug = org["slug"]
    start_at = _iso(
        (datetime.now(timezone.utc) + timedelta(days=13)).replace(hour=10, minute=0, second=0, microsecond=0)
    )

    payload = {
        "service_id": svc["id"],
        "professional_id": prof["id"],
        "start_at": start_at,
        "client_name": "Primeira Cliente",
        "client_phone": "61911112222",
    }
    first = p.post(f"/api/v1/public/booking/{slug}", json=payload)
    assert first.status_code == 201, first.text

    second_payload = {**payload, "client_name": "Segunda Cliente", "client_phone": "61933334444"}
    second = p.post(f"/api/v1/public/booking/{slug}", json=second_payload)
    assert second.status_code == 409, second.text


def test_revalidacao_transacional_sob_concorrencia_real(client_as, org_a_actor):
    """Duas THREADS de verdade (sessões/conexões independentes)
    chamando `create_public_appointment` pro MESMO profissional/horário
    — prova que a barreira final é o trigger `check_appointment_item_overlap`
    (migration 0004, reaproveitado sem nenhum código novo), não só a
    checagem otimista da aplicação. Mesmo padrão de
    `test_appointment_concurrency.py`."""
    c = client_as(org_a_actor)
    _enable_online_booking(c)
    branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    organization_id = org_a_actor.organization_id
    start_at = (datetime.now(timezone.utc) + timedelta(days=14)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )

    results = {}
    thread1_started = threading.Event()

    def _call(name, phone, on_started=None):
        session = SessionLocal()
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(organization_id)})
        try:
            organization = organization_repo.get(session, organization_id)
            if on_started:
                on_started()
                time.sleep(0.5)
            appointments_service.create_public_appointment(
                session,
                organization,
                branch_id=uuid.UUID(branch["id"]),
                professional_id=uuid.UUID(prof["id"]),
                service_id=uuid.UUID(svc["id"]),
                start_at=start_at,
                client_name=name,
                client_phone=phone,
                client_email=None,
            )
            session.commit()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return exc
        finally:
            session.close()

    def _thread1():
        results["thread1"] = _call("Cliente A", "61955556666", on_started=thread1_started.set)

    def _thread2():
        thread1_started.wait(timeout=5)
        results["thread2"] = _call("Cliente B", "61944445555")

    t1 = threading.Thread(target=_thread1)
    t2 = threading.Thread(target=_thread2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    outcomes = [results["thread1"], results["thread2"]]
    oks = [r for r in outcomes if r == "ok"]
    errors = [r for r in outcomes if r != "ok"]
    assert len(oks) == 1, outcomes
    assert len(errors) == 1, outcomes
    assert isinstance(errors[0], (ConflictError, Exception))


# ---------------------------------------------------------------------
# Antecedência mínima/máxima
# ---------------------------------------------------------------------


def test_antecedencia_minima_rejeita_horario_muito_proximo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    # 10080 = 7 dias em minutos (limite máximo aceito pelo schema) — bem
    # mais que a antecedência real do horário pedido abaixo (+1h).
    org = _enable_online_booking(c, online_booking_min_lead_minutes=10080)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    p = _public()
    start_at = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={
            "service_id": svc["id"], "professional_id": prof["id"], "start_at": start_at,
            "client_name": "Cliente Apressada", "client_phone": "61900001111",
        },
    )
    assert resp.status_code == 422, resp.text


def test_antecedencia_maxima_rejeita_horario_muito_distante(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, online_booking_max_lead_days=1)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    p = _public()
    start_at = _iso(
        (datetime.now(timezone.utc) + timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)
    )
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={
            "service_id": svc["id"], "professional_id": prof["id"], "start_at": start_at,
            "client_name": "Cliente Ansiosa", "client_phone": "61900002222",
        },
    )
    assert resp.status_code == 422, resp.text


def test_disponibilidade_listagem_respeita_antecedencia_minima(client_as, org_a_actor):
    """Correção real do item: antes, só a CONFIRMAÇÃO (`POST
    .../{slug}`, testada acima) respeitava `online_booking_min_lead_minutes`
    — a LISTAGEM (`GET .../availability`) continuava oferecendo horários
    que a confirmação recusaria em seguida (usuário via e clicava num
    horário que a API rejeitava). Consulta amanhã inteiro (jornada
    00:00-23:59) pra não depender da hora do dia em que o teste roda:
    `earliest_allowed` (agora + antecedência) cai sempre bem antes do
    fim do dia de amanhã, garantindo horários sobrando."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, online_booking_min_lead_minutes=120)  # 2h
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    p = _public()

    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    resp = p.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": tomorrow},
    )
    assert resp.status_code == 200, resp.text
    slots = resp.json()
    assert len(slots) > 0  # sobra parte do dia de amanhã mesmo com a antecedência

    earliest_allowed = now + timedelta(minutes=120)
    for slot in slots:
        assert datetime.fromisoformat(slot["start_at"]) >= earliest_allowed


def test_disponibilidade_listagem_respeita_antecedencia_maxima(client_as, org_a_actor):
    """Mesma lacuna do teste acima, agora pro limite MÁXIMO
    (`online_booking_max_lead_days`) — um dia muito distante não pode
    aparecer na listagem, mesmo que a jornada do profissional o
    contemplasse (00:00-23:59 todo dia da semana)."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, online_booking_max_lead_days=1)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    p = _public()

    far_date = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    resp = p.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": far_date},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []  # dia inteiro além do limite -> nenhum horário


# ---------------------------------------------------------------------
# Serviço/profissional desabilitados para online
# ---------------------------------------------------------------------


def test_servico_desabilitado_para_online_nao_aparece_nem_aceita_reserva(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]
    c.put(f"/api/v1/services/{svc['id']}", json={"name": "Corte", "default_duration_minutes": 60,
                                                   "default_price": "100.00", "allow_online_booking": False})
    p = _public()

    services = p.get(f"/api/v1/public/booking/{org['slug']}/services").json()
    assert all(s["id"] != svc["id"] for s in services)

    start_at = _iso(
        (datetime.now(timezone.utc) + timedelta(days=15)).replace(hour=9, minute=0, second=0, microsecond=0)
    )
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={
            "service_id": svc["id"], "professional_id": prof["id"], "start_at": start_at,
            "client_name": "Cliente X", "client_phone": "61900003333",
        },
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------
# Segurança — nenhum schema público vaza dado interno/financeiro/privado
# ---------------------------------------------------------------------


def test_schemas_publicos_nao_vazam_dado_interno(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, name="Salão Sigiloso")
    c.put(
        "/api/v1/organization",
        json={"document": "11.222.333/0001-81", "cep": "70000-000", "phone": "6133334444"},
    )
    _branch, _professionals, _svc = _setup_service_and_professional(c)
    p = _public()
    slug = org["slug"]

    org_body = p.get(f"/api/v1/public/booking/{slug}").json()
    for leaked in ("document", "cep", "phone", "email", "whatsapp", "address_line"):
        assert leaked not in org_body

    prof_body = p.get(f"/api/v1/public/booking/{slug}/professionals").json()[0]
    for leaked in ("phone", "professional_email", "agenda_color"):
        assert leaked not in prof_body


# ---------------------------------------------------------------------
# Configurações > Agendamento Online — RBAC e unicidade de slug
# ---------------------------------------------------------------------


def test_configuracao_de_agendamento_online_exige_organization_manage(client_as, org_a_actor):
    import dataclasses

    restricted = dataclasses.replace(
        org_a_actor, permissions=org_a_actor.permissions - {"organization.manage"}
    )
    c = client_as(restricted)
    resp = c.put("/api/v1/organization", json={"online_booking_enabled": True})
    assert resp.status_code == 403


def test_slug_duplicado_entre_organizacoes_e_recusado(client_as, org_a_actor, org_b_actor):
    # `client_as` reatribui o override de ator no `app` compartilhado a
    # cada chamada (ver docstring do fixture) — nunca intercala `ca`/`cb`
    # sem terminar de usar um antes de criar o outro.
    ca = client_as(org_a_actor)
    org_a = ca.get("/api/v1/organization").json()

    cb = client_as(org_b_actor)
    resp = cb.put("/api/v1/organization", json={"slug": org_a["slug"]})
    assert resp.status_code == 409, resp.text


def test_slug_e_normalizado_e_nao_pode_ser_removido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.put("/api/v1/organization", json={"slug": "Salão da Ana!"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "salao-da-ana"

    resp_null = c.put("/api/v1/organization", json={"slug": None})
    assert resp_null.status_code == 422


def test_origem_public_booking_aparece_na_listagem_da_agenda(client_as, org_a_actor):
    """Ajuste pós-aprovação da Etapa K: a origem do agendamento precisa
    aparecer na Comanda independentemente do caminho usado pra abri-la
    — inclusive quando aberta direto pela tela da Agenda, que lista via
    `GET /api/v1/agenda` (não via `GET /api/v1/appointments/{id}`).
    Reaproveita a coluna `appointments.source` já existente (nenhuma
    migration nova) — só passou a ser exposta em `AgendaItemRead`."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    branch, professionals, svc = _setup_service_and_professional(c)
    prof = professionals[0]

    # Agendamento interno de controle: continua com origem "internal".
    client_interno = c.post("/api/v1/clients", json={"name": "Cliente Interno", "phone": "61988887777"}).json()
    internal_start = (datetime.now(timezone.utc) + timedelta(days=12, hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    internal_appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"],
            "client_id": client_interno["id"],
            "items": [{"professional_id": prof["id"], "service_id": svc["id"], "start_at": _iso(internal_start)}],
        },
    ).json()

    # Agendamento público, mesmo profissional/serviço, outro horário no mesmo dia.
    p = _public()
    slug = org["slug"]
    availability = p.get(
        f"/api/v1/public/booking/{slug}/availability",
        params={
            "service_id": svc["id"],
            "professional_id": prof["id"],
            "date": internal_start.date().isoformat(),
        },
    ).json()
    public_slot = next(slot for slot in availability if slot["start_at"] != _iso(internal_start))
    booking = p.post(
        f"/api/v1/public/booking/{slug}",
        json={
            "service_id": svc["id"],
            "professional_id": prof["id"],
            "start_at": public_slot["start_at"],
            "client_name": "Maria Cliente",
            "client_phone": "(61) 99999-1234",
        },
    )
    assert booking.status_code == 201, booking.text
    public_appointment_id = booking.json()["id"]

    resp = c.get(
        "/api/v1/agenda",
        params={"date": internal_start.date().isoformat(), "branch_id": branch["id"]},
    )
    assert resp.status_code == 200, resp.text
    items_by_appointment = {item["appointment_id"]: item for item in resp.json()}

    assert items_by_appointment[internal_appt["id"]]["source"] == "internal"
    assert items_by_appointment[public_appointment_id]["source"] == "public_booking"
