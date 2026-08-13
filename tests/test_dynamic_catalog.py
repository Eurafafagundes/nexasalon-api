"""Revisão de domínio (pré Etapa 3B) — prova que NENHUM profissional,
serviço, categoria ou regra de agenda precisa ser hardcoded: cada
organização cria seu próprio catálogo, e a Agenda principal monta suas
colunas 100% a partir do banco (is_active/has_schedule/
show_on_main_schedule + display_order), nunca de uma lista fixa."""
import uuid


def _create_branch(c, name="Matriz"):
    return c.post("/api/v1/branches", json={"name": name, "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()


def _create_category(c, name, **overrides):
    payload = {"name": name, **overrides}
    resp = c.post("/api/v1/service-categories", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_service(c, name, **overrides):
    payload = {"name": name, "default_duration_minutes": 30, "default_price": "50.00", **overrides}
    resp = c.post("/api/v1/services", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_professional(c, name, **overrides):
    payload = {"name": name, **overrides}
    resp = c.post("/api/v1/professionals", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _link_service(c, professional_id, service_id, **overrides):
    resp = c.put(
        f"/api/v1/professionals/{professional_id}/services",
        json={"items": [{"service_id": service_id, **overrides}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 1. cada organização cria seus próprios serviços/categorias -----------


def test_organizacao_cria_servicos_e_categorias_proprios(client_as, org_a_actor):
    c = client_as(org_a_actor)
    categoria = _create_category(c, "Categoria de teste A", color="#112233", display_order=1)
    servico = _create_service(
        c, "Serviço de teste A",
        category_id=categoria["id"], allow_online_booking=False, display_order=2,
        requires_deposit=True, deposit_type="percentage", deposit_value="20.00",
        buffer_before_minutes=5, buffer_after_minutes=10,
    )
    assert servico["category_id"] == categoria["id"]
    assert servico["allow_online_booking"] is False
    assert servico["display_order"] == 2
    assert servico["requires_deposit"] is True
    assert servico["deposit_type"] == "percentage"
    assert servico["buffer_before_minutes"] == 5
    assert servico["buffer_after_minutes"] == 10


def test_deposito_exige_type_e_value_juntos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/services",
        json={
            "name": "Serviço com sinal incompleto", "default_duration_minutes": 30, "default_price": "50.00",
            "requires_deposit": True,
        },
    )
    assert resp.status_code == 422, resp.text


def test_category_id_deve_pertencer_a_mesma_organizacao(client_as, org_a_actor, org_b_actor):
    categoria_b = _create_category(client_as(org_b_actor), "Categoria da org B")
    resp = client_as(org_a_actor).post(
        "/api/v1/services",
        json={
            "name": "Serviço A com categoria alheia", "default_duration_minutes": 30,
            "default_price": "50.00", "category_id": categoria_b["id"],
        },
    )
    assert resp.status_code == 422, resp.text


# --- 2/9/10. isolamento multi-tenant do catálogo ---------------------------


def test_catalogo_completamente_diferente_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    # IMPORTANTE: nunca guardar dois `client_as(...)` simultâneos — o
    # dependency_override fica no `app` compartilhado, então o último
    # `client_as(...)` chamado vale pra QUALQUER referência anterior
    # (ver docstring do fixture em conftest.py). Por isso cada bloco
    # cria e usa seu client na hora, sem reter o anterior.
    _create_service(client_as(org_a_actor), "Serviço exclusivo A")
    _create_service(client_as(org_b_actor), "Serviço exclusivo B")

    nomes_a = {s["name"] for s in client_as(org_a_actor).get("/api/v1/services").json()}
    nomes_b = {s["name"] for s in client_as(org_b_actor).get("/api/v1/services").json()}
    assert "Serviço exclusivo A" in nomes_a and "Serviço exclusivo B" not in nomes_a
    assert "Serviço exclusivo B" in nomes_b and "Serviço exclusivo A" not in nomes_b


def test_categorias_isoladas_por_organizacao(client_as, org_a_actor, org_b_actor):
    _create_category(client_as(org_a_actor), "Categoria A exclusiva")
    _create_category(client_as(org_b_actor), "Categoria B exclusiva")

    nomes_a = {cat["name"] for cat in client_as(org_a_actor).get("/api/v1/service-categories").json()}
    nomes_b = {cat["name"] for cat in client_as(org_b_actor).get("/api/v1/service-categories").json()}
    assert "Categoria A exclusiva" in nomes_a and "Categoria B exclusiva" not in nomes_a
    assert "Categoria B exclusiva" in nomes_b and "Categoria A exclusiva" not in nomes_b


def test_nenhum_dado_de_catalogo_vaza_via_get_direto(client_as, org_a_actor, org_b_actor):
    """Além de não aparecer nas listagens, a org B não consegue nem
    buscar por ID um serviço/categoria/profissional da org A (404, RLS +
    filtro explícito por organization_id)."""
    c_a = client_as(org_a_actor)
    categoria_a = _create_category(c_a, "Categoria só da A")
    servico_a = _create_service(c_a, "Serviço só da A")
    profissional_a = _create_professional(c_a, "Profissional só da A")

    c_b = client_as(org_b_actor)
    assert c_b.get(f"/api/v1/service-categories/{categoria_a['id']}").status_code == 404
    assert c_b.get(f"/api/v1/services/{servico_a['id']}").status_code == 404
    assert c_b.get(f"/api/v1/professionals/{profissional_a['id']}").status_code == 404


# --- 3. profissionais diferentes executam serviços diferentes -------------


def test_profissionais_diferentes_executam_servicos_diferentes(client_as, org_a_actor):
    c = client_as(org_a_actor)
    corte = _create_service(c, "Serviço capilar")
    sobrancelha = _create_service(c, "Serviço de sobrancelha")
    prof_cabelo = _create_professional(c, "Especialista em cabelo")
    prof_sobrancelha = _create_professional(c, "Especialista em sobrancelha")
    _link_service(c, prof_cabelo["id"], corte["id"])
    _link_service(c, prof_sobrancelha["id"], sobrancelha["id"])

    servicos_prof_cabelo = c.get(f"/api/v1/professionals/{prof_cabelo['id']}/services").json()
    servicos_prof_sobrancelha = c.get(f"/api/v1/professionals/{prof_sobrancelha['id']}/services").json()
    assert {s["service_id"] for s in servicos_prof_cabelo} == {corte["id"]}
    assert {s["service_id"] for s in servicos_prof_sobrancelha} == {sobrancelha["id"]}

    profissionais_de_corte = c.get(f"/api/v1/services/{corte['id']}/professionals").json()
    assert {p["professional_id"] for p in profissionais_de_corte} == {prof_cabelo["id"]}


# --- 4/5/6/agenda dinâmica --------------------------------------------------


def test_profissional_sem_has_schedule_nao_aparece_na_agenda(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    gerente = _create_professional(c, "Gerente sem agenda", branch_id=branch["id"], has_schedule=False)
    atendente = _create_professional(c, "Atendente com agenda", branch_id=branch["id"], has_schedule=True)

    colunas = c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()
    ids = {p["id"] for p in colunas}
    assert gerente["id"] not in ids
    assert atendente["id"] in ids


def test_profissional_show_on_main_schedule_false_nao_aparece_na_agenda_principal(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    oculto = _create_professional(
        c, "Profissional oculto da grade principal", branch_id=branch["id"],
        has_schedule=True, show_on_main_schedule=False,
    )
    visivel = _create_professional(
        c, "Profissional visível na grade principal", branch_id=branch["id"],
        has_schedule=True, show_on_main_schedule=True,
    )

    colunas = c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()
    ids = {p["id"] for p in colunas}
    assert oculto["id"] not in ids
    assert visivel["id"] in ids


def test_profissional_novo_aparece_dinamicamente_sem_alteracao_de_codigo(client_as, org_a_actor):
    """Nada no backend precisa mudar: basta cadastrar e habilitar as
    flags — a query já busca dinamicamente do banco."""
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    antes = c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()
    assert antes == []

    novo = _create_professional(c, "Recém-cadastrado", branch_id=branch["id"])  # flags default = True
    depois = c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()
    assert {p["id"] for p in depois} == {novo["id"]}


def test_display_order_determina_a_ordem_das_colunas(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    terceiro = _create_professional(c, "Terceiro", branch_id=branch["id"], display_order=2)
    primeiro = _create_professional(c, "Primeiro", branch_id=branch["id"], display_order=0)
    segundo = _create_professional(c, "Segundo", branch_id=branch["id"], display_order=1)

    colunas = c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()
    ids_em_ordem = [p["id"] for p in colunas]
    assert ids_em_ordem == [primeiro["id"], segundo["id"], terceiro["id"]]


def test_professional_desativado_tambem_some_da_agenda(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Vai ser desativado", branch_id=branch["id"])
    assert prof["id"] in {p["id"] for p in c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()}

    c.patch(f"/api/v1/professionals/{prof['id']}/deactivate")
    colunas = c.get("/api/v1/agenda/professionals", params={"branch_id": branch["id"]}).json()
    assert prof["id"] not in {p["id"] for p in colunas}


# --- 7/8. serviço/vínculo inativo não pode ser oferecido/agendado ---------


def test_servico_inativo_nao_aparece_na_listagem_padrao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    servico = _create_service(c, "Serviço a ser desativado")
    c.patch(f"/api/v1/services/{servico['id']}/deactivate")

    ativos = c.get("/api/v1/services").json()
    assert servico["id"] not in {s["id"] for s in ativos}
    todos = c.get("/api/v1/services", params={"include_inactive": True}).json()
    assert servico["id"] in {s["id"] for s in todos}


def test_servico_inativo_nao_pode_ser_agendado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Profissional", branch_id=branch["id"])
    servico = _create_service(c, "Serviço que será desativado")
    _link_service(c, prof["id"], servico["id"])
    c.patch(f"/api/v1/services/{servico['id']}/deactivate")
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )
    cliente = c.post("/api/v1/clients", json={"name": "Cliente"}).json()

    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": cliente["id"],
            "items": [
                {"professional_id": prof["id"], "service_id": servico["id"], "start_at": "2026-08-13T14:00:00-03:00"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text


def test_professional_service_inativo_impede_agendamento(client_as, org_a_actor):
    """O serviço e o profissional continuam ativos — só o VÍNCULO entre
    eles é que foi desativado (`is_active=False` no ProfessionalService).
    """
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Profissional", branch_id=branch["id"])
    servico = _create_service(c, "Serviço")
    _link_service(c, prof["id"], servico["id"], is_active=False)
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )
    cliente = c.post("/api/v1/clients", json={"name": "Cliente"}).json()

    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": cliente["id"],
            "items": [
                {"professional_id": prof["id"], "service_id": servico["id"], "start_at": "2026-08-13T14:00:00-03:00"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text


def test_sem_vinculo_professional_service_impede_agendamento(client_as, org_a_actor):
    """Nem precisa desativar nada: se o vínculo simplesmente não existe,
    o profissional não presta aquele serviço — ponto central do
    princípio "nenhum serviço disponível pra todo mundo por padrão"."""
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Profissional sem vínculo", branch_id=branch["id"])
    servico = _create_service(c, "Serviço sem vínculo")
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )
    cliente = c.post("/api/v1/clients", json={"name": "Cliente"}).json()

    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": cliente["id"],
            "items": [
                {"professional_id": prof["id"], "service_id": servico["id"], "start_at": "2026-08-13T14:00:00-03:00"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text


# --- organização: business_type é só metadado, sem regra de negócio -------


def test_business_type_nao_limita_cadastro_de_servicos(client_as, org_a_actor):
    """O sistema nunca deve impedir uma organização de cadastrar um
    serviço "fora do padrão" pro seu tipo de negócio — não há checagem
    de `business_type` em nenhum lugar do fluxo de criação de serviço."""
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    assert "business_type" in org  # exposto, mesmo sem endpoint de escrita ainda

    barbearia_cadastrando_estetica = _create_service(c, "Serviço de estética numa barbearia qualquer")
    assert barbearia_cadastrando_estetica["is_active"] is True
