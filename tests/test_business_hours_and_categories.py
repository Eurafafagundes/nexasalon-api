"""Etapa M, P1/P2 — horário de funcionamento do estabelecimento
(camada superior à jornada do profissional), "mesmo dia" ON/OFF, e
Categoria → Serviço no Agendamento Online público.

Reaproveita os mesmos helpers/convenções de `test_public_booking.py`
(`_enable_online_booking`, `_setup_service_and_professional`,
`_register_customer`) — profissionais nascem com jornada 00:00-23:59
todos os dias, isolando qualquer restrição observada como vindo
EXCLUSIVAMENTE do horário de funcionamento/regras de agendamento, não
da jornada do profissional."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


def _setup_service_and_professional(c):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    svc = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    prof = c.post("/api/v1/professionals", json={"name": "Profissional"}).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    return branch, prof, svc


def _register_customer(p, *, name: str = "Cliente Teste", phone: str | None = None) -> str:
    phone = phone or f"619{uuid.uuid4().int % 10_000_000:07d}"
    email = f"cliente-{uuid.uuid4().hex[:10]}@example.com"
    resp = p.post(
        "/api/v1/customer-auth/register",
        json={"name": name, "email": email, "phone": phone, "password": "Senha123!", "password_confirm": "Senha123!"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _set_business_hours(c, *, closed_weekdays: set[int] = frozenset(), open_start="09:00:00", open_end="18:00:00"):
    items = [
        {"weekday": w, "is_open": False} if w in closed_weekdays
        else {"weekday": w, "is_open": True, "start_time": open_start, "end_time": open_end}
        for w in range(7)
    ]
    resp = c.put("/api/v1/organization/business-hours", json={"items": items})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _our_weekday(d) -> int:
    return (d.weekday() + 1) % 7


def _next_date_for_weekday(target_weekday: int, *, min_days_ahead: int = 5):
    d = (datetime.now(timezone.utc) + timedelta(days=min_days_ahead)).date()
    while _our_weekday(d) != target_weekday:
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------------
# Horário de funcionamento do estabelecimento — camada superior.
# ---------------------------------------------------------------------


def test_sem_configuracao_de_horario_de_funcionamento_nao_restringe_nada(client_as, org_a_actor):
    """Compatibilidade retroativa: organização que nunca configurou
    `business_hours` continua sem restrição nenhuma (comportamento
    idêntico ao de antes desta feature existir)."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    target = _next_date_for_weekday(1)  # segunda qualquer, sem configurar business_hours

    resp = c.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": target.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) > 0


def test_estabelecimento_fechado_bloqueia_disponibilidade_mesmo_com_jornada_ativa(client_as, org_a_actor):
    """O item central da Etapa M: profissional tem jornada 00:00-23:59
    todos os dias (helper padrão), mas a EMPRESA está fechada numa
    segunda — nenhum horário deve ser oferecido nesse dia, pra nenhum
    profissional, mesmo com jornada antiga cadastrada."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    _set_business_hours(c, closed_weekdays={1})  # segunda fechada
    monday = _next_date_for_weekday(1)

    resp = c.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": monday.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_estabelecimento_fechado_bloqueia_criacao_via_agendamento_online(client_as, org_a_actor):
    """A garantia real não pode ficar só na LISTAGEM — a criação
    (confirmação) tem que recusar igual, mesmo que alguém monte a
    requisição manualmente pra um horário de segunda."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    _set_business_hours(c, closed_weekdays={1})
    monday = _next_date_for_weekday(1)
    start_at = datetime.combine(monday, datetime.min.time(), tzinfo=timezone.utc).replace(hour=10)

    p = _public()
    token = _register_customer(p)
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={"service_id": svc["id"], "professional_id": prof["id"], "start_at": start_at.isoformat()},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


def test_estabelecimento_fechado_bloqueia_criacao_de_agendamento_interno():
    """"Novo Agendamento" (staff, Agenda interna) reaproveita o MESMO
    motor (`_assert_within_working_hours` -> `effective_working_windows_utc`)
    — não deve conseguir criar um agendamento numa segunda fechada,
    mesmo com a jornada do profissional cobrindo o dia inteiro. Direto
    no service layer, mesma abordagem de `test_appointments.py`."""
    import uuid as uuid_mod
    from datetime import date as date_type, time as time_of_day
    from datetime import timezone as tz_type

    from sqlalchemy import text as sa_text

    from nexasalon_api.core.actor import ActorContext
    from nexasalon_api.core.db import SessionLocal
    from nexasalon_api.core.exceptions import ValidationDomainError as VDE
    from nexasalon_api.models.client import Client as ClientModel
    from nexasalon_api.models.identity import User as UserModel
    from nexasalon_api.models.organization import BusinessHours, Branch as BranchModel, Organization as OrgModel
    from nexasalon_api.models.professional import Professional as ProfessionalModel, WorkingHours as WHModel
    from nexasalon_api.models.service import ProfessionalService as PSModel, Service as ServiceModel
    from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
    from nexasalon_api.services import appointments as appointments_service

    org_id = uuid_mod.uuid4()
    with SessionLocal() as session:
        session.execute(sa_text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(OrgModel(id=org_id, name="Org Fechada", slug=f"org-fechada-{org_id.hex[:8]}"))
        session.flush()

        branch = BranchModel(organization_id=org_id, name="Unidade", slug=f"unidade-{org_id.hex[:8]}")
        session.add(branch)
        session.flush()

        prof = ProfessionalModel(organization_id=org_id, branch_id=branch.id, name="Profissional")
        session.add(prof)
        session.flush()

        service = ServiceModel(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=100)
        session.add(service)
        session.flush()

        session.add(PSModel(professional_id=prof.id, service_id=service.id))
        # jornada cobrindo o dia inteiro em TODOS os dias da semana.
        for weekday in range(7):
            session.add(
                WHModel(
                    organization_id=org_id, professional_id=prof.id, weekday=weekday,
                    start_time=time_of_day(0, 0), end_time=time_of_day(23, 59),
                )
            )
        # empresa fechada às segundas (weekday=1).
        for weekday in range(7):
            session.add(
                BusinessHours(
                    organization_id=org_id, weekday=weekday, is_open=(weekday != 1),
                    start_time=None if weekday == 1 else time_of_day(0, 0),
                    end_time=None if weekday == 1 else time_of_day(23, 58),
                )
            )
        client = ClientModel(organization_id=org_id, name="Cliente")
        session.add(client)
        user = UserModel(email=f"user-{org_id.hex[:8]}@nexasalon.local", name="Usuário Teste")
        session.add(user)
        session.flush()

        actor = ActorContext(
            organization_id=org_id, user_id=user.id, membership_id=uuid_mod.uuid4(), role_id=uuid_mod.uuid4(),
            role_name="Owner",
            permissions=frozenset({"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit"}),
            professional_id=None,
        )

        monday = date_type(2026, 8, 24)  # 2026-08-24 é segunda-feira.
        start_at = datetime.combine(monday, time_of_day(10, 0), tzinfo=tz_type.utc)
        data = AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=start_at)],
        )
        with pytest.raises(VDE):
            appointments_service.create_appointment(session, actor, data)
        session.rollback()


def test_horario_de_funcionamento_recorta_jornada_do_profissional(client_as, org_a_actor):
    """Empresa aberta terça 09h-12h, profissional com jornada
    00:00-23:59 (helper padrão) -> disponibilidade efetiva é só
    09h-12h, nunca o dia inteiro. Item explícito: "não confundir
    horário da empresa com jornada do profissional"."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    _set_business_hours(c, open_start="09:00:00", open_end="12:00:00")
    tuesday = _next_date_for_weekday(2)

    resp = c.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": tuesday.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    slots = resp.json()
    assert len(slots) > 0
    for slot in slots:
        hour = datetime.fromisoformat(slot["start_at"]).astimezone(timezone.utc).hour
        assert 9 <= hour < 12


# ---------------------------------------------------------------------
# "Permitir agendamento para o mesmo dia" — ON/OFF.
# ---------------------------------------------------------------------


def test_mesmo_dia_desligado_nao_oferece_horario_hoje(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, online_booking_same_day_enabled=False, online_booking_min_lead_minutes=0)
    _branch, prof, svc = _setup_service_and_professional(c)
    today = datetime.now(timezone.utc).date()

    resp = c.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": today.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_mesmo_dia_ligado_oferece_horario_hoje_respeitando_antecedencia(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, online_booking_same_day_enabled=True, online_booking_min_lead_minutes=0)
    _branch, prof, svc = _setup_service_and_professional(c)
    today = datetime.now(timezone.utc).date()

    resp = c.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "professional_id": prof["id"], "date": today.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) > 0


# ---------------------------------------------------------------------
# Categoria → Serviço (Agendamento Online público).
# ---------------------------------------------------------------------


def _create_category(c, name: str) -> dict:
    resp = c.post("/api/v1/service-categories", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_service(c, name: str, *, category_id: str | None = None, allow_online_booking: bool = True) -> dict:
    payload = {
        "name": name,
        "default_duration_minutes": 30,
        "default_price": "50.00",
        "allow_online_booking": allow_online_booking,
    }
    if category_id is not None:
        payload["category_id"] = category_id
    resp = c.post("/api/v1/services", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_categoria_filtra_servicos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    tratamentos = _create_category(c, "Tratamentos")
    cortes = _create_category(c, "Cortes")
    _create_service(c, "Hidratação", category_id=tratamentos["id"])
    _create_service(c, "Corte Masculino", category_id=cortes["id"])

    resp = c.get(f"/api/v1/public/booking/{org['slug']}/services", params={"category_id": tratamentos["id"]})
    assert resp.status_code == 200, resp.text
    names = {s["name"] for s in resp.json()}
    assert names == {"Hidratação"}


def test_categoria_vazia_nao_aparece_nas_categorias_publicas(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    com_servico = _create_category(c, "Com Serviço")
    vazia = _create_category(c, "Vazia")
    _create_service(c, "Corte", category_id=com_servico["id"])

    resp = c.get(f"/api/v1/public/booking/{org['slug']}/categories")
    assert resp.status_code == 200, resp.text
    ids = {cat["id"] for cat in resp.json()}
    assert com_servico["id"] in ids
    assert vazia["id"] not in ids


def test_categoria_com_apenas_servico_indisponivel_online_nao_aparece(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    categoria = _create_category(c, "Só Indisponível")
    _create_service(c, "Serviço Interno", category_id=categoria["id"], allow_online_booking=False)

    resp = c.get(f"/api/v1/public/booking/{org['slug']}/categories")
    assert resp.status_code == 200, resp.text
    ids = {cat["id"] for cat in resp.json()}
    assert categoria["id"] not in ids


def test_servico_sem_categoria_aparece_como_outros_servicos(client_as, org_a_actor):
    """Estratégia de compatibilidade explícita do pedido: serviço sem
    categoria não pode simplesmente desaparecer do público."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _create_service(c, "Serviço Sem Categoria")

    resp = c.get(f"/api/v1/public/booking/{org['slug']}/categories")
    assert resp.status_code == 200, resp.text
    outros = [cat for cat in resp.json() if cat["id"] is None]
    assert len(outros) == 1
    assert outros[0]["name"] == "Outros serviços"

    services_resp = c.get(f"/api/v1/public/booking/{org['slug']}/services", params={"uncategorized": True})
    assert services_resp.status_code == 200, services_resp.text
    assert {s["name"] for s in services_resp.json()} == {"Serviço Sem Categoria"}
