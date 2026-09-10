"""Testes de `services/tax_rates.py` — alíquota de imposto provisionada
por competência mensal (painel "Resultado disponível" do Dashboard).

Foco: a regra inegociável do produto — "alterar a alíquota atual nunca
recalcula uma competência já fechada no passado" — e a herança/vigência
(mês sem linha própria usa a última alíquota conhecida). Mesmo padrão
de `test_dashboard.py`/`test_dashboard_bi_update.py`: direto no service
layer via `SessionLocal`, fixtures duplicadas localmente."""
import uuid
from dataclasses import replace
from datetime import date, timedelta, timezone

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Organization
from nexasalon_api.schemas.tax_rate import TaxRateSet
from nexasalon_api.services import tax_rates as tax_rates_service

_TZ = timezone(timedelta(hours=-3))


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org fiscal", slug=f"org-fiscal-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=frozenset({"organization.manage"})) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def test_uma_competencia_com_uma_aliquota_resolve_direto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2026, 9, 1), tax_rate="6.00"))

    resolved = tax_rates_service.get_effective_rate(session, org_id, date(2026, 9, 15))
    assert resolved is not None
    assert resolved.tax_rate == 6


def test_mudanca_de_aliquota_no_mes_seguinte_nao_altera_o_mes_anterior(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2026, 9, 1), tax_rate="6.00"))
    tax_rates_service.set_rate(
        session, actor, TaxRateSet(competence_month=date(2026, 10, 1), tax_rate="8.00", confirm_past=True)
    )

    setembro = tax_rates_service.get_effective_rate(session, org_id, date(2026, 9, 1))
    outubro = tax_rates_service.get_effective_rate(session, org_id, date(2026, 10, 1))
    assert setembro.tax_rate == 6
    assert outubro.tax_rate == 8


def test_heranca_mes_sem_linha_propria_usa_a_ultima_conhecida(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    following = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=current, tax_rate="6.00"))

    inherited = tax_rates_service.get_effective_rate(session, org_id, following)
    assert inherited is not None
    assert inherited.tax_rate == 6
    assert inherited.competence_month == current


def test_sem_nenhuma_configuracao_devolve_none_nunca_inventa_zero(org_session):
    session, org_id = org_session
    resolved = tax_rates_service.get_effective_rate(session, org_id, date(2026, 9, 1))
    assert resolved is None


def test_competencia_futura_nunca_vaza_para_competencia_passada(org_session):
    """Configurar Outubro não pode retroativamente "aparecer" como a
    alíquota vigente de Setembro — herança só olha pra TRÁS."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    tax_rates_service.set_rate(
        session, actor, TaxRateSet(competence_month=date(2026, 10, 1), tax_rate="8.00", confirm_past=True)
    )

    setembro = tax_rates_service.get_effective_rate(session, org_id, date(2026, 9, 1))
    assert setembro is None


def test_editar_competencia_passada_sem_confirmar_e_rejeitado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2020, 1, 1), tax_rate="5.00", confirm_past=True))

    with pytest.raises(ConflictError):
        tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2020, 1, 1), tax_rate="7.00"))


def test_editar_competencia_passada_com_confirmacao_explicita_funciona(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2020, 1, 1), tax_rate="5.00", confirm_past=True))

    updated = tax_rates_service.set_rate(
        session, actor, TaxRateSet(competence_month=date(2020, 1, 1), tax_rate="7.00", confirm_past=True)
    )
    assert updated.tax_rate == 7


def test_criar_competencia_futura_ou_atual_nunca_exige_confirmacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    # Não levanta ConflictError mesmo sem confirm_past=True.
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2099, 1, 1), tax_rate="6.00"))


def test_qualquer_dia_do_mes_informado_e_normalizado_pro_primeiro_dia(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    rate = tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2026, 9, 17), tax_rate="6.00"))
    assert rate.competence_month == date(2026, 9, 1)


def test_isolamento_entre_organizacoes(org_session):
    session, org_a = org_session
    actor_a = _actor(session, org_a)
    tax_rates_service.set_rate(session, actor_a, TaxRateSet(competence_month=date(2026, 9, 1), tax_rate="6.00"))

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B fiscal", slug=f"org-b-fiscal-{org_b.hex[:8]}"))
    session.flush()

    resolved_for_b = tax_rates_service.get_effective_rate(session, org_b, date(2026, 9, 1))
    assert resolved_for_b is None  # nunca vê a alíquota da Org A.


def test_months_between_cobre_todas_as_competencias_do_intervalo():
    from datetime import datetime

    date_from = datetime(2026, 9, 15, 10, 0, tzinfo=_TZ)
    date_to = datetime(2026, 11, 1, 0, 0, tzinfo=_TZ)  # EXCLUSIVE — não deve incluir novembro.
    months = tax_rates_service.months_between(date_from, date_to)
    assert months == [date(2026, 9, 1), date(2026, 10, 1)]


def test_resolve_rates_for_months_aplica_a_aliquota_correta_por_competencia(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=date(2026, 9, 1), tax_rate="6.00"))
    tax_rates_service.set_rate(
        session, actor, TaxRateSet(competence_month=date(2026, 10, 1), tax_rate="8.00", confirm_past=True)
    )

    resolutions = tax_rates_service.resolve_rates_for_months(
        session, org_id, [date(2026, 9, 1), date(2026, 10, 1)]
    )
    assert resolutions[date(2026, 9, 1)].tax_rate == 6
    assert resolutions[date(2026, 10, 1)].tax_rate == 8


def test_http_sem_organization_manage_recebe_403_ao_editar(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"dashboard.view"}))
    client = client_as(restricted)
    resp = client.put("/api/v1/tax-rates", json={"competence_month": "2026-09-01", "tax_rate": "6.00"})
    assert resp.status_code == 403


def test_http_com_organization_manage_cria_e_lista(org_a_actor, client_as):
    client = client_as(org_a_actor)
    resp = client.put("/api/v1/tax-rates", json={"competence_month": "2026-09-01", "tax_rate": "6.00"})
    assert resp.status_code == 200
    assert resp.json()["tax_rate"] == "6.00"

    listed = client.get("/api/v1/tax-rates")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
