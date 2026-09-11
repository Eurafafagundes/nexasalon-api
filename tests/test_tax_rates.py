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
from nexasalon_api.schemas.tax_rate import TaxRateHistoryStatus, TaxRateSet
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


def _add_months(month: date, n: int) -> date:
    total = month.year * 12 + (month.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def _set(session, actor, competence: date, tax_rate: str, *, current: date):
    tax_rates_service.set_rate(
        session, actor,
        TaxRateSet(competence_month=competence, tax_rate=tax_rate, confirm_past=competence < current),
    )


# ---------------------------------------------------------------------
# `list_history` — histórico paginado (exclui a vigente).
# ---------------------------------------------------------------------


def test_history_somente_vigente_historico_vazio(org_session):
    """Contrato explícito pro estado vazio — NUNCA `page=0`, mesmo
    quando `total_pages=0`: `page=1` sempre, seja qual for o `page`
    pedido (o clamp `min(max(page,1), total_pages)` só vale quando
    `total_pages>0`; com `total_pages=0` o resultado é sempre `1`,
    nunca `0`, que não seria uma página válida pra exibir)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.items == []
    assert page.page == 1
    assert page.page_size == 12
    assert page.total == 0
    assert page.total_pages == 0
    assert page.has_programmed is False

    # Mesmo pedindo uma página maluca (ex.: usuário ainda com a página 3
    # aberta de antes de tudo ser excluído) — continua devolvendo page=1,
    # nunca 0 nem um erro.
    page_requested_high = tax_rates_service.list_history(session, org_id, page=3)
    assert page_requested_high.page == 1
    assert page_requested_high.total_pages == 0


def test_history_vigente_mais_uma_historica(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    anterior = _add_months(current, -3)
    _set(session, actor, anterior, "5.00", current=current)
    _set(session, actor, current, "6.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.total == 1
    assert len(page.items) == 1
    row = page.items[0]
    assert row.competence_month == anterior
    assert row.tax_rate == 5
    assert row.status == TaxRateHistoryStatus.ENCERRADA
    # Vigorou até o mês IMEDIATAMENTE anterior ao início da vigente.
    assert row.effective_until == _add_months(current, -1)
    # Só encerradas — a UI usa isso pra manter o rótulo "Ver histórico
    # de alíquotas", sem misturar com "programadas".
    assert page.has_programmed is False


def test_history_vigente_mais_competencia_futura_nunca_e_encerrada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    futura = _add_months(current, 4)
    _set(session, actor, current, "6.00", current=current)
    _set(session, actor, futura, "9.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.total == 1  # a futura ENTRA na contagem apresentada — só não é "encerrada".
    row = page.items[0]
    assert row.competence_month == futura
    assert row.status == TaxRateHistoryStatus.PROGRAMADA  # NUNCA "encerrada".
    assert row.effective_until is None  # nada depois dela ainda — vigência em aberto.
    assert page.has_programmed is True  # sinaliza pra UI trocar o rótulo do card.


def test_history_vigente_excluida_da_contagem_mesmo_com_muitas_linhas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    for i in range(1, 6):
        _set(session, actor, _add_months(current, -i), f"{i}.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.total == 5  # a vigente (6ª linha cadastrada) nunca conta.
    assert all(row.competence_month != current for row in page.items)


def test_history_apenas_encerradas_nao_sinaliza_programadas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    for i in range(1, 4):
        _set(session, actor, _add_months(current, -i), f"{i}.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.total == 3
    assert all(row.status == TaxRateHistoryStatus.ENCERRADA for row in page.items)
    assert page.has_programmed is False


def test_history_has_programmed_reflete_o_conjunto_completo_mesmo_fora_da_pagina_atual(org_session):
    """A linha `programada` (mais recente de todas, cronologicamente —
    sempre cai na página 1, já que a ordenação é da mais recente pra
    mais antiga) precisa continuar sinalizando `has_programmed=True`
    até na página 2, que só tem competências antigas e nenhuma
    programada — o rótulo do card reflete o TOTAL, nunca só a página
    pedida."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    for i in range(1, 13):
        _set(session, actor, _add_months(current, -i), f"{i}.00", current=current)
    futura = _add_months(current, 6)
    _set(session, actor, futura, "9.00", current=current)

    page1 = tax_rates_service.list_history(session, org_id, page=1)
    assert page1.total == 13
    assert page1.total_pages == 2
    assert page1.items[0].status == TaxRateHistoryStatus.PROGRAMADA  # a mais recente de todas — sempre no topo da página 1.
    assert page1.has_programmed is True

    page2 = tax_rates_service.list_history(session, org_id, page=2)
    assert all(row.status == TaxRateHistoryStatus.ENCERRADA for row in page2.items)  # nenhuma programada NESTA página.
    assert page2.has_programmed is True  # mas o sinalizador continua True — reflete o total, não a página.


def test_history_doze_historicas_cabem_todas_numa_pagina(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    for i in range(1, 13):
        _set(session, actor, _add_months(current, -i), f"{i}.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.total == 12
    assert page.total_pages == 1
    assert len(page.items) == 12


def test_history_treze_historicas_gera_duas_paginas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    for i in range(1, 14):
        _set(session, actor, _add_months(current, -i), f"{i}.00", current=current)

    page1 = tax_rates_service.list_history(session, org_id, page=1)
    assert page1.total == 13
    assert page1.total_pages == 2
    assert len(page1.items) == 12

    page2 = tax_rates_service.list_history(session, org_id, page=2)
    assert page2.total == 13
    assert page2.total_pages == 2
    assert len(page2.items) == 1
    assert page2.page == 2
    # A página 2 tem a linha MAIS ANTIGA (13 meses atrás) — nunca uma repetida da página 1.
    assert page2.items[0].competence_month == _add_months(current, -13)
    assert page2.items[0].competence_month not in [row.competence_month for row in page1.items]


def test_history_ordenacao_da_mais_recente_para_a_mais_antiga(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    _set(session, actor, _add_months(current, -1), "5.50", current=current)
    _set(session, actor, _add_months(current, -6), "5.00", current=current)
    _set(session, actor, _add_months(current, -12), "4.50", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    months = [row.competence_month for row in page.items]
    assert months == sorted(months, reverse=True)
    assert months == [_add_months(current, -1), _add_months(current, -6), _add_months(current, -12)]


def test_history_vigencia_calculada_em_cadeia_cronologica(org_session):
    """Jan/2026 5,50% seguida por Set/2026 (vigente) 6,00% — Jan vigorou
    até Ago/2026 (mês imediatamente anterior ao início da vigente),
    exatamente como no exemplo do pedido."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    jan = _add_months(current, -8)
    _set(session, actor, jan, "5.50", current=current)
    _set(session, actor, current, "6.00", current=current)

    page = tax_rates_service.list_history(session, org_id, page=1)
    assert page.total == 1
    row = page.items[0]
    assert row.competence_month == jan
    assert row.effective_until == _add_months(current, -1)
    assert row.status == TaxRateHistoryStatus.ENCERRADA


def test_history_pagina_fora_de_alcance_e_ajustada_sem_erro(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    current = tax_rates_service._current_competence_month()
    _set(session, actor, current, "6.00", current=current)
    _set(session, actor, _add_months(current, -1), "5.50", current=current)

    page = tax_rates_service.list_history(session, org_id, page=5)
    assert page.page == 1  # clampado — só existe 1 página com esses dados.
    assert page.total_pages == 1
    assert len(page.items) == 1


def test_http_history_endpoint_pagina_e_respeita_permissao(org_a_actor, client_as):
    client = client_as(org_a_actor)
    current = tax_rates_service._current_competence_month()
    # Linha na competência ATUAL — é ela que vira a vigente; sem isso, a
    # mais recente das 13 linhas abaixo (i=1) assumiria o papel de
    # vigente e sobrariam só 12 no histórico, não 13.
    resp = client.put("/api/v1/tax-rates", json={"competence_month": current.isoformat(), "tax_rate": "6.00"})
    assert resp.status_code == 200, resp.text
    for i in range(1, 14):
        competence = _add_months(current, -i)
        resp = client.put(
            "/api/v1/tax-rates",
            json={"competence_month": competence.isoformat(), "tax_rate": "5.00", "confirm_past": True},
        )
        assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/tax-rates/history", params={"page": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_size"] == 12
    assert body["total"] == 13
    assert body["total_pages"] == 2
    assert len(body["items"]) == 12


def test_http_history_sem_organization_manage_recebe_403(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"dashboard.view"}))
    client = client_as(restricted)
    resp = client.get("/api/v1/tax-rates/history")
    assert resp.status_code == 403


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
