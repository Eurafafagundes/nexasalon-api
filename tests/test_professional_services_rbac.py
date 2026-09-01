"""Bug real corrigido: `GET /professionals/{professional_id}/services`
exigia `professionals.view` (permissão administrativa do módulo
Profissionais) pra uma necessidade puramente OPERACIONAL — montar um
agendamento no Novo Agendamento da Agenda. Um funcionário criado em
Configurações → Equipe e acessos com `agenda.create`/`agenda.edit`
(consegue abrir a Agenda e criar agendamentos normalmente, e também
consegue `GET /services/lookup` via `services.lookup`) ficava bloqueado
só nesta leitura — a UI de Equipe e acessos nem expõe um jeito de
conceder `professionals.view` a esse tipo de conta.

Correção: `professionals.py::_view_professional_services` agora aceita
`professionals.view` OU `agenda.create` OU `agenda.edit` — mesmo
raciocínio já usado por `services.py::_lookup` (migration 0030). Só a
LEITURA deste endpoint específico afrouxa; `professionals.manage`
continua a única forma de criar/editar/desativar profissional, jornada
ou vínculos de serviço (nunca concedido por `agenda.*`)."""
import uuid

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User


def _actor_same_org(org_id: uuid.UUID, *, permissions: frozenset[str]) -> ActorContext:
    """Ator numa organização JÁ EXISTENTE (reaproveita `org_a_actor`),
    com um conjunto de permissions arbitrário — simula um funcionário
    com role/override mais restrito que os 4 papéis de sistema, exatamente
    a situação real que expôs o bug (a UI de Equipe e acessos não
    consegue representar `professionals.view` de forma alguma)."""
    with SessionLocal() as session:
        user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=org_id,
        user_id=user_id,
        membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(),
        role_name="role-teste",
        permissions=permissions,
    )


def _setup_professional_with_service(c):
    """MASTER (org_a_actor, todas as permissions) cadastra uma branch,
    um profissional e um serviço vinculado ativo — dado real que a
    leitura em teste vai tentar consultar."""
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    prof = c.post("/api/v1/professionals", json={"name": "Jhon", "branch_id": branch["id"]}).json()
    servico = c.post(
        "/api/v1/services",
        json={"name": "Corte", "default_duration_minutes": 30, "default_price": "50.00"},
    ).json()
    resp = c.put(
        f"/api/v1/professionals/{prof['id']}/services",
        json={"items": [{"service_id": servico["id"], "is_active": True}]},
    )
    assert resp.status_code == 200, resp.text
    return prof, servico


def test_master_consegue_consultar_servicos_do_profissional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    prof, _servico = _setup_professional_with_service(c)

    resp = c.get(f"/api/v1/professionals/{prof['id']}/services")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


def test_funcionario_com_agenda_create_consegue_consultar_servicos_do_profissional(client_as, org_a_actor):
    """O cenário real relatado: um funcionário sem `professionals.view`,
    mas com a permissão operacional que já abre a Agenda e cria
    agendamentos, deve conseguir montar o Novo Agendamento — inclusive
    ver os serviços vinculados ao profissional escolhido."""
    master = client_as(org_a_actor)
    prof, _servico = _setup_professional_with_service(master)

    funcionario_actor = _actor_same_org(org_a_actor.organization_id, permissions=frozenset({"agenda.create"}))
    funcionario = client_as(funcionario_actor)

    resp = funcionario.get(f"/api/v1/professionals/{prof['id']}/services")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert resp.json()[0]["is_active"] is True


def test_funcionario_com_agenda_edit_tambem_consegue(client_as, org_a_actor):
    """`agenda.edit` sozinho (sem `agenda.create`) também deve passar —
    o pedido explícito foi 'criar OU editar agendamentos'."""
    master = client_as(org_a_actor)
    prof, _servico = _setup_professional_with_service(master)

    funcionario_actor = _actor_same_org(org_a_actor.organization_id, permissions=frozenset({"agenda.edit"}))
    funcionario = client_as(funcionario_actor)

    resp = funcionario.get(f"/api/v1/professionals/{prof['id']}/services")
    assert resp.status_code == 200, resp.text


def test_usuario_sem_permissao_de_agenda_nem_profissionais_continua_bloqueado(client_as, org_a_actor):
    """Sem `professionals.view` E sem `agenda.create`/`agenda.edit` — a
    correção não deve abrir a porta pra qualquer permissão qualquer,
    só pras três alternativas explicitamente equivalentes."""
    master = client_as(org_a_actor)
    prof, _servico = _setup_professional_with_service(master)

    sem_acesso_actor = _actor_same_org(
        org_a_actor.organization_id, permissions=frozenset({"clients.view", "finance.view"})
    )
    sem_acesso = client_as(sem_acesso_actor)

    resp = sem_acesso.get(f"/api/v1/professionals/{prof['id']}/services")
    assert resp.status_code == 403, resp.text


def test_agenda_view_own_sozinho_nao_e_suficiente(client_as, org_a_actor):
    """Só VER a própria agenda (`agenda.view_own`) não é 'criar ou editar
    agendamento' — não deve satisfazer a checagem. Escopo deliberadamente
    estreito: só `agenda.create`/`agenda.edit` (mais `professionals.view`)
    passam, não qualquer permission de agenda."""
    master = client_as(org_a_actor)
    prof, _servico = _setup_professional_with_service(master)

    view_only_actor = _actor_same_org(org_a_actor.organization_id, permissions=frozenset({"agenda.view_own"}))
    view_only = client_as(view_only_actor)

    resp = view_only.get(f"/api/v1/professionals/{prof['id']}/services")
    assert resp.status_code == 403, resp.text


def test_outro_tenant_continua_bloqueado_mesmo_com_agenda_create(client_as, org_a_actor, org_b_actor):
    """Isolamento por Organization é ortogonal à permissão — um ator de
    outra org, mesmo com `agenda.create` completo (org_b_actor tem TODAS
    as permissions, inclusive essa), nunca deve enxergar um profissional
    de outra organização."""
    master_a = client_as(org_a_actor)
    prof, _servico = _setup_professional_with_service(master_a)

    outro_tenant = client_as(org_b_actor)
    resp = outro_tenant.get(f"/api/v1/professionals/{prof['id']}/services")
    assert resp.status_code in (403, 404), resp.text


def test_professionals_manage_nunca_e_concedido_por_agenda_permissions(client_as, org_a_actor):
    """A correção afrouxa só a LEITURA de vínculos. Um funcionário com
    `agenda.create` continua sem conseguir editar o cadastro do
    profissional nem redefinir seus vínculos de serviço — isso exige
    `professionals.manage`, nunca satisfeito por permissão de agenda."""
    master = client_as(org_a_actor)
    prof, servico = _setup_professional_with_service(master)

    funcionario_actor = _actor_same_org(org_a_actor.organization_id, permissions=frozenset({"agenda.create"}))
    funcionario = client_as(funcionario_actor)

    update_resp = funcionario.put(f"/api/v1/professionals/{prof['id']}", json={"name": "Nome Alterado"})
    assert update_resp.status_code == 403, update_resp.text

    replace_resp = funcionario.put(
        f"/api/v1/professionals/{prof['id']}/services",
        json={"items": [{"service_id": servico["id"], "is_active": False}]},
    )
    assert replace_resp.status_code == 403, replace_resp.text
