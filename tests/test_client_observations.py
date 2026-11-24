"""Central de observações do cliente (geral + agendamento + comanda) —
agregação, auditoria com revisões, concorrência otimista e RBAC. Mesmo
padrão de fixtures HTTP de `test_clients.py`/`test_client_profile.py`
(`client_as`/`org_a_actor`/`org_b_actor`); alguns testes descem ao
service layer (`org_session` local, mesmo padrão de `test_appointments.py`)
quando o cenário exige controlar o `User` diretamente (nome histórico
preservado após renomear/desativar)."""
import uuid

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ForbiddenError, NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Organization
from nexasalon_api.services import client_observations


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org observations", slug=f"org-obs-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _user_actor(session, org_id, user: User, *, permissions) -> ActorContext:
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Teste", permissions=frozenset(permissions),
    )


_ALL_CLIENT_OBS_PERMS = frozenset(
    {"clients.view", "clients.manage", "clients.view_audit_log", "agenda.edit", "orders.view", "orders.manage"}
)


def _restricted_actor(base_actor: ActorContext, *, permissions) -> ActorContext:
    """Mesmo padrão de `test_client_profile.py::_restricted_actor` — um
    `User` de verdade (não só um `ActorContext` fabricado), necessário
    porque `services/client_observations.py` resolve
    `user_repo.get(session, actor.user_id).name` pra gravar o snapshot
    de autor."""
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Restrito")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Restrito", permissions=frozenset(permissions),
    )


def _setup_branch_professional_service(c):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    prof = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    svc = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    return branch, prof, svc


def _create_appointment(c, branch, prof, svc, client_id, start_at="2026-12-01T10:00:00-03:00"):
    return c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"],
            "client_id": client_id,
            "items": [{"professional_id": prof["id"], "service_id": svc["id"], "start_at": start_at}],
        },
    ).json()


def _open_cash_register(c, branch):
    c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"})


def _close_appointment_as_order(c, branch, appt):
    for target in ("confirmed", "finished"):
        c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target})
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    ).json()
    return closed


# ---------------------------------------------------------------------
# Agregação das 3 origens
# ---------------------------------------------------------------------


def test_lista_agrega_geral_agendamento_e_comanda_sem_duplicar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Central"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)

    c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Observação geral"})

    appt = _create_appointment(c, branch, prof, svc, client_id)
    c.patch(
        f"/api/v1/appointments/{appt['id']}/notes",
        json={"notes": "Observação da visita", "expected_notes_version": 0},
    )
    _open_cash_register(c, branch)
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.patch(
        f"/api/v1/orders/{order['id']}/observation",
        json={"observation": "Observação da comanda", "expected_observation_version": 0},
    )

    resp = c.get(f"/api/v1/clients/{client_id}/observations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    origins = {item["origin"] for item in body["items"]}
    assert origins == {"general", "appointment", "order"}
    contents = {item["origin"]: item["content"] for item in body["items"]}
    assert contents["general"] == "Observação geral"
    assert contents["appointment"] == "Observação da visita"
    assert contents["order"] == "Observação da comanda"
    order_card = next(i for i in body["items"] if i["origin"] == "order")
    assert order_card["order_number"] == order["order_number"]
    assert order_card["appointment_id"] == appt["id"]


def test_sem_observacao_nenhuma_lista_fica_vazia(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Vazio"}).json()["id"]
    resp = c.get(f"/api/v1/clients/{client_id}/observations")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_criar_multiplas_observacoes_gerais_independentes(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Multi"}).json()["id"]

    c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Primeira observação"})
    c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Segunda observação"})

    resp = c.get(f"/api/v1/clients/{client_id}/observations", params={"origin": "general"})
    body = resp.json()
    assert body["total"] == 2
    contents = {item["content"] for item in body["items"]}
    assert contents == {"Primeira observação", "Segunda observação"}


# ---------------------------------------------------------------------
# Edição / fonte única de verdade
# ---------------------------------------------------------------------


def test_editar_observacao_geral_atualiza_a_fonte_original(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Edição"}).json()["id"]
    created = c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Texto original"}).json()

    resp = c.patch(
        f"/api/v1/clients/{client_id}/observations/{created['id']}",
        json={"content": "Texto corrigido", "expected_version": 0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "Texto corrigido"
    assert resp.json()["version"] == 1

    listed = c.get(f"/api/v1/clients/{client_id}/observations").json()["items"][0]
    assert listed["content"] == "Texto corrigido"
    assert listed["updated_by_name"] is not None


def test_editar_notes_do_agendamento_aparece_na_lista_do_perfil(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Agenda"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id)

    resp = c.patch(
        f"/api/v1/appointments/{appt['id']}/notes",
        json={"notes": "Cliente prefere tesoura", "expected_notes_version": 0},
    )
    assert resp.status_code == 200, resp.text

    listed = c.get(f"/api/v1/clients/{client_id}/observations", params={"origin": "appointment"}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["content"] == "Cliente prefere tesoura"
    assert listed["items"][0]["id"] == appt["id"]


def test_editar_observacao_da_comanda_aparece_na_lista_do_perfil(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Comanda"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id)
    _open_cash_register(c, branch)
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    resp = c.patch(
        f"/api/v1/orders/{order['id']}/observation",
        json={"observation": "65cm - 200g - castanho escuro", "expected_observation_version": 0},
    )
    assert resp.status_code == 200, resp.text

    listed = c.get(f"/api/v1/clients/{client_id}/observations", params={"origin": "order"}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["content"] == "65cm - 200g - castanho escuro"
    assert listed["items"][0]["order_number"] == order["order_number"]


def test_comanda_finalizada_observacao_editavel_sem_efeitos_financeiros(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Fechado"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id)
    closed = _close_appointment_as_order(c, branch, appt)
    assert closed["status"] == "closed"

    resp = c.patch(
        f"/api/v1/orders/{closed['id']}/observation",
        json={"observation": "Observação pós-fechamento", "expected_observation_version": 0},
    )
    assert resp.status_code == 200, resp.text

    reloaded = c.get(f"/api/v1/orders/{closed['id']}").json()
    assert reloaded["status"] == "closed"
    assert reloaded["total"] == closed["total"]
    assert reloaded["observation"] == "Observação pós-fechamento"


# ---------------------------------------------------------------------
# Concorrência
# ---------------------------------------------------------------------


def test_concorrencia_na_observacao_geral_retorna_409(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Concorrente"}).json()["id"]
    created = c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "V0"}).json()

    ok = c.patch(
        f"/api/v1/clients/{client_id}/observations/{created['id']}",
        json={"content": "V1 (venceu)", "expected_version": 0},
    )
    assert ok.status_code == 200

    stale = c.patch(
        f"/api/v1/clients/{client_id}/observations/{created['id']}",
        json={"content": "V1 (perdeu)", "expected_version": 0},
    )
    assert stale.status_code == 409, stale.text
    assert "alterada por outro" in stale.json()["error"]["message"].lower()


def test_concorrencia_no_agendamento_retorna_409(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Concorrente Agenda"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id)

    ok = c.patch(f"/api/v1/appointments/{appt['id']}/notes", json={"notes": "V1", "expected_notes_version": 0})
    assert ok.status_code == 200

    stale = c.patch(f"/api/v1/appointments/{appt['id']}/notes", json={"notes": "V2", "expected_notes_version": 0})
    assert stale.status_code == 409, stale.text


def test_alteracao_sem_mudanca_nao_gera_revisao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Sem Mudança"}).json()["id"]
    created = c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Mesmo texto"}).json()

    resp = c.patch(
        f"/api/v1/clients/{client_id}/observations/{created['id']}",
        json={"content": "Mesmo texto", "expected_version": 0},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 0  # nunca incrementa sem mudança real

    history = c.get(
        f"/api/v1/clients/{client_id}/observations/history",
        params={"origin": "general", "origin_id": created["id"]},
    ).json()
    # só a criação aparece — a "edição" sem mudança não virou revisão nova.
    assert len(history["items"]) == 1
    assert history["items"][0]["action"] == "create"


# ---------------------------------------------------------------------
# Histórico de auditoria
# ---------------------------------------------------------------------


def test_historico_mostra_conteudo_anterior_e_novo_mais_recente_primeiro(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Histórico"}).json()["id"]
    created = c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "65cm - 200g"}).json()
    c.patch(
        f"/api/v1/clients/{client_id}/observations/{created['id']}",
        json={"content": "65cm - 250g", "expected_version": 0},
    )

    history = c.get(
        f"/api/v1/clients/{client_id}/observations/history",
        params={"origin": "general", "origin_id": created["id"]},
    ).json()
    assert len(history["items"]) == 2
    assert history["items"][0]["action"] == "update"
    assert history["items"][0]["old_content"] == "65cm - 200g"
    assert history["items"][0]["new_content"] == "65cm - 250g"
    assert history["items"][1]["action"] == "create"
    assert history["items"][1]["new_content"] == "65cm - 200g"


def test_historico_do_agendamento_nunca_mistura_outros_eventos_de_auditoria(client_as, org_a_actor):
    """`entity_type="appointment"` recebe `AuditLog` de QUALQUER mudança
    do agendamento (ex. status) — o histórico da observação precisa
    filtrar só os eventos de `notes`, nunca misturar com outros."""
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Sem Mistura"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id)
    c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": "confirmed"})
    c.patch(f"/api/v1/appointments/{appt['id']}/notes", json={"notes": "Nota única", "expected_notes_version": 0})

    history = c.get(
        f"/api/v1/clients/{client_id}/observations/history",
        params={"origin": "appointment", "origin_id": appt["id"]},
    ).json()
    assert len(history["items"]) == 1
    assert history["items"][0]["new_content"] == "Nota única"


# ---------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------


def test_sem_clients_manage_nao_pode_criar_observacao_geral(client_as, org_a_actor):
    owner = client_as(org_a_actor)
    client_id = owner.post("/api/v1/clients", json={"name": "Cliente RBAC"}).json()["id"]

    restricted = _restricted_actor(org_a_actor, permissions={"clients.view"})
    c = client_as(restricted)
    resp = c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Tentativa"})
    assert resp.status_code == 403


def test_sem_clients_view_audit_log_nao_ve_historico(client_as, org_a_actor):
    owner = client_as(org_a_actor)
    client_id = owner.post("/api/v1/clients", json={"name": "Cliente Sem Histórico"}).json()["id"]
    created = owner.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Texto"}).json()

    restricted = _restricted_actor(org_a_actor, permissions={"clients.view", "clients.manage"})
    c = client_as(restricted)
    resp = c.get(
        f"/api/v1/clients/{client_id}/observations/history",
        params={"origin": "general", "origin_id": created["id"]},
    )
    assert resp.status_code == 403


def test_recepcionista_com_agenda_edit_mas_sem_orders_view_nao_ve_cartao_de_comanda(client_as, org_a_actor):
    owner = client_as(org_a_actor)
    client_id = owner.post("/api/v1/clients", json={"name": "Cliente Escopo"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(owner)
    appt = _create_appointment(owner, branch, prof, svc, client_id)
    owner.patch(f"/api/v1/appointments/{appt['id']}/notes", json={"notes": "Nota", "expected_notes_version": 0})
    _open_cash_register(owner, branch)
    order = owner.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    owner.patch(
        f"/api/v1/orders/{order['id']}/observation",
        json={"observation": "Obs comanda", "expected_observation_version": 0},
    )

    restricted = _restricted_actor(
        org_a_actor, permissions={"clients.view", "clients.manage", "agenda.edit", "agenda.view_all"}
    )
    c = client_as(restricted)
    body = c.get(f"/api/v1/clients/{client_id}/observations").json()
    origins = {item["origin"] for item in body["items"]}
    assert origins == {"appointment"}  # comanda nunca aparece sem orders.view


def test_usuario_somente_leitura_nao_pode_editar_mas_ve_a_lista(client_as, org_a_actor):
    owner = client_as(org_a_actor)
    client_id = owner.post("/api/v1/clients", json={"name": "Cliente Leitura"}).json()["id"]
    created = owner.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Texto"}).json()

    restricted = _restricted_actor(org_a_actor, permissions={"clients.view"})
    c = client_as(restricted)
    body = c.get(f"/api/v1/clients/{client_id}/observations").json()
    assert body["items"][0]["can_edit"] is False

    resp = c.patch(
        f"/api/v1/clients/{client_id}/observations/{created['id']}",
        json={"content": "Tentativa", "expected_version": 0},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# Isolamento multi-tenant
# ---------------------------------------------------------------------


def test_isolamento_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    a = client_as(org_a_actor)
    client_id = a.post("/api/v1/clients", json={"name": "Cliente da Org A"}).json()["id"]
    a.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Confidencial da Org A"})

    b = client_as(org_b_actor)
    resp = b.get(f"/api/v1/clients/{client_id}/observations")
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# Filtros, busca e paginação
# ---------------------------------------------------------------------


def test_filtro_por_origem_e_busca_por_texto(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Filtro"}).json()["id"]
    c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Alergia a amônia"})
    c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Prefere água morna"})

    resp = c.get(f"/api/v1/clients/{client_id}/observations", params={"search": "amônia"})
    body = resp.json()
    assert body["total"] == 1
    assert "amônia" in body["items"][0]["content"]


def test_paginacao_respeita_page_e_page_size(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Paginado"}).json()["id"]
    for i in range(5):
        c.post(f"/api/v1/clients/{client_id}/observations", json={"content": f"Observação {i}"})

    page1 = c.get(f"/api/v1/clients/{client_id}/observations", params={"page": 1, "page_size": 2}).json()
    page2 = c.get(f"/api/v1/clients/{client_id}/observations", params={"page": 2, "page_size": 2}).json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    ids_page1 = {i["id"] for i in page1["items"]}
    ids_page2 = {i["id"] for i in page2["items"]}
    assert ids_page1.isdisjoint(ids_page2)


# ---------------------------------------------------------------------
# Clientes arquivados
# ---------------------------------------------------------------------


def test_cliente_arquivado_preserva_observacoes(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Arquivado"}).json()["id"]
    c.post(f"/api/v1/clients/{client_id}/observations", json={"content": "Antes de arquivar"})
    c.patch(f"/api/v1/clients/{client_id}/deactivate")

    resp = c.get(f"/api/v1/clients/{client_id}/observations")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["content"] == "Antes de arquivar"


# ---------------------------------------------------------------------
# Autor nunca vem do payload do frontend
# ---------------------------------------------------------------------


def test_autor_e_sempre_o_usuario_autenticado_nunca_o_payload(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Autor"}).json()["id"]
    # mesmo enviando campos extras, o schema (`ObservationCreate`) só
    # aceita `content` — não existe como o frontend "informar" o autor.
    resp = c.post(
        f"/api/v1/clients/{client_id}/observations",
        json={"content": "Texto", "created_by": "00000000-0000-0000-0000-000000000000", "created_by_name": "Hacker"},
    )
    assert resp.status_code == 201
    assert resp.json()["created_by_name"] != "Hacker"


# ---------------------------------------------------------------------
# Nome histórico preservado (service layer — precisa controlar o User)
# ---------------------------------------------------------------------


def test_nome_historico_preservado_apos_renomear_usuario(org_session):
    session, org_id = org_session
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Nome Original")
    session.add(user)
    client = Client(organization_id=org_id, name="Cliente Renomeado")
    session.add(client)
    session.flush()
    actor = _user_actor(session, org_id, user, permissions=_ALL_CLIENT_OBS_PERMS)

    observation = client_observations.create_general_observation(session, actor, client.id, "Texto original")
    session.flush()
    assert observation.created_by_name == "Nome Original"

    user.name = "Nome Novo"
    session.flush()

    # recarrega — o snapshot já gravado nunca muda retroativamente.
    reloaded = client_observations.list_observations(session, actor, client.id)
    assert reloaded.items[0].created_by_name == "Nome Original"

    history = client_observations.get_observation_history(session, actor, client.id, "general", observation.id)
    assert history[0].user_name == "Nome Original"


def test_nome_historico_preservado_apos_desativar_usuario(org_session):
    session, org_id = org_session
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Profissional Desativado")
    session.add(user)
    client = Client(organization_id=org_id, name="Cliente Com Usuário Desativado")
    session.add(client)
    session.flush()
    actor = _user_actor(session, org_id, user, permissions=_ALL_CLIENT_OBS_PERMS)

    client_observations.create_general_observation(session, actor, client.id, "Texto")
    session.flush()

    user.is_active = False
    session.flush()

    reloaded = client_observations.list_observations(session, actor, client.id)
    assert reloaded.items[0].created_by_name == "Profissional Desativado"


def test_edicao_sem_permissao_nenhuma_e_bloqueada_no_service(org_session):
    session, org_id = org_session
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Sem Permissão")
    session.add(user)
    client = Client(organization_id=org_id, name="Cliente Service")
    session.add(client)
    session.flush()
    owner = _user_actor(session, org_id, user, permissions=_ALL_CLIENT_OBS_PERMS)
    observation = client_observations.create_general_observation(session, owner, client.id, "Texto")
    session.flush()

    restricted = _user_actor(session, org_id, user, permissions=frozenset({"clients.view"}))
    with pytest.raises(ForbiddenError):
        client_observations.get_observation_history(session, restricted, client.id, "general", observation.id)


def test_historico_de_observacao_de_outro_cliente_gera_404(org_session):
    session, org_id = org_session
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário")
    session.add(user)
    client_a = Client(organization_id=org_id, name="Cliente A")
    client_b = Client(organization_id=org_id, name="Cliente B")
    session.add_all([client_a, client_b])
    session.flush()
    actor = _user_actor(session, org_id, user, permissions=_ALL_CLIENT_OBS_PERMS)
    observation = client_observations.create_general_observation(session, actor, client_a.id, "Texto")
    session.flush()

    with pytest.raises(NotFoundError):
        client_observations.get_observation_history(session, actor, client_b.id, "general", observation.id)


def test_backfill_de_notes_legado_vira_observacao_importada_sem_autor_nem_data(org_session):
    """Reproduz exatamente a lógica de backfill da migration 0058 —
    `Client.notes` legado vira UMA observação geral com
    `is_legacy_import=True`, `created_at`/`created_by` desconhecidos
    (nunca inventados)."""
    session, org_id = org_session
    client = Client(organization_id=org_id, name="Cliente Legado", notes="  Observação legada antiga  ")
    session.add(client)
    session.flush()

    session.execute(
        text(
            "INSERT INTO client_observations (organization_id, client_id, content, version, is_legacy_import) "
            "SELECT organization_id, id, TRIM(notes), 0, true FROM clients "
            "WHERE id = :cid AND notes IS NOT NULL AND TRIM(notes) <> ''"
        ),
        {"cid": str(client.id)},
    )
    session.flush()

    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Qualquer Um")
    session.add(user)
    session.flush()
    actor = _user_actor(session, org_id, user, permissions=_ALL_CLIENT_OBS_PERMS)

    result = client_observations.list_observations(session, actor, client.id)
    assert result.total == 1
    legacy = result.items[0]
    assert legacy.origin == "general"
    assert legacy.content == "Observação legada antiga"
    assert legacy.is_legacy_import is True
    assert legacy.created_at is None
    assert legacy.created_by_name is None
