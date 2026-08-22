"""Testes de `GET /clients/lookup`, `POST /clients` (com `clients.create`)
e `GET /services/lookup` — Etapa L, Bloco 1 ("acesso ao módulo != uso
operacional do dado", migration 0030). Cobre: um ator com SÓ a permissão
granular (sem `clients.view`/`services.view`) consegue pesquisar/criar
nos fluxos operacionais; um ator sem NENHUMA das duas (nem ampla nem
granular) continua recebendo 403; o schema enxuto de `/lookup` nunca
inclui campos administrativos (CPF/endereço/preço detalhado etc.)."""
import dataclasses


def _restricted(actor, *, permissions: set[str]):
    return dataclasses.replace(actor, permissions=frozenset(permissions))


# ---------------------------------------------------------------------
# GET /clients/lookup
# ---------------------------------------------------------------------


def test_clients_lookup_funciona_com_permissao_granular_sem_view_amplo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client = c.post("/api/v1/clients", json={"name": "Maria Cliente", "phone": "61988887777"}).json()

    restricted = _restricted(org_a_actor, permissions={"clients.lookup", "agenda.view_all"})
    resp = client_as(restricted).get("/api/v1/clients/lookup", params={"search": "Maria"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(item["id"] == client["id"] for item in body)
    # Schema enxuto — nunca CPF/endereço/histórico (isso é Ficha 360°,
    # exige `clients.view`, não `clients.lookup`).
    for item in body:
        assert set(item.keys()) == {"id", "name", "phone", "whatsapp"}


def test_clients_lookup_sem_nenhuma_permissao_recebe_403(client_as, org_a_actor):
    restricted = _restricted(org_a_actor, permissions={"agenda.view_all"})
    resp = client_as(restricted).get("/api/v1/clients/lookup")
    assert resp.status_code == 403


def test_clients_lookup_continua_funcionando_com_clients_view_amplo(client_as, org_a_actor):
    """Quem já tinha `clients.view` (a maioria dos roles de sistema)
    continua funcionando exatamente igual — a permissão granular só
    SOMA uma via de acesso, nunca substitui a ampla."""
    restricted = _restricted(org_a_actor, permissions={"clients.view"})
    resp = client_as(restricted).get("/api/v1/clients/lookup")
    assert resp.status_code == 200, resp.text


def test_clients_create_operacional_funciona_com_permissao_granular_sem_manage(client_as, org_a_actor):
    restricted = _restricted(org_a_actor, permissions={"clients.create", "agenda.view_all"})
    resp = client_as(restricted).post("/api/v1/clients", json={"name": "Cliente Novo", "phone": "61977776666"})
    assert resp.status_code == 201, resp.text


def test_clients_create_sem_nenhuma_permissao_recebe_403(client_as, org_a_actor):
    restricted = _restricted(org_a_actor, permissions={"clients.view", "agenda.view_all"})
    resp = client_as(restricted).post("/api/v1/clients", json={"name": "Cliente Novo", "phone": "61977776666"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# GET /services/lookup
# ---------------------------------------------------------------------


def test_services_lookup_funciona_com_permissao_granular_sem_view_amplo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 30, "default_price": "50.00"}
    ).json()

    restricted = _restricted(org_a_actor, permissions={"services.lookup", "agenda.view_all"})
    resp = client_as(restricted).get("/api/v1/services/lookup")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(item["id"] == service["id"] for item in body)
    for item in body:
        assert set(item.keys()) == {"id", "name", "default_duration_minutes", "default_price"}


def test_services_lookup_sem_nenhuma_permissao_recebe_403(client_as, org_a_actor):
    restricted = _restricted(org_a_actor, permissions={"agenda.view_all"})
    resp = client_as(restricted).get("/api/v1/services/lookup")
    assert resp.status_code == 403


def test_services_lookup_so_lista_servicos_ativos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    service = c.post(
        "/api/v1/services", json={"name": "Descartado", "default_duration_minutes": 30, "default_price": "50.00"}
    ).json()
    c.patch(f"/api/v1/services/{service['id']}/deactivate")

    restricted = _restricted(org_a_actor, permissions={"services.lookup"})
    resp = client_as(restricted).get("/api/v1/services/lookup")
    assert resp.status_code == 200, resp.text
    assert all(item["id"] != service["id"] for item in resp.json())


# ---------------------------------------------------------------------
# Migration 0030 — concessão de fábrica (só OWNER/ADMIN, nunca
# RECEPTIONIST/PROFESSIONAL "de brinde") — cobertura complementar à de
# `test_auth.py::test_permissions_efetivas_por_role`.
# ---------------------------------------------------------------------


def test_isolamento_multi_tenant_no_lookup_de_clientes(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    client_a = c_a.post("/api/v1/clients", json={"name": "Só da Org A", "phone": "61911112222"}).json()

    c_b = client_as(org_b_actor)
    resp = c_b.get("/api/v1/clients/lookup", params={"search": "Só da Org A"})
    assert resp.status_code == 200
    assert all(item["id"] != client_a["id"] for item in resp.json())
