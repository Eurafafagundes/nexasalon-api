def test_crud_client(client_as, org_a_actor):
    c = client_as(org_a_actor)

    resp = c.post(
        "/api/v1/clients",
        json={"name": "Maria Souza", "phone": "11999990000", "email": "maria@example.com"},
    )
    assert resp.status_code == 201, resp.text
    client_obj = resp.json()
    assert client_obj["is_active"] is True
    client_id = client_obj["id"]

    resp = c.get(f"/api/v1/clients/{client_id}")
    assert resp.status_code == 200

    resp = c.put(
        f"/api/v1/clients/{client_id}",
        json={"name": "Maria S. Souza", "phone": "11999990000"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Maria S. Souza"


def test_buscar_por_nome_e_telefone(client_as, org_a_actor):
    c = client_as(org_a_actor)
    c.post("/api/v1/clients", json={"name": "Maria Souza", "phone": "11999990000"})
    c.post("/api/v1/clients", json={"name": "João Pereira", "phone": "11988887777"})

    resp = c.get("/api/v1/clients", params={"search": "Maria"})
    assert resp.status_code == 200
    names = [cl["name"] for cl in resp.json()]
    assert "Maria Souza" in names
    assert "João Pereira" not in names

    resp = c.get("/api/v1/clients", params={"search": "999990000"})
    assert any(cl["phone"] == "11999990000" for cl in resp.json())


def test_busca_em_campo_unico_tambem_encontra_por_cpf(client_as, org_a_actor):
    """Item "busca de cliente por nome/telefone/CPF num único campo,
    sem seletor" — digitar um CPF (formatado ou só dígitos) encontra o
    cliente pelo mesmo parâmetro `search`."""
    c = client_as(org_a_actor)
    c.post("/api/v1/clients", json={"name": "Cliente CPF", "cpf": "111.444.777-35"})
    c.post("/api/v1/clients", json={"name": "Outro Cliente"})

    resp = c.get("/api/v1/clients", params={"search": "111.444.777-35"})
    assert resp.status_code == 200
    names = [cl["name"] for cl in resp.json()]
    assert names == ["Cliente CPF"]

    resp = c.get("/api/v1/clients", params={"search": "11144477735"})
    assert [cl["name"] for cl in resp.json()] == ["Cliente CPF"]


def test_desativar_cliente_nao_apaga(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Temporário"}).json()["id"]

    resp = c.patch(f"/api/v1/clients/{client_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = c.get(f"/api/v1/clients/{client_id}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    assert client_id not in [cl["id"] for cl in c.get("/api/v1/clients").json()]
    assert client_id in [cl["id"] for cl in c.get("/api/v1/clients?include_inactive=true").json()]

    resp = c.patch(f"/api/v1/clients/{client_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# ---------------------------------------------------------------------
# Normalização de telefone/CPF (item "pense na qualidade dos dados")
# ---------------------------------------------------------------------


def test_telefone_normalizado_converge_independente_do_formato(client_as, org_a_actor):
    """(61) 99999-9999, 61999999999 e +5561999999999 devem virar o
    MESMO valor armazenado — nunca 3 clientes diferentes só por
    formatação."""
    c = client_as(org_a_actor)

    a = c.post("/api/v1/clients", json={"name": "Cliente A", "phone": "(61) 99999-9999"}).json()
    b = c.post("/api/v1/clients", json={"name": "Cliente B", "phone": "61999999999"}).json()
    d = c.post("/api/v1/clients", json={"name": "Cliente D", "phone": "+5561999999999"}).json()

    assert a["phone"] == b["phone"] == d["phone"] == "61999999999"


def test_cpf_valido_e_normalizado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    # CPF válido conhecido (dígitos verificadores corretos).
    resp = c.post("/api/v1/clients", json={"name": "Cliente CPF", "cpf": "111.444.777-35"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["cpf"] == "11144477735"


def test_cpf_invalido_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/clients", json={"name": "Cliente CPF Inválido", "cpf": "123.456.789-00"})
    assert resp.status_code == 422


def test_cpf_continua_opcional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/clients", json={"name": "Cliente Sem CPF"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["cpf"] is None


def test_endereco_aceita_uf_controlada(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/clients",
        json={"name": "Cliente Endereço", "cep": "70000-000", "state": "DF", "city": "Brasília"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "DF"
    assert body["cep"] == "70000000"

    invalid = c.post("/api/v1/clients", json={"name": "Cliente UF Inválida", "state": "XX"})
    assert invalid.status_code == 422


# ---------------------------------------------------------------------
# Etapa C.1 — evolução do cadastro (gênero, número do endereço,
# unicidade de CPF por organização, compatibilidade com cliente antigo)
# ---------------------------------------------------------------------


def test_cadastro_completo_aceita_todos_os_campos_novos(client_as, org_a_actor):
    """Cadastro completo (Clientes > Novo) — todos os campos pedidos,
    incluindo os dois novos desta rodada (gender, address_number)."""
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/clients",
        json={
            "name": "Cliente Completo",
            "phone": "11999990000",
            "whatsapp": "11999990000",
            "email": "completo@example.com",
            "birth_date": "1990-05-20",
            "gender": "female",
            "cpf": "111.444.777-35",
            "cep": "70000-000",
            "state": "DF",
            "city": "Brasília",
            "neighborhood": "Asa Sul",
            "address_line": "Quadra 5",
            "address_number": "120",
            "complement": "Bloco B, apto 302",
            "notes": "Prefere atendimento pela manhã.",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["gender"] == "female"
    assert body["address_number"] == "120"
    assert body["complement"] == "Bloco B, apto 302"
    assert body["notes"] == "Prefere atendimento pela manhã."


def test_cadastro_rapido_da_agenda_continua_funcionando_so_com_nome_e_telefone(client_as, org_a_actor):
    """Item explícito "não transforme o cadastro rápido em formulário
    enorme" — o mesmo endpoint aceita um payload mínimo (nome + telefone,
    exatamente o que `client-picker.tsx` já envia hoje), sem exigir
    NENHUM dos campos novos."""
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/clients", json={"name": "Cliente Rápido", "phone": "11988887777"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Cliente Rápido"
    assert body["gender"] is None
    assert body["cpf"] is None
    assert body["address_number"] is None
    assert body["cep"] is None


def test_genero_e_opcional_e_nao_aceita_valor_fora_do_enum(client_as, org_a_actor):
    c = client_as(org_a_actor)
    sem_genero = c.post("/api/v1/clients", json={"name": "Sem Gênero"})
    assert sem_genero.status_code == 201, sem_genero.text
    assert sem_genero.json()["gender"] is None

    invalido = c.post("/api/v1/clients", json={"name": "Gênero Inválido", "gender": "banana"})
    assert invalido.status_code == 422


def test_editar_cliente_atualiza_campos_novos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Editável"}).json()["id"]

    resp = c.put(
        f"/api/v1/clients/{client_id}",
        json={"name": "Cliente Editável", "gender": "non_binary", "address_number": "45", "notes": "Alérgica a amônia."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gender"] == "non_binary"
    assert body["address_number"] == "45"
    assert body["notes"] == "Alérgica a amônia."


def test_endereco_completo_e_opcional_mesmo_com_alguns_campos_preenchidos(client_as, org_a_actor):
    """Endereço não é tudo-ou-nada — só CEP e cidade preenchidos, sem
    UF/bairro/logradouro/número, continua um cadastro válido."""
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/clients", json={"name": "Cliente Endereço Parcial", "cep": "01310-100", "city": "São Paulo"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["cep"] == "01310100"
    assert body["city"] == "São Paulo"
    assert body["state"] is None
    assert body["address_line"] is None
    assert body["address_number"] is None


def test_nao_permite_dois_clientes_com_mesmo_cpf_na_mesma_organizacao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    primeiro = c.post("/api/v1/clients", json={"name": "Primeiro Cliente", "cpf": "111.444.777-35"})
    assert primeiro.status_code == 201, primeiro.text

    duplicado = c.post("/api/v1/clients", json={"name": "Segundo Cliente (mesmo CPF)", "cpf": "111.444.777-35"})
    assert duplicado.status_code == 409, duplicado.text

    # Mesmo com o CPF formatado diferente (a normalização precisa
    # acontecer ANTES da checagem de duplicidade, não depois).
    duplicado_formatado = c.post("/api/v1/clients", json={"name": "Terceiro Cliente", "cpf": "11144477735"})
    assert duplicado_formatado.status_code == 409


def test_editar_o_proprio_cliente_mantendo_o_mesmo_cpf_nao_conflita_consigo_mesmo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente CPF Próprio", "cpf": "111.444.777-35"}).json()["id"]

    resp = c.put(f"/api/v1/clients/{client_id}", json={"name": "Cliente CPF Próprio Renomeado", "cpf": "111.444.777-35"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cpf"] == "11144477735"


def test_editar_cliente_para_cpf_ja_usado_por_outro_e_bloqueado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    c.post("/api/v1/clients", json={"name": "Dono do CPF", "cpf": "111.444.777-35"})
    outro_id = c.post("/api/v1/clients", json={"name": "Outro Cliente"}).json()["id"]

    resp = c.put(f"/api/v1/clients/{outro_id}", json={"name": "Outro Cliente", "cpf": "111.444.777-35"})
    assert resp.status_code == 409


def test_mesmo_cpf_em_organizacoes_diferentes_nunca_conflita(client_as, org_a_actor, org_b_actor):
    """Item explícito do pedido: "uma mesma pessoa pode existir em
    organizações diferentes — nunca criar unicidade global indevida"."""
    c_a = client_as(org_a_actor)
    resp_a = c_a.post("/api/v1/clients", json={"name": "Cliente Org A", "cpf": "111.444.777-35"})
    assert resp_a.status_code == 201, resp_a.text

    c_b = client_as(org_b_actor)
    resp_b = c_b.post("/api/v1/clients", json={"name": "Cliente Org B", "cpf": "111.444.777-35"})
    assert resp_b.status_code == 201, resp_b.text  # mesmo CPF, organização diferente — nunca é conflito


def test_cpf_duplicado_bloqueia_mesmo_se_cliente_original_estiver_desativado(client_as, org_a_actor):
    """Um cadastro desativado por engano/duplicado continua "dono" do
    CPF — reativar o original (ou corrigir o cadastro) é o caminho
    certo, não permitir um segundo cadastro ativo com o mesmo documento."""
    c = client_as(org_a_actor)
    original_id = c.post("/api/v1/clients", json={"name": "Cliente Original", "cpf": "111.444.777-35"}).json()["id"]
    c.patch(f"/api/v1/clients/{original_id}/deactivate")

    resp = c.post("/api/v1/clients", json={"name": "Cliente Novo Mesmo CPF", "cpf": "111.444.777-35"})
    assert resp.status_code == 409


def test_compatibilidade_com_cliente_antigo_sem_os_campos_novos(client_as, org_a_actor):
    """Cliente criado antes desta rodada (sem gender/address_number,
    exatamente como um registro legado no banco) continua sendo lido e
    editado normalmente — os campos novos simplesmente aparecem como
    `None` até serem preenchidos."""
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Legado", "phone": "11977776666"}).json()["id"]

    resp = c.get(f"/api/v1/clients/{client_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gender"] is None
    assert body["address_number"] is None

    # E dá pra editar só um dos campos novos sem precisar reenviar tudo
    # (o schema de Update é o mesmo Base — cada campo omitido volta a
    # ser None, então o teste reenvia o que já existia de propósito).
    resp = c.put(f"/api/v1/clients/{client_id}", json={"name": "Cliente Legado", "phone": "11977776666", "gender": "male"})
    assert resp.status_code == 200
    assert resp.json()["gender"] == "male"
    assert resp.json()["address_number"] is None


def test_observacoes_sao_internas_nao_aparecem_no_historico_do_cliente(client_as, org_a_actor):
    """Item explícito "observações são internas — não expor fora de
    endpoints autorizados": o endpoint de Histórico (usado também pra
    montar qualquer resumo do atendimento) não inclui `notes` em lugar
    nenhum do payload — ele só devolve `client_since`/`visits_count`/
    `total_spent`/`orders`, nunca o próprio `Client`."""
    c = client_as(org_a_actor)
    client_id = c.post(
        "/api/v1/clients", json={"name": "Cliente Observações", "notes": "Informação sensível interna"}
    ).json()["id"]

    history = c.get(f"/api/v1/clients/{client_id}/history")
    assert history.status_code == 200
    assert "notes" not in history.json()
    assert "Informação sensível interna" not in history.text


def test_isolamento_multiempresa_cliente_de_outra_org_nao_aparece(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    client_id = c_a.post("/api/v1/clients", json={"name": "Cliente Org A"}).json()["id"]

    c_b = client_as(org_b_actor)
    resp = c_b.get(f"/api/v1/clients/{client_id}")
    assert resp.status_code == 404

    resp_list = c_b.get("/api/v1/clients")
    assert client_id not in [cl["id"] for cl in resp_list.json()]


# ---------------------------------------------------------------------
# Histórico do cliente — derivado de Order, nunca campo manual
# ---------------------------------------------------------------------


def _setup_finished_appointment_two_services(c, client_id):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid_hex()}"}).json()
    prof_a = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    prof_b = c.post("/api/v1/professionals", json={"name": "Ingrid"}).json()
    svc_a = c.post(
        "/api/v1/services", json={"name": "Manutenção", "default_duration_minutes": 60, "default_price": "300.00"}
    ).json()
    svc_b = c.post(
        "/api/v1/services", json={"name": "Mechas", "default_duration_minutes": 90, "default_price": "500.00"}
    ).json()
    for prof, svc in ((prof_a, svc_a), (prof_b, svc_b)):
        c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
        c.put(
            f"/api/v1/professionals/{prof['id']}/working-hours",
            json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
        )

    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"],
            "client_id": client_id,
            "items": [
                {"professional_id": prof_a["id"], "service_id": svc_a["id"], "start_at": "2026-08-13T09:00:00-03:00"},
                {"professional_id": prof_b["id"], "service_id": svc_b["id"], "start_at": "2026-08-13T11:00:00-03:00"},
            ],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target})
    return appt, branch


def uuid_hex():
    import uuid

    return uuid.uuid4().hex[:6]


def test_historico_do_cliente_deriva_de_comanda_com_pagamento_misto(client_as, org_a_actor):
    """1 cliente, 1 visita com 2 serviços, pago Pix+Crédito — histórico
    deve mostrar 1 atendimento, R$ 800 gastos (nunca 2 nem R$ 1.600),
    com os nomes de serviço/profissional preservados."""
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Ana Souza"}).json()["id"]
    appt, branch = _setup_finished_appointment_two_services(c, client_id)
    register = c.post(
        "/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}
    ).json()

    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    assert order["total"] == "800.00"

    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={
            "payments": [
                {"method": "pix", "amount": "300.00", "cash_register_id": register["id"]},
                {"method": "credit", "amount": "500.00", "card_brand": "visa", "cash_register_id": register["id"]},
            ]
        },
    )
    assert closed.status_code == 200, closed.text

    history = c.get(f"/api/v1/clients/{client_id}/history")
    assert history.status_code == 200
    body = history.json()
    assert body["visits_count"] == 1
    assert body["total_spent"] == "800.00"
    assert len(body["orders"]) == 1
    order_out = body["orders"][0]
    assert order_out["order_number"] >= 1
    names = {item["service_name"] for item in order_out["items"]}
    assert names == {"Manutenção", "Mechas"}


def test_historico_do_cliente_sem_nenhuma_comanda_fica_vazio(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Novo"}).json()["id"]

    history = c.get(f"/api/v1/clients/{client_id}/history").json()
    assert history["visits_count"] == 0
    assert history["total_spent"] == "0"
    assert history["orders"] == []
