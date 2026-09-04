import uuid

from sqlalchemy import select, text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User
from nexasalon_api.models.rbac import Role, RolePermission


def test_crud_branch(client_as, org_a_actor):
    c = client_as(org_a_actor)

    resp = c.post("/api/v1/branches", json={"name": "Matriz", "slug": "matriz"})
    assert resp.status_code == 201, resp.text
    branch = resp.json()
    assert branch["organization_id"] == str(org_a_actor.organization_id)
    assert branch["is_active"] is True
    branch_id = branch["id"]

    resp = c.get(f"/api/v1/branches/{branch_id}")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "matriz"

    resp = c.get("/api/v1/branches")
    assert resp.status_code == 200
    assert any(b["id"] == branch_id for b in resp.json())

    resp = c.put(f"/api/v1/branches/{branch_id}", json={"name": "Matriz Renomeada", "slug": "matriz"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Matriz Renomeada"


def test_desativar_branch_nao_apaga_historico(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = c.post("/api/v1/branches", json={"name": "Unidade X", "slug": "unidade-x"}).json()["id"]

    resp = c.patch(f"/api/v1/branches/{branch_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # continua existindo e acessível por id — só não aparece na listagem padrão
    resp = c.get(f"/api/v1/branches/{branch_id}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = c.get("/api/v1/branches")
    assert branch_id not in [b["id"] for b in resp.json()]

    resp = c.get("/api/v1/branches?include_inactive=true")
    assert branch_id in [b["id"] for b in resp.json()]

    resp = c.patch(f"/api/v1/branches/{branch_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_branch_nao_encontrada_404(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/api/v1/branches/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


# ---------------------------------------------------------------------
# Bug real: Recepcionista (e qualquer role customizado com só
# `agenda.view_own`/`agenda.view_all`, sem `branches.view`) tomava 403
# em `GET /branches`, o frontend engolia o erro (`.catch(() =>
# setBranches([]))`) e a Agenda caía no estado "Estabelecimento não
# configurado" mesmo com a Branch corretamente cadastrada e funcionando
# para o Usuário Master. Ver `api/v1/branches.py::_view`
# (`require_any_permission("branches.view", "agenda.view_own",
# "agenda.view_all")`).
# ---------------------------------------------------------------------


def _restricted(base_actor: ActorContext, *, permissions: set[str], role_name: str = "Restrito") -> ActorContext:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name=f"Usuário {role_name}")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name=role_name, permissions=frozenset(permissions),
    )


def _real_receptionist_permissions() -> frozenset[str]:
    """Permissões efetivas do role de SISTEMA `RECEPTIONIST` (seedado na
    migration 0007, `organization_id IS NULL`), lidas do catálogo real
    em vez de duplicadas à mão aqui — o teste acompanha a fonte da
    verdade automaticamente se o catálogo mudar, em vez de arriscar
    ficar desatualizado."""
    with SessionLocal() as session:
        role_id = session.execute(
            select(Role.id).where(Role.name == "RECEPTIONIST", Role.organization_id.is_(None))
        ).scalar_one()
        keys = session.execute(
            select(RolePermission.permission_key).where(RolePermission.role_id == role_id)
        ).scalars().all()
    return frozenset(keys)


def _configured_branch(c) -> dict:
    resp = c.post(
        "/api/v1/branches",
        json={
            "name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}",
            "agenda_view_start": "08:00:00", "agenda_view_end": "20:00:00", "agenda_slot_minutes": 30,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_receptionist_real_le_branch_sem_branches_view_bug_da_agenda_nao_configurada(client_as, org_a_actor):
    """Reproduz o cenário exato do bug: Master cadastra a Matriz,
    Recepcionista (role de SISTEMA real, sem `branches.view` por
    padrão) precisa conseguir listar/detalhar essa MESMA Branch pra
    Agenda resolver `agenda_view_start`/`agenda_view_end`/
    `agenda_slot_minutes`/timezone e NÃO cair em "Estabelecimento não
    configurado"."""
    master = client_as(org_a_actor)
    branch = _configured_branch(master)

    receptionist_permissions = _real_receptionist_permissions()
    assert "agenda.view_all" in receptionist_permissions
    assert "branches.view" not in receptionist_permissions  # confirma a premissa do bug

    receptionist = client_as(_restricted(org_a_actor, permissions=receptionist_permissions, role_name="RECEPTIONIST"))

    resp = receptionist.get("/api/v1/branches")
    assert resp.status_code == 200, resp.text
    assert any(b["id"] == branch["id"] for b in resp.json())

    resp = receptionist.get(f"/api/v1/branches/{branch['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["agenda_view_start"] == "08:00:00"
    assert resp.json()["agenda_slot_minutes"] == 30


def test_agenda_view_own_tambem_le_branches_sem_branches_view(client_as, org_a_actor):
    """`agenda.view_own` (ex.: role PROFESSIONAL) também precisa
    resolver a Branch pra própria Agenda — mesmo raciocínio de
    `agenda.view_all`, só que pro escopo "própria agenda"."""
    master = client_as(org_a_actor)
    branch = _configured_branch(master)

    restricted = client_as(_restricted(org_a_actor, permissions={"agenda.view_own"}))
    resp = restricted.get("/api/v1/branches")
    assert resp.status_code == 200, resp.text
    assert any(b["id"] == branch["id"] for b in resp.json())


def test_custom_role_com_agenda_view_all_tambem_le_branches(client_as, org_a_actor):
    """Item explícito do pedido: o bug não pode ser específico do role
    `RECEPTIONIST` — qualquer role CUSTOMIZADO com `agenda.view_all`
    precisa funcionar igual, sem exceção por nome de role."""
    master = client_as(org_a_actor)
    branch = _configured_branch(master)

    custom = client_as(_restricted(org_a_actor, permissions={"agenda.view_all"}, role_name="Gerente de Sala"))
    resp = custom.get("/api/v1/branches")
    assert resp.status_code == 200, resp.text
    assert any(b["id"] == branch["id"] for b in resp.json())


def test_sem_branches_view_nem_agenda_view_continua_bloqueado_403(client_as, org_a_actor):
    """A correção é um OR estrito com as permissions de Agenda — um
    ator sem NENHUMA das três (`branches.view`, `agenda.view_own`,
    `agenda.view_all`) continua corretamente bloqueado. Prova que a
    trava não virou um `require_permission` vazio / aberto demais."""
    master = client_as(org_a_actor)
    branch = _configured_branch(master)

    restricted = client_as(_restricted(org_a_actor, permissions={"clients.view"}))
    resp = restricted.get("/api/v1/branches")
    assert resp.status_code == 403

    resp = restricted.get(f"/api/v1/branches/{branch['id']}")
    assert resp.status_code == 403


def test_agenda_view_all_nao_concede_gerenciar_branch(client_as, org_a_actor):
    """`agenda.view_all`/`agenda.view_own` só destrava LEITURA — criar,
    editar, ativar e desativar uma unidade continuam exigindo
    `branches.manage` (nunca concedido "de brinde" por só enxergar a
    Agenda). Confirma que a Recepcionista não ganhou nenhuma
    capacidade administrativa nova."""
    master = client_as(org_a_actor)
    branch = _configured_branch(master)

    restricted = client_as(_restricted(org_a_actor, permissions={"agenda.view_all", "branches.view"}))

    resp = restricted.post("/api/v1/branches", json={"name": "Nova Unidade", "slug": f"nova-{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 403

    resp = restricted.put(f"/api/v1/branches/{branch['id']}", json={"name": "Renomeada", "slug": branch["slug"]})
    assert resp.status_code == 403

    resp = restricted.patch(f"/api/v1/branches/{branch['id']}/deactivate")
    assert resp.status_code == 403

    resp = restricted.patch(f"/api/v1/branches/{branch['id']}/activate")
    assert resp.status_code == 403


def test_isolamento_multi_tenant_leitura_de_branches_via_agenda_view_all(client_as, org_a_actor, org_b_actor):
    """Ator da Org B com `agenda.view_all` nunca pode enxergar a Branch
    da Org A, mesmo com a permission correta — isolamento por
    `organization_id` continua intacto (inalterado em
    `branches_service`)."""
    master_a = client_as(org_a_actor)
    branch_a = _configured_branch(master_a)

    restricted_b = client_as(_restricted(org_b_actor, permissions={"agenda.view_all"}))
    resp = restricted_b.get("/api/v1/branches")
    assert resp.status_code == 200
    assert all(b["id"] != branch_a["id"] for b in resp.json())

    resp = restricted_b.get(f"/api/v1/branches/{branch_a['id']}")
    assert resp.status_code == 404
