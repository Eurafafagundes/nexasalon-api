"""Testes da Etapa A — controle granular de agenda (VISUALIZAR x EDITAR
por profissional), além das permissions grosseiras `agenda.view_own` /
`agenda.view_all` / `agenda.edit` já cobertas em `test_agenda.py` /
`test_auth.py`.

Duas camadas testadas separadamente:
  - Nível de serviço (`services/agenda_access.py`, `services/agenda.py`,
    `services/appointments.py`): usa o mesmo padrão de `_actor()` direto
    em `ActorContext` de `test_agenda.py`, construindo o frozenset de
    ids manualmente — rápido, sem precisar de login real.
  - Nível HTTP ponta a ponta (`test_*_via_http`): login de verdade,
    `OrganizationMembership.agenda_view_scope/agenda_edit_scope` reais no
    banco + linhas em `membership_agenda_grants` — prova que a
    dependency `get_current_actor` (api/deps.py) resolve o escopo
    corretamente E que a rota HTTP (não só o service isolado) barra o
    acesso. É a evidência explícita pedida: "um usuário sem acesso a uma
    agenda não deve conseguir buscar diretamente pelo endpoint"."""
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ForbiddenError, NotFoundError, ValidationDomainError
from nexasalon_api.core.security import hash_password
from nexasalon_api.main import app
from nexasalon_api.models.agenda_access import MembershipAgendaGrant
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AgendaAccessScope, AppointmentStatus, MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership, User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.rbac import Role, RolePermission
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate, AppointmentItemUpdate
from nexasalon_api.services import agenda, agenda_access, appointments

_OUR_THURSDAY = 4
_TZ = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------
# Nível de serviço — mesmo padrão de fixtures de test_agenda.py
# ---------------------------------------------------------------------


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org agenda access", slug=f"org-aa-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(
    session,
    org_id,
    *,
    permissions,
    professional_id=None,
    viewable=None,
    editable=None,
) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="role", permissions=frozenset(permissions), professional_id=professional_id,
        agenda_viewable_professional_ids=viewable, agenda_editable_professional_ids=editable,
    )


def _branch(session, org_id) -> Branch:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _professional(session, org_id, branch_id, name="Prof") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Corte", duration=60, price=100) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=duration, default_price=price)
    session.add(s)
    session.flush()
    return s


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _setup_two_professionals(session, org_id):
    branch = _branch(session, org_id)
    prof_a = _professional(session, org_id, branch.id, "Profissional A")
    prof_b = _professional(session, org_id, branch.id, "Profissional B")
    service = _service(session, org_id)
    for p in (prof_a, prof_b):
        session.add(ProfessionalService(professional_id=p.id, service_id=service.id))
        session.add(
            WorkingHours(
                organization_id=org_id, professional_id=p.id, weekday=_OUR_THURSDAY,
                start_time=time(9, 0), end_time=time(18, 0),
            )
        )
    session.flush()
    client = Client(organization_id=org_id, name="Cliente")
    session.add(client)
    session.flush()
    return branch, prof_a, prof_b, service, client


def _create_appointment(session, org_id, owner, branch, prof, service, client, hour):
    return appointments.create_appointment(
        session, owner,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(hour))],
        ),
    )


def test_scope_all_nao_restringe_visualizacao_nem_edicao(org_session):
    """ALL (default) preserva o comportamento anterior — inclusive para
    um profissional criado DEPOIS do ator existir (auto-apply "novas
    agendas" sem precisar de linha nenhuma em membership_agenda_grants)."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    actor = _actor(session, org_id, permissions={"agenda.view_all", "agenda.edit"})
    assert agenda_access.can_view_professional(actor, prof_a.id) is True
    assert agenda_access.can_edit_professional(actor, prof_a.id) is True

    novo_prof = _professional(session, org_id, branch.id, "Profissional Novo")
    assert agenda_access.can_view_professional(actor, novo_prof.id) is True
    assert agenda_access.can_edit_professional(actor, novo_prof.id) is True


def test_scope_selected_restringe_a_lista_de_agenda(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)
    _create_appointment(session, org_id, owner, branch, prof_b, service, client, 14)

    # SELECTED com só prof_a liberado, mesmo tendo agenda.view_all na
    # permission grosseira — o escopo granular SUBSTITUI a checagem por
    # permission quando concreto (não é aditivo).
    restricted = _actor(
        session, org_id, permissions={"agenda.view_all"}, viewable=frozenset({prof_a.id}),
    )
    items = agenda.list_agenda(session, restricted, date_from=_dt(0), date_to=_dt(23, 59))
    assert {i.professional_id for i in items} == {prof_a.id}


def test_scope_selected_vazio_nao_ve_nada(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)

    empty_actor = _actor(session, org_id, permissions={"agenda.view_all"}, viewable=frozenset())
    items = agenda.list_agenda(session, empty_actor, date_from=_dt(0), date_to=_dt(23, 59))
    assert items == []


def test_get_appointment_404_quando_profissional_fora_do_escopo_de_visualizacao(org_session):
    """Convenção preservada: agendamento de um profissional fora do
    escopo de VISUALIZAÇÃO -> 404 (não confirma nem nega a existência),
    igual ao comportamento pré-existente de view_own."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)

    restricted = _actor(session, org_id, permissions={"agenda.view_all"}, viewable=frozenset({prof_b.id}))
    with pytest.raises(NotFoundError):
        appointments.get_appointment(session, restricted, appt.id)

    # mas o dono do escopo vê o dele normalmente
    allowed = _actor(session, org_id, permissions={"agenda.view_all"}, viewable=frozenset({prof_a.id}))
    fetched = appointments.get_appointment(session, allowed, appt.id)
    assert fetched.id == appt.id


def test_edicao_e_403_quando_visivel_mas_nao_editavel(org_session):
    """Caso distinto do 404: o agendamento É visível (visualização
    liberada), mas o ator não pode EDITAR aquele profissional -> 403
    (ForbiddenError), não 404 — "consigo ver que existe, só não posso
    mexer"."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)

    # visualizar liberado para prof_a, mas editar restrito a um conjunto
    # que NÃO inclui prof_a (assimetria proposital)
    view_only = _actor(
        session, org_id, permissions={"agenda.view_all", "agenda.edit"},
        viewable=frozenset({prof_a.id}), editable=frozenset(),
    )
    # visível:
    assert appointments.get_appointment(session, view_only, appt.id).id == appt.id
    # mas não editável:
    with pytest.raises(ForbiddenError):
        appointments.update_status(session, view_only, appt.id, AppointmentStatus.CONFIRMED)
    with pytest.raises(ForbiddenError):
        appointments.cancel_appointment(session, view_only, appt.id)


def test_assimetria_ver_colega_editar_so_o_proprio(org_session):
    """Caso explícito do pedido: profissional B pode VER a agenda do
    colega A, mas só EDITAR a própria (B)."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt_a = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)
    appt_b = _create_appointment(session, org_id, owner, branch, prof_b, service, client, 14)

    actor_b = _actor(
        session, org_id, permissions={"agenda.view_all", "agenda.edit"}, professional_id=prof_b.id,
        viewable=frozenset({prof_a.id, prof_b.id}), editable=frozenset({prof_b.id}),
    )
    # vê os dois:
    assert appointments.get_appointment(session, actor_b, appt_a.id).id == appt_a.id
    assert appointments.get_appointment(session, actor_b, appt_b.id).id == appt_b.id
    # edita só o próprio:
    appointments.update_status(session, actor_b, appt_b.id, AppointmentStatus.CONFIRMED)
    with pytest.raises(ForbiddenError):
        appointments.update_status(session, actor_b, appt_a.id, AppointmentStatus.CONFIRMED)


def test_create_appointment_recusa_profissional_fora_do_escopo_de_edicao(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    actor = _actor(
        session, org_id, permissions={"agenda.create"}, editable=frozenset({prof_b.id}),
    )
    with pytest.raises(ForbiddenError):
        appointments.create_appointment(
            session, actor,
            AppointmentCreate(
                branch_id=branch.id, client_id=client.id,
                items=[AppointmentItemCreate(professional_id=prof_a.id, service_id=service.id, start_at=_dt(10))],
            ),
        )


def test_drag_and_drop_recusa_profissional_de_origem_fora_do_escopo_de_edicao(org_session):
    """`update_appointment_item` (drawer de preço/duração + drag-and-drop,
    Etapa F) precisa reusar a MESMA checagem granular já provada acima
    pra `update_status`/`cancel_appointment`/`create_appointment` — item
    explícito do pedido "respeitar view/edit por agenda no drag-and-drop".
    Aqui o item já pertence a um profissional FORA do escopo de edição
    (mesmo visível) -> 403 só de tentar editar preço/duração, sem nem
    chegar a mover de profissional."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)
    item = appt.items[0]

    view_only = _actor(
        session, org_id, permissions={"agenda.view_all", "agenda.edit"},
        viewable=frozenset({prof_a.id}), editable=frozenset(),
    )
    with pytest.raises(ForbiddenError):
        appointments.update_appointment_item(
            session, view_only, appt.id, item.id,
            AppointmentItemUpdate(duration_override=90),
        )


def test_drag_and_drop_recusa_mover_para_profissional_fora_do_escopo_de_edicao(org_session):
    """Caso simétrico: o item começa num profissional EDITÁVEL (prof_a),
    mas o destino do arraste (prof_b) está fora do escopo de edição do
    ator -> 403. Prova que `_assert_can_edit` checa AMBOS os
    profissionais (origem e destino), não só o de origem."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)
    item = appt.items[0]

    can_edit_only_a = _actor(
        session, org_id, permissions={"agenda.view_all", "agenda.edit"},
        viewable=frozenset({prof_a.id, prof_b.id}), editable=frozenset({prof_a.id}),
    )
    with pytest.raises(ForbiddenError):
        appointments.update_appointment_item(
            session, can_edit_only_a, appt.id, item.id,
            AppointmentItemUpdate(professional_id=prof_b.id),
        )

    # mas mover para dentro do escopo de edição (continuar com prof_a) funciona:
    updated = appointments.update_appointment_item(
        session, can_edit_only_a, appt.id, item.id,
        AppointmentItemUpdate(start_at=_dt(11)),
    )
    assert updated.items[0].start_at == _dt(11)


# ---------------------------------------------------------------------
# services/agenda_access.py — validação de set_agenda_access
# ---------------------------------------------------------------------


def _membership(session, org_id, role_id) -> OrganizationMembership:
    user = User(email=f"m-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Membership Teste")
    session.add(user)
    session.flush()
    membership = OrganizationMembership(
        user_id=user.id, organization_id=org_id, role_id=role_id, status=MembershipStatus.ACTIVE,
    )
    session.add(membership)
    session.flush()
    return membership


def _role(session, org_id) -> Role:
    role = Role(organization_id=org_id, name=f"Role-{uuid.uuid4().hex[:6]}", is_system=False)
    session.add(role)
    session.flush()
    return role


def test_set_agenda_access_edit_scope_all_exige_view_scope_all(org_session):
    session, org_id = org_session
    role = _role(session, org_id)
    membership = _membership(session, org_id, role.id)
    with pytest.raises(ValidationDomainError):
        agenda_access.set_agenda_access(
            session, org_id, membership.id,
            view_scope=AgendaAccessScope.SELECTED, edit_scope=AgendaAccessScope.ALL,
            viewable_professional_ids=[], editable_professional_ids=[],
        )


def test_set_agenda_access_edit_ids_devem_ser_subconjunto_de_view_ids(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    role = _role(session, org_id)
    membership = _membership(session, org_id, role.id)
    with pytest.raises(ValidationDomainError):
        agenda_access.set_agenda_access(
            session, org_id, membership.id,
            view_scope=AgendaAccessScope.SELECTED, edit_scope=AgendaAccessScope.SELECTED,
            viewable_professional_ids=[prof_a.id], editable_professional_ids=[prof_b.id],
        )


def test_set_agenda_access_recusa_profissional_inexistente(org_session):
    session, org_id = org_session
    role = _role(session, org_id)
    membership = _membership(session, org_id, role.id)
    with pytest.raises(ValidationDomainError):
        agenda_access.set_agenda_access(
            session, org_id, membership.id,
            view_scope=AgendaAccessScope.SELECTED, edit_scope=AgendaAccessScope.SELECTED,
            viewable_professional_ids=[uuid.uuid4()], editable_professional_ids=[],
        )


def test_set_agenda_access_persiste_e_resolve_corretamente(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    role = _role(session, org_id)
    membership = _membership(session, org_id, role.id)

    summary = agenda_access.set_agenda_access(
        session, org_id, membership.id,
        view_scope=AgendaAccessScope.SELECTED, edit_scope=AgendaAccessScope.SELECTED,
        viewable_professional_ids=[prof_a.id, prof_b.id], editable_professional_ids=[prof_a.id],
    )
    assert summary.view_scope == AgendaAccessScope.SELECTED
    assert {g.professional_id for g in summary.grants if g.can_view} == {prof_a.id, prof_b.id}
    assert {g.professional_id for g in summary.grants if g.can_edit} == {prof_a.id}

    session.refresh(membership)
    assert agenda_access.resolve_viewable_ids(session, membership) == frozenset({prof_a.id, prof_b.id})
    assert agenda_access.resolve_editable_ids(session, membership) == frozenset({prof_a.id})

    # chamar de novo com um conjunto MENOR substitui (semântica PUT) —
    # não fica grant "órfão" da configuração anterior.
    agenda_access.set_agenda_access(
        session, org_id, membership.id,
        view_scope=AgendaAccessScope.SELECTED, edit_scope=AgendaAccessScope.SELECTED,
        viewable_professional_ids=[prof_a.id], editable_professional_ids=[prof_a.id],
    )
    session.refresh(membership)
    assert agenda_access.resolve_viewable_ids(session, membership) == frozenset({prof_a.id})


# ---------------------------------------------------------------------
# Nível HTTP ponta a ponta — login real + escopo real no banco. Prova
# que a rota (não só o service isolado) barra o acesso, incluindo a
# resolução em api/deps.py.
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _real_auth_mode(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_enabled", False)


@pytest.fixture(autouse=True)
def _disable_rate_limiting(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _http_scenario():
    """Monta, com sessões/commits reais (não em memória, como os testes
    de serviço acima): uma org, dois profissionais com agendamentos, uma
    role com agenda.view_all+agenda.edit, e uma membership ACTIVE com
    escopo SELECTED liberando só um dos dois profissionais para
    visualização (e nenhum para edição)."""
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org HTTP AA", slug=f"org-http-aa-{org_id.hex[:8]}"))
        session.flush()

        branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{org_id.hex[:8]}")
        session.add(branch)
        session.flush()

        prof_a = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional A")
        prof_b = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional B")
        session.add_all([prof_a, prof_b])
        session.flush()

        service = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=100)
        session.add(service)
        session.flush()
        for p in (prof_a, prof_b):
            session.add(ProfessionalService(professional_id=p.id, service_id=service.id))
            session.add(
                WorkingHours(
                    organization_id=org_id, professional_id=p.id, weekday=_OUR_THURSDAY,
                    start_time=time(9, 0), end_time=time(18, 0),
                )
            )
        session.flush()

        client_row = Client(organization_id=org_id, name="Cliente")
        session.add(client_row)
        session.flush()

        role = Role(organization_id=org_id, name="Custom Agenda Role", is_system=False)
        session.add(role)
        session.flush()
        for key in ("agenda.view_all", "agenda.edit", "agenda.create"):
            session.add(RolePermission(role_id=role.id, permission_key=key))
        session.flush()

        owner_role = Role(organization_id=org_id, name="Owner HTTP AA", is_system=False)
        session.add(owner_role)
        session.flush()
        for key in ("agenda.view_all", "agenda.edit", "agenda.create"):
            session.add(RolePermission(role_id=owner_role.id, permission_key=key))
        session.flush()

        password = "Senha123!"
        owner_user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com", name="Owner",
            password_hash=hash_password(password),
        )
        session.add(owner_user)
        session.flush()
        owner_membership = OrganizationMembership(
            user_id=owner_user.id, organization_id=org_id, role_id=owner_role.id, status=MembershipStatus.ACTIVE,
        )
        session.add(owner_membership)
        session.flush()

        restricted_user = User(
            email=f"restricted-{uuid.uuid4().hex[:8]}@example.com", name="Restrito",
            password_hash=hash_password(password),
        )
        session.add(restricted_user)
        session.flush()
        restricted_membership = OrganizationMembership(
            user_id=restricted_user.id, organization_id=org_id, role_id=role.id, status=MembershipStatus.ACTIVE,
            agenda_view_scope=AgendaAccessScope.SELECTED, agenda_edit_scope=AgendaAccessScope.SELECTED,
        )
        session.add(restricted_membership)
        session.flush()
        session.add(
            MembershipAgendaGrant(
                organization_id=org_id, membership_id=restricted_membership.id, professional_id=prof_a.id,
                can_view=True, can_edit=False,
            )
        )
        session.flush()

        # Captura os ids/valores em variáveis Python IMEDIATAMENTE após o
        # flush, ANTES do commit — mesma armadilha documentada em
        # test_auth.py/conftest.py: `set_config(..., true)` (não LOCAL)
        # desfaz seu efeito no commit, revertendo `app.current_org_id`
        # para string vazia; qualquer acesso a atributo expirado (padrão
        # após commit) dispara um SELECT que a policy RLS filtra,
        # produzindo `ObjectDeletedError` em vez do valor esperado.
        ids = {
            "org_id": org_id, "prof_a_id": prof_a.id, "prof_b_id": prof_b.id,
            "branch_id": branch.id, "service_id": service.id, "client_id": client_row.id,
            "owner_email": owner_user.email, "restricted_email": restricted_user.email,
            "password": password,
        }

        session.commit()
    return ids


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_http_membership_sem_acesso_a_agenda_recebe_404_ao_buscar_direto(client):
    """A prova ponta a ponta explícita do pedido: a membership restrita
    tem grant só para prof_a (view=True, edit=False) — um agendamento de
    prof_b não pode ser buscado diretamente por ID via HTTP, mesmo tendo
    a permission grosseira agenda.view_all."""
    ids = _http_scenario()

    owner_body = _login(client, ids["owner_email"], ids["password"])
    owner_headers = _auth_headers(owner_body["tokens"]["access_token"])

    appt_a = client.post(
        "/api/v1/appointments",
        json={
            "branch_id": str(ids["branch_id"]), "client_id": str(ids["client_id"]),
            "items": [{
                "professional_id": str(ids["prof_a_id"]), "service_id": str(ids["service_id"]),
                "start_at": _dt(10).isoformat(),
            }],
        },
        headers=owner_headers,
    )
    assert appt_a.status_code == 201, appt_a.text

    appt_b = client.post(
        "/api/v1/appointments",
        json={
            "branch_id": str(ids["branch_id"]), "client_id": str(ids["client_id"]),
            "items": [{
                "professional_id": str(ids["prof_b_id"]), "service_id": str(ids["service_id"]),
                "start_at": _dt(14).isoformat(),
            }],
        },
        headers=owner_headers,
    )
    assert appt_b.status_code == 201, appt_b.text

    restricted_body = _login(client, ids["restricted_email"], ids["password"])
    restricted_headers = _auth_headers(restricted_body["tokens"]["access_token"])

    # prof_a está liberado -> visível
    resp_a = client.get(f"/api/v1/appointments/{appt_a.json()['id']}", headers=restricted_headers)
    assert resp_a.status_code == 200, resp_a.text

    # prof_b NÃO está no grant -> 404 direto no endpoint, apesar de ter
    # agenda.view_all como permission grosseira
    resp_b = client.get(f"/api/v1/appointments/{appt_b.json()['id']}", headers=restricted_headers)
    assert resp_b.status_code == 404, resp_b.text

    # e mesmo o visível (prof_a) não pode ser EDITADO (edit_scope
    # SELECTED sem nenhum grant can_edit=True) -> 403, não 404
    edit_resp = client.patch(
        f"/api/v1/appointments/{appt_a.json()['id']}/status",
        json={"status": "confirmed"},
        headers=restricted_headers,
    )
    assert edit_resp.status_code == 403, edit_resp.text


# ---------------------------------------------------------------------
# Verificação direcionada (pós-aprovação da Etapa A): o default ALL de
# agenda_view_scope/agenda_edit_scope NUNCA amplia permissão nenhuma —
# ele só remove uma restrição ADICIONAL, deixando a decisão inteira nas
# mãos das permissions grosseiras (agenda.view_own/view_all/edit) já
# existentes. "Escopo ALL" = "sem filtro extra", nunca = "acesso total
# concedido pelo escopo". Cobre exatamente os 5 pontos pedidos:
#   1. agenda.view_own sozinho + scope ALL -> só a própria agenda.
#   2. agenda.view_all + scope SELECTED -> só os profissionais
#      selecionados (agenda.view_all não "vaza" pro que está fora do
#      SELECTED).
#   3. scope ALL nunca concede permissão global sozinho (sem
#      agenda.view_own/view_all, ALL não muda nada: continua vazio).
#   4. scope é sempre uma restrição ADICIONAL sobre o RBAC, nunca uma
#      concessão — SELECTED vazio nunca é "sobrescrito" por
#      agenda.view_all.
#   5. editar continua subconjunto de visualizar mesmo com ambos os
#      escopos em ALL (o default).
# ---------------------------------------------------------------------


def test_default_orm_da_membership_e_all_all_e_resolve_para_none(org_session):
    """O valor default do MODELO (não só o que um teste passa à mão
    pra `ActorContext`) é ALL/ALL, e `resolve_*` traduz isso pra `None`
    — "sem restrição adicional", o sinal que o resto do sistema usa
    pra dizer "decida pela permission grosseira"."""
    session, org_id = org_session
    role = _role(session, org_id)
    membership = _membership(session, org_id, role.id)
    assert membership.agenda_view_scope == AgendaAccessScope.ALL
    assert membership.agenda_edit_scope == AgendaAccessScope.ALL
    assert agenda_access.resolve_viewable_ids(session, membership) is None
    assert agenda_access.resolve_editable_ids(session, membership) is None


def test_view_own_com_scope_all_default_continua_vendo_so_a_propria_agenda(org_session):
    """Ponto 1 do pedido: ainda que o escopo estrutural seja ALL
    (default, sem nenhuma linha em membership_agenda_grants), um ator
    com só `agenda.view_own` continua restrito à própria agenda — o
    ALL do escopo não vira, por si só, um `agenda.view_all`."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)
    _create_appointment(session, org_id, owner, branch, prof_b, service, client, 14)

    # scope ALL default -> viewable=None (idêntico ao que resolve_viewable_ids devolveria)
    prof_a_actor = _actor(
        session, org_id, permissions={"agenda.view_own"}, professional_id=prof_a.id, viewable=None,
    )
    items = agenda.list_agenda(session, prof_a_actor, date_from=_dt(0), date_to=_dt(23, 59))
    assert {i.professional_id for i in items} == {prof_a.id}

    # e get_appointment nega (404) o agendamento do colega, mesmo o
    # escopo ESTRUTURAL sendo ALL — só a permission decide aqui.
    from nexasalon_api.schemas.appointment import AppointmentCreate as _AC
    from nexasalon_api.schemas.appointment import AppointmentItemCreate as _AIC

    appt_b = appointments.create_appointment(
        session, owner,
        _AC(
            branch_id=branch.id, client_id=client.id,
            items=[_AIC(professional_id=prof_b.id, service_id=service.id, start_at=_dt(16))],
        ),
    )
    with pytest.raises(NotFoundError):
        appointments.get_appointment(session, prof_a_actor, appt_b.id)


def test_view_all_com_scope_selected_ve_so_os_profissionais_selecionados(org_session):
    """Ponto 2 do pedido: `agenda.view_all` NÃO "vaza" para fora do
    SELECTED — o escopo concreto substitui a checagem grosseira, nunca
    convive com ela como um OU."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)
    _create_appointment(session, org_id, owner, branch, prof_b, service, client, 14)

    actor = _actor(
        session, org_id, permissions={"agenda.view_all"}, viewable=frozenset({prof_a.id}),
    )
    items = agenda.list_agenda(session, actor, date_from=_dt(0), date_to=_dt(23, 59))
    assert {i.professional_id for i in items} == {prof_a.id}
    assert prof_b.id not in {i.professional_id for i in items}


def test_scope_all_nunca_concede_permissao_sozinho_sem_view_own_nem_view_all(org_session):
    """Ponto 3 do pedido: sem NENHUMA permission de visualização
    (nem view_own, nem view_all), o escopo estrutural ALL não concede
    nada — continua vendo zero agendamentos, exatamente como
    `test_sem_view_own_nem_view_all_lista_vazia` já provava em
    test_agenda.py antes desta feature existir (comportamento
    preservado, não uma regressão nova)."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt_a = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)

    no_perms_actor = _actor(session, org_id, permissions=set(), viewable=None)
    assert agenda.list_agenda(session, no_perms_actor, date_from=_dt(0), date_to=_dt(23, 59)) == []
    with pytest.raises(NotFoundError):
        appointments.get_appointment(session, no_perms_actor, appt_a.id)
    assert agenda_access.can_view_professional(no_perms_actor, prof_a.id) is False


def test_scope_e_sempre_restricao_adicional_nunca_concessao(org_session):
    """Ponto 4 do pedido: um `agenda.view_all` de verdade, combinado com
    um escopo SELECTED vazio, continua vendo zero — o escopo restringe
    por CIMA da permission, nunca "релaxa" o que já foi restringido.
    Escopo largo (ALL) nunca é necessário pra a permission valer, e
    escopo estreito (SELECTED vazio) sempre vence sobre uma permission
    larga."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)

    actor = _actor(session, org_id, permissions={"agenda.view_all"}, viewable=frozenset())
    assert agenda.list_agenda(session, actor, date_from=_dt(0), date_to=_dt(23, 59)) == []
    assert agenda_access.can_view_professional(actor, prof_a.id) is False


def test_editar_continua_subconjunto_de_visualizar_mesmo_com_scope_all_default(org_session):
    """Ponto 5 do pedido: com AMBOS os escopos no default ALL, um ator
    que só tem `agenda.edit` (sem view_own nem view_all) — cenário
    hipotético de role mal configurada, mas exatamente o caso que a
    regra "editar é subconjunto de visualizar" existe pra cobrir —
    continua sem poder editar NADA, porque `can_edit_professional` cai
    em `can_view_professional`, que nega por falta de permission de
    visualização. O escopo ALL não bypassa essa regra."""
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt_a = _create_appointment(session, org_id, owner, branch, prof_a, service, client, 10)

    edit_only_actor = _actor(
        session, org_id, permissions={"agenda.edit", "agenda.view_all"}, viewable=None, editable=None,
    )
    # agenda.view_all presente -> ENXERGA o agendamento (visualizar ok)...
    assert appointments.get_appointment(session, edit_only_actor, appt_a.id).id == appt_a.id

    edit_only_sem_view_actor = _actor(
        session, org_id, permissions={"agenda.edit"}, viewable=None, editable=None,
    )
    # ...mas SEM nenhuma permission de visualização, mesmo tendo
    # agenda.edit e ambos os escopos em ALL, nem consegue ENXERGAR o
    # agendamento pra chegar perto de editar: 404 (convenção anti-leak
    # de `get_appointment`, chamado antes de `_assert_can_edit` em
    # `update_status`) — nunca um "passa direto porque o escopo é ALL".
    with pytest.raises(NotFoundError):
        appointments.update_status(session, edit_only_sem_view_actor, appt_a.id, AppointmentStatus.CONFIRMED)
    # e, isolando só a checagem de edição (sem passar pelo 404 de
    # visualização), can_edit_professional confirma: nega, porque cai
    # em can_view_professional, que nega por falta de view_own/view_all.
    assert agenda_access.can_edit_professional(edit_only_sem_view_actor, prof_a.id) is False
