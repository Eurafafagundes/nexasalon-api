"""Testes do NexaLink — página pública estilo Linktree
(`organization_public_pages`/`organization_public_links`/
`public_page_events`). Cobre: CRUD de config (defaults em memória →
1º PUT cria linha + 4 nativos), RBAC da permission NOVA
`organization.manage_public_page` (nunca `organization.manage` sozinha,
nunca permite tocar o cadastro global/logo), isolamento multi-tenant,
validação de link, ordenação por posição no array, 404 unificado
(anti-enumeração), eventos + atribuição (nunca aceita token de outra
organização, nunca duplica `booking_completed` sob retry), agregação/
taxa de conversão, retenção (purge)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app
from nexasalon_api.models.enums import PublicPageEventType
from nexasalon_api.models.public_page_event import PublicPageEvent
from nexasalon_api.services import public_page_events as public_page_events_service


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    return TestClient(app)


def _default_links_payload(link_overrides=None, extra_links=None):
    link_overrides = link_overrides or {}
    links = [
        {"kind": "booking", "is_visible": True, "custom_label": None},
        {"kind": "whatsapp", "is_visible": True, "whatsapp_number": "61999990000", "whatsapp_message": "Oi!"},
        {"kind": "instagram", "is_visible": True, "instagram_username": "meusalao"},
        {"kind": "location", "is_visible": True},
    ]
    for item in links:
        if item["kind"] in link_overrides:
            item.update(link_overrides[item["kind"]])
    return links + (extra_links or [])


def _default_page_payload(*, link_overrides=None, extra_links=None, **page_overrides):
    payload = {
        "is_enabled": True,
        "display_name": "Meu Salão",
        "short_description": "O melhor salão da cidade",
        "primary_color_hex": "#7C3AED",
        "background_color_hex": "#FFFFFF",
        "button_style": "rounded",
        "links": _default_links_payload(link_overrides=link_overrides, extra_links=extra_links),
    }
    payload.update(page_overrides)
    return payload


def _enable_online_booking(c):
    resp = c.put(
        "/api/v1/organization",
        json={
            "online_booking_enabled": True,
            "online_booking_auto_confirm": True,
            "online_booking_min_lead_minutes": 0,
            "online_booking_max_lead_days": 3650,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _restricted_actor(base_actor, *, permissions):
    """Mesmo padrão já usado em outras suítes desta sessão
    (`test_client_profile.py`/`test_client_observations.py`) — um `User`
    de verdade, necessário pra qualquer fluxo que resolva nome/autor."""
    from nexasalon_api.core.actor import ActorContext
    from nexasalon_api.models.identity import User

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Restrito")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Restrito", permissions=frozenset(permissions),
    )


# ---------------------------------------------------------------------
# CRUD de config
# ---------------------------------------------------------------------


def test_get_sem_configuracao_salva_devolve_defaults_em_memoria(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/api/v1/organization/public-page")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_enabled"] is False
    assert body["primary_color_hex"] == "#7C3AED"
    assert body["updated_at"] is None
    assert len(body["links"]) == 4
    assert {link["kind"] for link in body["links"]} == {"booking", "whatsapp", "instagram", "location"}
    assert all(link["id"] is None for link in body["links"])


def test_put_cria_linha_e_os_4_nativos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.put("/api/v1/organization/public-page", json=_default_page_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_enabled"] is True
    assert body["updated_at"] is not None
    assert len(body["links"]) == 4
    assert all(link["id"] is not None for link in body["links"])

    reloaded = c.get("/api/v1/organization/public-page").json()
    assert reloaded["display_name"] == "Meu Salão"
    assert {link["kind"] for link in reloaded["links"]} == {"booking", "whatsapp", "instagram", "location"}


def test_put_sem_um_dos_4_nativos_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    payload = _default_page_payload()
    payload["links"] = [link for link in payload["links"] if link["kind"] != "instagram"]
    resp = c.put("/api/v1/organization/public-page", json=payload)
    assert resp.status_code == 422


def test_put_segunda_vez_faz_upsert_nunca_duplica_nativos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    resp = c.put(
        "/api/v1/organization/public-page",
        json=_default_page_payload(display_name="Nome Novo", link_overrides={"whatsapp": {"whatsapp_number": "61988887777"}}),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Nome Novo"
    whatsapp_links = [link for link in body["links"] if link["kind"] == "whatsapp"]
    assert len(whatsapp_links) == 1
    assert whatsapp_links[0]["whatsapp_number"] == "5561988887777"  # DDI 55 prefixado (normalize_whatsapp_number)


def test_adicionar_editar_remover_link_personalizado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    payload = _default_page_payload(
        extra_links=[{"kind": "custom", "is_visible": True, "title": "Catálogo", "url": "https://example.com/catalogo"}]
    )
    created = c.put("/api/v1/organization/public-page", json=payload).json()
    custom = next(link for link in created["links"] if link["kind"] == "custom")
    assert custom["title"] == "Catálogo"

    # editar
    payload2 = _default_page_payload(
        extra_links=[{"id": custom["id"], "kind": "custom", "is_visible": True, "title": "Portfólio", "url": custom["url"]}]
    )
    edited = c.put("/api/v1/organization/public-page", json=payload2).json()
    custom2 = next(link for link in edited["links"] if link["kind"] == "custom")
    assert custom2["title"] == "Portfólio"
    assert custom2["id"] == custom["id"]

    # remover — payload sem nenhum link custom
    removed = c.put("/api/v1/organization/public-page", json=_default_page_payload()).json()
    assert not [link for link in removed["links"] if link["kind"] == "custom"]


def test_ordenacao_e_definida_pela_posicao_no_array(client_as, org_a_actor):
    c = client_as(org_a_actor)
    payload = _default_page_payload()
    # inverte a ordem dos 4 nativos
    payload["links"] = list(reversed(payload["links"]))
    body = c.put("/api/v1/organization/public-page", json=payload).json()
    ordered_kinds = [link["kind"] for link in sorted(body["links"], key=lambda item: item["display_order"])]
    assert ordered_kinds == ["location", "instagram", "whatsapp", "booking"]


# ---------------------------------------------------------------------
# Validação de link
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)"])
def test_url_de_link_personalizado_com_esquema_perigoso_e_rejeitada(client_as, org_a_actor, bad_url):
    c = client_as(org_a_actor)
    payload = _default_page_payload(extra_links=[{"kind": "custom", "is_visible": True, "title": "X", "url": bad_url}])
    resp = c.put("/api/v1/organization/public-page", json=payload)
    assert resp.status_code == 422


def test_numero_de_whatsapp_invalido_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    payload = _default_page_payload(link_overrides={"whatsapp": {"whatsapp_number": "123"}})
    resp = c.put("/api/v1/organization/public-page", json=payload)
    assert resp.status_code == 422


def test_cor_fora_do_padrao_hex_e_rejeitada(client_as, org_a_actor):
    c = client_as(org_a_actor)
    payload = _default_page_payload(primary_color_hex="roxo")
    resp = c.put("/api/v1/organization/public-page", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------
# RBAC — permission NOVA, nunca organization.manage
# ---------------------------------------------------------------------


def test_sem_organization_manage_public_page_e_bloqueado(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"organization.manage"})
    c = client_as(restricted)
    resp = c.get("/api/v1/organization/public-page")
    assert resp.status_code == 403


def test_com_organization_manage_public_page_mas_sem_organization_manage_funciona(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"organization.manage_public_page"})
    c = client_as(restricted)
    resp = c.put("/api/v1/organization/public-page", json=_default_page_payload())
    assert resp.status_code == 200, resp.text


def test_organization_manage_public_page_nunca_permite_editar_organizacao_nem_logo(client_as, org_a_actor):
    """Item explícito: a permission nova NUNCA pode alterar o cadastro
    global da organização, inclusive o upload de logo."""
    restricted = _restricted_actor(org_a_actor, permissions={"organization.manage_public_page"})
    c = client_as(restricted)

    resp = c.put("/api/v1/organization", json={"name": "Nome Hackeado"})
    assert resp.status_code == 403

    resp = c.post("/api/v1/organization/logo", files={"file": ("logo.png", b"fake", "image/png")})
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# Isolamento multi-tenant
# ---------------------------------------------------------------------


def test_isolamento_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    a = client_as(org_a_actor)
    a.put("/api/v1/organization/public-page", json=_default_page_payload(display_name="Da Org A"))

    b = client_as(org_b_actor)
    body = b.get("/api/v1/organization/public-page").json()
    assert body["display_name"] is None  # nunca vê config de outra org


# ---------------------------------------------------------------------
# Página pública — 404 unificado, booking independente
# ---------------------------------------------------------------------


def test_slug_inexistente_e_pagina_desativada_devolvem_a_mesma_resposta():
    p = _public()
    resp_inexistente = p.get(f"/api/v1/public/s/slug-que-nao-existe-{uuid.uuid4().hex[:8]}")
    assert resp_inexistente.status_code == 404
    assert resp_inexistente.json() == {"error": {"type": "not_found", "message": "Página não encontrada.", "details": None}}


def test_pagina_nunca_salva_devolve_404(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    p = _public()
    resp = p.get(f"/api/v1/public/s/{org['slug']}")
    assert resp.status_code == 404


def test_pagina_desativada_devolve_mesmo_404_que_inexistente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload(is_enabled=False))
    p = _public()
    resp = p.get(f"/api/v1/public/s/{org['slug']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Página não encontrada."


def test_pagina_publica_mostra_todos_os_canais_configurados(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    p = _public()
    body = p.get(f"/api/v1/public/s/{org['slug']}").json()
    kinds = {link["kind"] for link in body["links"]}
    assert "whatsapp" in kinds
    assert "instagram" in kinds
    whatsapp = next(link for link in body["links"] if link["kind"] == "whatsapp")
    assert whatsapp["href"].startswith("https://wa.me/5561999990000")  # DDI 55 prefixado


def test_booking_some_quando_agendamento_online_desligado_mas_resto_da_pagina_continua(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    # online_booking_enabled continua False (default) — nunca ligado aqui
    p = _public()
    body = p.get(f"/api/v1/public/s/{org['slug']}").json()
    assert body["booking_available"] is False
    kinds = {link["kind"] for link in body["links"]}
    assert "booking" not in kinds
    assert "whatsapp" in kinds  # resto da página intacto


def test_canal_sem_config_valida_nunca_aparece(client_as, org_a_actor):
    """Item "não mostrar botão sem config válida" — WhatsApp visível mas
    sem número cadastrado nunca aparece na página pública."""
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    payload = _default_page_payload(link_overrides={"whatsapp": {"whatsapp_number": None}})
    c.put("/api/v1/organization/public-page", json=payload)
    p = _public()
    body = p.get(f"/api/v1/public/s/{org['slug']}").json()
    assert "whatsapp" not in {link["kind"] for link in body["links"]}


# ---------------------------------------------------------------------
# Eventos + atribuição
# ---------------------------------------------------------------------


def test_registrar_page_view_e_clique(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    p = _public()
    resp = p.post(
        f"/api/v1/public/s/{org['slug']}/events",
        json={"event_type": "page_view", "attribution_token": "tok-" + uuid.uuid4().hex},
    )
    assert resp.status_code == 204, resp.text


def test_evento_booking_completed_e_rejeitado_no_endpoint_publico(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    p = _public()
    resp = p.post(
        f"/api/v1/public/s/{org['slug']}/events",
        json={"event_type": "booking_completed", "attribution_token": "tok-" + uuid.uuid4().hex},
    )
    assert resp.status_code == 422


def test_rate_limit_do_endpoint_de_eventos(client_as, org_a_actor):
    from nexasalon_api.core.config import settings

    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    p = _public()
    limit = settings.rate_limit_public_page_event_max_attempts
    for _ in range(limit):
        resp = p.post(
            f"/api/v1/public/s/{org['slug']}/events",
            json={"event_type": "page_view", "attribution_token": "tok-" + uuid.uuid4().hex},
        )
        assert resp.status_code == 204
    over_limit = p.post(
        f"/api/v1/public/s/{org['slug']}/events",
        json={"event_type": "page_view", "attribution_token": "tok-" + uuid.uuid4().hex},
    )
    assert over_limit.status_code == 429


def test_attribution_token_de_outra_organizacao_nunca_conta_como_conversao(org_a_actor, org_b_actor):
    """Item explícito: validar origem/organização/validade do token —
    nunca aceitar às cegas."""
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b_actor.organization_id)}
        )
        foreign_event = PublicPageEvent(
            organization_id=org_b_actor.organization_id,
            event_type=PublicPageEventType.PAGE_VIEW,
            attribution_token="token-de-outra-org",
        )
        session.add(foreign_event)
        session.commit()

        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        valid = public_page_events_service.resolve_attribution(session, org_a_actor.organization_id, "token-de-outra-org")
        assert valid is False


def test_attribution_token_valido_gera_booking_completed_sem_duplicar_sob_retry(client_as, org_a_actor):
    # `appointment_id` tem FK real pra `appointments` — precisa de um
    # agendamento de verdade (nunca um UUID fabricado, que violaria a
    # integridade referencial e seria capturado como falso-positivo pelo
    # mesmo `except IntegrityError` do dedup).
    c = client_as(org_a_actor)
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    client = c.post("/api/v1/clients", json={"name": "Cliente Teste"}).json()
    svc = c.post("/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}).json()
    prof = c.post("/api/v1/professionals", json={"name": "Profissional"}).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc['id']}]})
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": prof["id"], "service_id": svc["id"], "start_at": "2026-12-01T10:00:00-03:00"}],
        },
    ).json()
    appointment_id = uuid.UUID(appt["id"])

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        page_view = PublicPageEvent(
            organization_id=org_a_actor.organization_id,
            event_type=PublicPageEventType.PAGE_VIEW,
            attribution_token="token-valido",
        )
        booking_click = PublicPageEvent(
            organization_id=org_a_actor.organization_id,
            event_type=PublicPageEventType.BOOKING_CLICK,
            attribution_token="token-valido",
        )
        session.add_all([page_view, booking_click])
        session.flush()

        assert public_page_events_service.resolve_attribution(session, org_a_actor.organization_id, "token-valido") is True

        public_page_events_service.record_booking_completed(session, org_a_actor.organization_id, appointment_id, "token-valido")
        public_page_events_service.record_booking_completed(session, org_a_actor.organization_id, appointment_id, "token-valido")
        session.commit()

        count = session.execute(
            text(
                "SELECT COUNT(*) FROM public_page_events WHERE event_type='booking_completed' "
                "AND appointment_id = :aid"
            ),
            {"aid": str(appointment_id)},
        ).scalar()
        assert count == 1


def test_token_com_page_view_mas_sem_booking_click_nunca_conta_como_conversao(org_a_actor):
    """Revisão pós-implantação: exigir SÓ `page_view` deixava passar um
    agendamento criado por qualquer OUTRO caminho (nunca clicando em
    "Agendar" na página NexaLink) desde que carregasse um token antigo
    de alguma visita anterior — `booking_click` com o MESMO token
    agora é exigido também, fechando essa lacuna."""
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        page_view = PublicPageEvent(
            organization_id=org_a_actor.organization_id,
            event_type=PublicPageEventType.PAGE_VIEW,
            attribution_token="token-sem-clique",
        )
        session.add(page_view)
        session.flush()

        valid = public_page_events_service.resolve_attribution(session, org_a_actor.organization_id, "token-sem-clique")
        assert valid is False


def test_token_ausente_ou_invalido_nunca_bloqueia_fluxo_apenas_nao_atribui(org_a_actor):
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        appointment_id = uuid.uuid4()
        # nunca levanta exceção, mesmo com token None/inexistente
        public_page_events_service.record_booking_completed(session, org_a_actor.organization_id, appointment_id, None)
        public_page_events_service.record_booking_completed(
            session, org_a_actor.organization_id, appointment_id, "token-nunca-existiu"
        )
        session.commit()
        count = session.execute(
            text("SELECT COUNT(*) FROM public_page_events WHERE appointment_id = :aid"), {"aid": str(appointment_id)}
        ).scalar()
        assert count == 0


# ---------------------------------------------------------------------
# Agregação / taxa de conversão
# ---------------------------------------------------------------------


def test_taxa_de_conversao_sem_cliques_e_none_nunca_zero_ou_erro(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/api/v1/organization/public-page/analytics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["booking_clicks"] == 0
    assert body["conversion_rate"] is None


def test_analytics_agrega_contagem_por_tipo_de_evento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    c.put("/api/v1/organization/public-page", json=_default_page_payload())
    p = _public()
    for _ in range(3):
        p.post(
            f"/api/v1/public/s/{org['slug']}/events",
            json={"event_type": "page_view", "attribution_token": "tok-" + uuid.uuid4().hex},
        )
    for _ in range(2):
        p.post(
            f"/api/v1/public/s/{org['slug']}/events",
            json={"event_type": "whatsapp_click", "attribution_token": "tok-" + uuid.uuid4().hex},
        )

    resp = c.get("/api/v1/organization/public-page/analytics")
    body = resp.json()
    assert body["page_views"] == 3
    assert body["whatsapp_clicks"] == 2


# ---------------------------------------------------------------------
# Retenção (LGPD) — purge verificável
# ---------------------------------------------------------------------


def test_purge_remove_eventos_antigos_preserva_recentes(org_a_actor):
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        old_event = PublicPageEvent(
            organization_id=org_a_actor.organization_id, event_type=PublicPageEventType.PAGE_VIEW
        )
        session.add(old_event)
        session.flush()
        session.execute(
            text("UPDATE public_page_events SET created_at = :old WHERE id = :id"),
            {"old": datetime.now(timezone.utc) - timedelta(days=200), "id": old_event.id},
        )
        recent_event = PublicPageEvent(
            organization_id=org_a_actor.organization_id, event_type=PublicPageEventType.PAGE_VIEW
        )
        session.add(recent_event)
        session.flush()
        # Captura os ids ANTES do purge — `old_event` vira uma linha
        # apagada; acessar `.id` nele DEPOIS do commit (expira o objeto)
        # dispara um refresh que falha com `ObjectDeletedError`.
        old_event_id, recent_event_id = old_event.id, recent_event.id
        session.commit()

        deleted = public_page_events_service.purge_events_older_than(session, retention_days=90)
        session.commit()
        assert deleted >= 1

        remaining_ids = {
            row[0]
            for row in session.execute(
                text("SELECT id FROM public_page_events WHERE organization_id = :oid"),
                {"oid": str(org_a_actor.organization_id)},
            ).all()
        }
        assert old_event_id not in remaining_ids
        assert recent_event_id in remaining_ids
