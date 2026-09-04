"""Testes de `clients.view_contact_data` — privacidade de dados de
contato do cliente (telefone/WhatsApp/CPF/e-mail/endereço). Problema
real de negócio: um funcionário com acesso à própria Agenda já usou o
telefone de uma cliente pra oferecer serviço por fora. Toda resposta que
pode devolver um `Client` (lookup, detalhe, listagem, criação, edição,
Ficha 360°) passa pelo MESMO helper centralizado
(`core/client_privacy.py::apply_client_contact_masking`) — estes testes
cobrem esse contrato ponta a ponta via HTTP real, nunca testando o
helper isolado."""
import dataclasses


def _restricted(actor, *, permissions: set[str]):
    return dataclasses.replace(actor, permissions=frozenset(permissions))


_FULL_CONTACT = {"clients.view", "clients.manage", "clients.lookup", "clients.create", "clients.view_contact_data"}
_NO_CONTACT = {"clients.view", "clients.manage", "clients.lookup", "clients.create"}


def _create_full_client(c):
    return c.post(
        "/api/v1/clients",
        json={
            "name": "Maria Cliente",
            "phone": "61988887777",
            "whatsapp": "61988887777",
            "email": "maria@example.com",
            "cpf": "11144477735",
            "cep": "70000000",
            "state": "DF",
            "city": "Brasília",
            "neighborhood": "Asa Sul",
            "address_line": "Rua das Flores",
            "address_number": "10",
        },
    ).json()


def test_get_client_sem_permission_mascara_telefone_whatsapp_cpf_email_endereco(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = _create_full_client(c)

    restricted = client_as(_restricted(org_a_actor, permissions=_NO_CONTACT))
    resp = restricted.get(f"/api/v1/clients/{created['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["name"] == "Maria Cliente"  # nome NUNCA é mascarado
    assert body["phone"] == "(**) *****-7777"
    assert body["whatsapp"] == "(**) *****-7777"
    assert body["cpf"] == "***.***.***-**"
    assert body["email"] == "Oculto"
    assert body["cep"] is None
    assert body["state"] is None
    assert body["city"] is None
    assert body["neighborhood"] is None
    assert body["address_line"] is None
    assert body["address_number"] is None
    assert body["can_view_contact_data"] is False
    # Nenhum resquício do dado real na resposta, em nenhum campo.
    raw = resp.text
    assert "61988887777" not in raw
    assert "11144477735" not in raw
    assert "maria@example.com" not in raw
    assert "Rua das Flores" not in raw


def test_get_client_com_permission_traz_dados_completos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = _create_full_client(c)

    full = client_as(_restricted(org_a_actor, permissions=_FULL_CONTACT))
    resp = full.get(f"/api/v1/clients/{created['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phone"] == "61988887777"
    assert body["cpf"] == "11144477735"
    assert body["email"] == "maria@example.com"
    assert body["address_line"] == "Rua das Flores"
    assert body["can_view_contact_data"] is True


def test_list_clients_mascara_sem_permission_de_contato(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _create_full_client(c)

    restricted = client_as(_restricted(org_a_actor, permissions=_NO_CONTACT))
    resp = restricted.get("/api/v1/clients")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["phone"] == "(**) *****-7777"
    assert body[0]["can_view_contact_data"] is False


def test_lookup_mascara_sem_permission_de_contato(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = _create_full_client(c)

    restricted = client_as(_restricted(org_a_actor, permissions={"clients.lookup"}))
    resp = restricted.get("/api/v1/clients/lookup", params={"search": "Maria"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    found = next(item for item in body if item["id"] == created["id"])
    assert found["phone"] == "(**) *****-7777"
    assert found["whatsapp"] == "(**) *****-7777"
    assert found["can_view_contact_data"] is False
    assert set(found.keys()) == {"id", "name", "phone", "whatsapp", "can_view_contact_data"}


def test_busca_por_nome_funciona_mesmo_sem_permission_de_contato(client_as, org_a_actor):
    c = client_as(org_a_actor)
    c.post("/api/v1/clients", json={"name": "Fernanda Lima", "phone": "61966665555"})

    restricted = client_as(_restricted(org_a_actor, permissions={"clients.lookup"}))
    resp = restricted.get("/api/v1/clients/lookup", params={"search": "Fernanda"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(item["name"] == "Fernanda Lima" for item in body)


def test_busca_por_telefone_funciona_mas_resposta_nunca_revela_o_numero_completo(client_as, org_a_actor):
    """Item explícito do pedido: aceitar o termo digitado (nome,
    telefone ou CPF) na busca continua funcionando no backend — só a
    RESPOSTA nunca revela o dado completo, pra nunca virar um endpoint
    de enumeração (adivinhar um telefone testando prefixos)."""
    c = client_as(org_a_actor)
    created = c.post("/api/v1/clients", json={"name": "Cliente X", "phone": "61955554444"}).json()

    restricted = client_as(_restricted(org_a_actor, permissions={"clients.lookup"}))
    resp = restricted.get("/api/v1/clients/lookup", params={"search": "61955554444"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(item["id"] == created["id"] for item in body)
    assert all("61955554444" not in (item["phone"] or "") for item in body)


def test_create_client_sem_permission_de_contato_continua_funcionando_mas_resposta_ja_sai_mascarada(client_as, org_a_actor):
    restricted = client_as(_restricted(org_a_actor, permissions={"clients.create"}))
    resp = restricted.post("/api/v1/clients", json={"name": "Nova Cliente", "phone": "61977776666"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Nova Cliente"
    assert body["phone"] == "(**) *****-6666"
    assert body["can_view_contact_data"] is False
    assert "61977776666" not in resp.text


def test_cliente_recem_criado_continua_mascarado_no_proximo_get(client_as, org_a_actor):
    """Cadastrar cliente NÃO concede acesso aos dados completos — vale
    até pra cliente que o próprio ator acabou de cadastrar."""
    restricted = client_as(_restricted(org_a_actor, permissions={"clients.create", "clients.view"}))
    created = restricted.post("/api/v1/clients", json={"name": "Nova Cliente", "phone": "61977776666"}).json()

    resp = restricted.get(f"/api/v1/clients/{created['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "(**) *****-6666"


def test_update_client_sem_permission_de_contato_ignora_campos_de_contato_do_payload(client_as, org_a_actor):
    """Bug real evitado: se o frontend mandar de volta o PLACEHOLDER
    mascarado (o que aconteceria sem a trava do formulário de edição),
    o backend precisa ignorar esses campos — nunca gravar o placeholder
    como se fosse o dado real."""
    c = client_as(org_a_actor)
    created = c.post(
        "/api/v1/clients", json={"name": "Cliente Original", "phone": "61944443333", "email": "original@example.com"}
    ).json()

    restricted = client_as(_restricted(org_a_actor, permissions={"clients.manage", "clients.view"}))
    resp = restricted.put(
        f"/api/v1/clients/{created['id']}",
        json={"name": "Cliente Renomeado", "phone": "(**) *****-3333", "email": "Oculto"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Cliente Renomeado"

    full = client_as(org_a_actor)
    real = full.get(f"/api/v1/clients/{created['id']}").json()
    assert real["phone"] == "61944443333"
    assert real["email"] == "original@example.com"


def test_update_client_com_permission_de_contato_altera_normalmente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = c.post("/api/v1/clients", json={"name": "Cliente", "phone": "61944443333"}).json()

    full = client_as(_restricted(org_a_actor, permissions=_FULL_CONTACT))
    resp = full.put(f"/api/v1/clients/{created['id']}", json={"name": "Cliente", "phone": "61933332222"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "61933332222"


def test_ficha_360_mascara_dados_de_contato_do_cliente_embutido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = _create_full_client(c)

    restricted = client_as(_restricted(org_a_actor, permissions=_NO_CONTACT))
    resp = restricted.get(f"/api/v1/clients/{created['id']}/profile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client"]["phone"] == "(**) *****-7777"
    assert body["client"]["can_view_contact_data"] is False


def test_isolamento_multi_tenant_no_mascaramento(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    created = _create_full_client(c_a)

    # org_b_actor tem TODAS as permissions (inclusive `clients.view_contact_data`)
    # na PRÓPRIA organização, mas isso nunca importa aqui — o isolamento
    # de tenant (organization_id) já bloqueia o acesso antes mesmo de
    # chegar no mascaramento.
    resp_b = client_as(org_b_actor).get(f"/api/v1/clients/{created['id']}")
    assert resp_b.status_code == 404


def test_master_mantem_acesso_a_dados_completos(client_as, org_a_actor):
    """`org_a_actor` é o Usuário Master de fábrica (todas as permissions
    do catálogo, ver conftest.py::seed_organization) — inclui
    `clients.view_contact_data` automaticamente, sem nenhuma concessão
    extra."""
    c = client_as(org_a_actor)
    created = _create_full_client(c)

    resp = c.get(f"/api/v1/clients/{created['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phone"] == "61988887777"
    assert body["can_view_contact_data"] is True
