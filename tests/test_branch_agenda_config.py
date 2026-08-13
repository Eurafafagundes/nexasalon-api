"""Testes de `Branch.agenda_view_start/agenda_view_end/agenda_slot_minutes`
(Etapa 3B — segunda rodada: eliminar a janela de horas hardcoded do
frontend e substituir por configuração real por unidade).

Cobre exatamente o que foi pedido:
  - unidades diferentes podem ter janelas diferentes;
  - unidades diferentes podem usar grade de 15 ou 30 minutos;
  - `agenda_slot_minutes` só aceita 15 ou 30;
  - estas configurações são só apresentação: `WorkingHours` continua
    sendo a única fonte de disponibilidade real (o endpoint de
    disponibilidade ignora completamente estes 3 campos do Branch).
"""
import uuid
from datetime import date, time, timedelta

import pytest
from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.services import availability


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org agenda config", slug=f"org-cfg-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _make_branch(session, org_id, **overrides) -> Branch:
    branch = Branch(
        organization_id=org_id,
        name=overrides.pop("name", "Unidade Teste"),
        slug=overrides.pop("slug", f"unidade-{uuid.uuid4().hex[:8]}"),
        **overrides,
    )
    session.add(branch)
    session.flush()
    return branch


# ---------------------------------------------------------------------
# Nível banco/model: defaults de compatibilidade + CHECK constraints
# ---------------------------------------------------------------------


def test_branch_usa_defaults_de_compatibilidade_quando_nao_informado(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    session.refresh(branch)
    assert branch.agenda_view_start == time(7, 0)
    assert branch.agenda_view_end == time(21, 0)
    assert branch.agenda_slot_minutes == 30


def test_unidades_diferentes_podem_ter_janelas_diferentes(org_session):
    session, org_id = org_session
    manha = _make_branch(
        session, org_id, name="Unidade Manhã", agenda_view_start=time(6, 0), agenda_view_end=time(14, 0)
    )
    noite = _make_branch(
        session, org_id, name="Unidade Noite", agenda_view_start=time(14, 0), agenda_view_end=time(23, 0)
    )
    session.refresh(manha)
    session.refresh(noite)
    assert (manha.agenda_view_start, manha.agenda_view_end) == (time(6, 0), time(14, 0))
    assert (noite.agenda_view_start, noite.agenda_view_end) == (time(14, 0), time(23, 0))


def test_unidades_diferentes_podem_usar_granularidade_diferente(org_session):
    session, org_id = org_session
    fina = _make_branch(session, org_id, name="Unidade 15min", agenda_slot_minutes=15)
    grossa = _make_branch(session, org_id, name="Unidade 30min", agenda_slot_minutes=30)
    session.refresh(fina)
    session.refresh(grossa)
    assert fina.agenda_slot_minutes == 15
    assert grossa.agenda_slot_minutes == 30


def test_agenda_slot_minutes_so_aceita_15_ou_30_no_banco(org_session):
    session, org_id = org_session
    branch = Branch(
        organization_id=org_id,
        name="Unidade Inválida",
        slug=f"unidade-{uuid.uuid4().hex[:8]}",
        agenda_slot_minutes=45,
    )
    session.add(branch)
    with pytest.raises(Exception):
        session.flush()
    session.rollback()


def test_agenda_view_start_deve_ser_antes_do_end_no_banco(org_session):
    session, org_id = org_session
    branch = Branch(
        organization_id=org_id,
        name="Unidade Invertida",
        slug=f"unidade-{uuid.uuid4().hex[:8]}",
        agenda_view_start=time(20, 0),
        agenda_view_end=time(8, 0),
    )
    session.add(branch)
    with pytest.raises(Exception):
        session.flush()
    session.rollback()


# ---------------------------------------------------------------------
# Nível API: schema valida 15/30, e a leitura devolve o que foi salvo
# ---------------------------------------------------------------------


def test_api_cria_e_le_unidade_com_configuracao_de_agenda_propria(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/branches",
        json={
            "name": "Unidade Express",
            "slug": "unidade-express",
            "agenda_view_start": "06:30:00",
            "agenda_view_end": "13:00:00",
            "agenda_slot_minutes": 15,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agenda_view_start"] == "06:30:00"
    assert body["agenda_view_end"] == "13:00:00"
    assert body["agenda_slot_minutes"] == 15

    resp = c.get(f"/api/v1/branches/{body['id']}")
    assert resp.json()["agenda_slot_minutes"] == 15


def test_api_rejeita_agenda_slot_minutes_fora_de_15_ou_30(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/branches",
        json={"name": "Unidade Ruim", "slug": "unidade-ruim", "agenda_slot_minutes": 45},
    )
    assert resp.status_code == 422, resp.text


def test_api_rejeita_janela_com_inicio_depois_do_fim(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/branches",
        json={
            "name": "Unidade Ruim 2",
            "slug": "unidade-ruim-2",
            "agenda_view_start": "20:00:00",
            "agenda_view_end": "08:00:00",
        },
    )
    assert resp.status_code == 422, resp.text


def test_api_atualiza_configuracao_de_agenda_da_unidade(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = c.post("/api/v1/branches", json={"name": "Unidade Y", "slug": "unidade-y"}).json()["id"]

    resp = c.put(
        f"/api/v1/branches/{branch_id}",
        json={
            "name": "Unidade Y",
            "slug": "unidade-y",
            "agenda_view_start": "10:00:00",
            "agenda_view_end": "22:00:00",
            "agenda_slot_minutes": 15,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agenda_view_start"] == "10:00:00"
    assert body["agenda_view_end"] == "22:00:00"
    assert body["agenda_slot_minutes"] == 15


# ---------------------------------------------------------------------
# A configuração de apresentação NÃO é disponibilidade real: WorkingHours
# continua mandando, mesmo quando a janela/granularidade da unidade diz
# outra coisa.
# ---------------------------------------------------------------------

_THURSDAY = date(2026, 8, 13)
_OUR_THURSDAY = 4  # 0=domingo..6=sábado; 13/08/2026 é quinta


def test_working_hours_continua_determinando_disponibilidade_real_independente_da_config_de_apresentacao(
    org_session,
):
    session, org_id = org_session
    # Unidade configurada para EXIBIR só 10h-12h na grade — mas o
    # profissional trabalha 06:00-09:00, fora dessa janela de apresentação.
    branch = _make_branch(
        session, org_id, agenda_view_start=time(10, 0), agenda_view_end=time(12, 0), agenda_slot_minutes=30
    )
    prof = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional Madrugador")
    session.add(prof)
    session.flush()
    service = Service(organization_id=org_id, name="Corte", default_duration_minutes=30, default_price=50)
    session.add(service)
    session.flush()
    session.add(ProfessionalService(professional_id=prof.id, service_id=service.id))
    session.add(
        WorkingHours(
            organization_id=org_id,
            professional_id=prof.id,
            weekday=_OUR_THURSDAY,
            start_time=time(6, 0),
            end_time=time(9, 0),
        )
    )
    session.flush()

    # slot_minutes aqui é o parâmetro do endpoint de disponibilidade
    # (granularidade do cálculo real de horários), INDEPENDENTE de
    # `branch.agenda_slot_minutes` (que é só apresentação da grade).
    slots = availability.compute_availability(
        session,
        org_id,
        branch_id=branch.id,
        professional_id=prof.id,
        service_id=service.id,
        target_date=_THURSDAY,
        slot_minutes=30,
    )

    assert len(slots) > 0
    starts = sorted(s.start_at.time() for s in slots)
    # os horários reais vêm de WorkingHours (06:00-09:00) — totalmente
    # fora da janela de apresentação da unidade (10:00-12:00). Isso prova
    # que agenda_view_start/end não vaza pra disponibilidade real.
    assert starts[0] == time(6, 0)
    assert all(t < time(10, 0) for t in starts)
    for s in slots:
        assert (s.end_at - s.start_at) == timedelta(minutes=30)
