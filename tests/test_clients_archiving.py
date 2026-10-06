"""Testes de "Excluir cliente" = arquivamento seguro (`Client.is_active`,
`services/clients.py::set_client_active`). Cobre: lista padrão só com
ativos; arquivado consultável; restauração com o MESMO `Client.id`;
bloqueio por agendamento FUTURO que ocupa agenda (mesma
`OCCUPYING_STATUSES` do arquivamento de profissional) e liberação para
passado/cancelado/finalizado/no_show; arquivado não recebe novo
agendamento (interno e Agendamento Online); histórico (comandas,
pagamentos, Extrato, histórico do cliente) permanece; AuditLog;
RBAC (`clients.manage`); isolamento multi-tenant; idempotência; nenhuma
exclusão física."""
import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentSource,
    AppointmentStatus,
    AuditAction,
    OrderStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import Service
from nexasalon_api.repositories import (
    audit_log_repo,
    order_item_repo,
    order_repo,
    payment_repo,
)
from nexasalon_api.services import appointments as appointments_service
from nexasalon_api.services import cash_register, extract
from nexasalon_api.services import clients as clients_service

_FUTURE = (datetime.now(timezone.utc) + timedelta(days=60)).replace(microsecond=0)
_PAST = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0)


# ---------------------------------------------------------------------
# Helpers HTTP (mesmo padrão de test_professional_archiving.py)
# ---------------------------------------------------------------------


def _setup_agenda(c, *, client_name="Cliente Arquivável"):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "John", "branch_id": branch["id"]}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    c.put(f"/api/v1/professionals/{professional['id']}/services", json={"items": [{"service_id": service["id"]}]})
    c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    client = c.post("/api/v1/clients", json={"name": client_name}).json()
    return branch, professional, service, client


def _create_future_appointment(c, branch, professional, service, client, start_at=_FUTURE):
    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": start_at.isoformat()}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _insert_appointment(org_id, branch_id, client_id, professional_id, service_id, *, start_at, status):
    """Inserção direta (agendamento PASSADO ou com status não-ocupante —
    casos que a API de criação não produz com a mesma facilidade)."""
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        end_at = start_at + timedelta(hours=1)
        appt = Appointment(
            organization_id=org_id, branch_id=branch_id, client_id=client_id, status=status,
            source=AppointmentSource.INTERNAL, starts_at=start_at, ends_at=end_at,
        )
        session.add(appt)
        session.flush()
        session.add(
            AppointmentItem(
                organization_id=org_id, appointment_id=appt.id, service_id=service_id,
                professional_id=professional_id, start_at=start_at, end_at=end_at,
                duration_minutes=60, price=Decimal("100.00"), status=None,
            )
        )
        appt_id = appt.id  # capturado antes do commit: o refresh pós-commit pode cair em outra conexão do pool (RLS)
        session.commit()
        return appt_id


def _client_row_count(org_id) -> int:
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        return int(session.scalar(select(func.count(Client.id)).where(Client.organization_id == org_id)) or 0)


def _audit_logs(org_id, client_id):
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        return audit_log_repo.list_for_entity(session, org_id, "client", client_id)


# ---------------------------------------------------------------------
# Lista padrão x Arquivados / Restauração
# ---------------------------------------------------------------------


def test_cliente_ativo_aparece_na_lista_padrao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Ativa"}).json()["id"]

    assert client_id in [cl["id"] for cl in c.get("/api/v1/clients").json()]


def test_arquivado_some_da_lista_padrao_e_aparece_em_arquivados(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Arquivável"}).json()["id"]

    resp = c.patch(f"/api/v1/clients/{client_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    assert client_id not in [cl["id"] for cl in c.get("/api/v1/clients").json()]
    assert client_id in [cl["id"] for cl in c.get("/api/v1/clients", params={"include_inactive": True}).json()]


def test_restaurar_devolve_a_lista_ativa_com_o_mesmo_id(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Restaurável"}).json()["id"]
    c.patch(f"/api/v1/clients/{client_id}/deactivate")

    resp = c.patch(f"/api/v1/clients/{client_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["id"] == client_id  # mesmo Client.id — nunca cria duplicado
    assert resp.json()["is_active"] is True
    assert client_id in [cl["id"] for cl in c.get("/api/v1/clients").json()]


def test_arquivar_e_restaurar_repetidos_sao_idempotentes_e_nao_duplicam_auditoria(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Repetível"}).json()["id"]

    assert c.patch(f"/api/v1/clients/{client_id}/deactivate").status_code == 200
    assert c.patch(f"/api/v1/clients/{client_id}/deactivate").status_code == 200  # repetir não quebra
    assert c.patch(f"/api/v1/clients/{client_id}/activate").status_code == 200
    assert c.patch(f"/api/v1/clients/{client_id}/activate").status_code == 200

    assert c.get(f"/api/v1/clients/{client_id}").json()["is_active"] is True
    # Uma transição de cada tipo (desativar e reativar) — repetições não geram entradas extras.
    assert len(_audit_logs(org_a_actor.organization_id, uuid.UUID(client_id))) == 2


# ---------------------------------------------------------------------
# Agendamentos futuros x passados x status canônicos
# ---------------------------------------------------------------------


def test_agendamento_futuro_ativo_bloqueia_arquivamento_com_422_e_quantidade(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    _create_future_appointment(c, branch, professional, service, client)

    resp = c.patch(f"/api/v1/clients/{client['id']}/deactivate")

    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["details"]["future_appointments_count"] == 1
    assert body["message"] == (
        "Este cliente possui 1 agendamento futuro. Cancele ou conclua esses agendamentos antes de excluir o cliente."
    )
    # Nada foi alterado: continua ativo e o agendamento segue intacto.
    assert c.get(f"/api/v1/clients/{client['id']}").json()["is_active"] is True


def test_agendamento_passado_nao_bloqueia_arquivamento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    _insert_appointment(
        org_a_actor.organization_id, uuid.UUID(branch["id"]), uuid.UUID(client["id"]),
        uuid.UUID(professional["id"]), uuid.UUID(service["id"]), start_at=_PAST, status=AppointmentStatus.SCHEDULED,
    )

    resp = c.patch(f"/api/v1/clients/{client['id']}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW, AppointmentStatus.FINISHED],
)
def test_agendamento_futuro_cancelado_no_show_ou_finalizado_nao_bloqueia(client_as, org_a_actor, status):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    _insert_appointment(
        org_a_actor.organization_id, uuid.UUID(branch["id"]), uuid.UUID(client["id"]),
        uuid.UUID(professional["id"]), uuid.UUID(service["id"]), start_at=_FUTURE, status=status,
    )

    resp = c.patch(f"/api/v1/clients/{client['id']}/deactivate")
    assert resp.status_code == 200, resp.text


def test_arquivado_nao_pode_receber_novo_agendamento_e_restaurado_volta_a_poder(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    c.patch(f"/api/v1/clients/{client['id']}/deactivate")

    blocked = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _FUTURE.isoformat()}],
        },
    )
    assert blocked.status_code == 422
    assert "arquivado" in blocked.json()["error"]["message"]

    c.patch(f"/api/v1/clients/{client['id']}/activate")
    _create_future_appointment(c, branch, professional, service, client)  # volta a agendar normalmente


def test_agendamento_online_bloqueia_cliente_arquivado(org_a_actor):
    """Fluxo público (conta de cliente vinculada a um Client arquivado):
    a reserva é recusada antes de qualquer validação de agenda — nunca
    cria um cadastro novo (identidade continua sendo a mesma)."""
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)})
        organization = session.get(Organization, org_a_actor.organization_id)
        archived = Client(organization_id=org_a_actor.organization_id, name="Online", is_active=False)
        session.add(archived)
        session.flush()
        with pytest.raises(ValidationDomainError, match="agendamento online"):
            appointments_service.create_public_appointment_for_customer(
                session, organization, branch_id=uuid.uuid4(), professional_id=None,
                service_id=uuid.uuid4(), start_at=_FUTURE, client_id=archived.id,
            )


# ---------------------------------------------------------------------
# RBAC e isolamento multi-tenant
# ---------------------------------------------------------------------


def test_ator_sem_clients_manage_recebe_403_ao_arquivar_e_restaurar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Protegida"}).json()["id"]

    restricted = dataclasses.replace(org_a_actor, permissions=org_a_actor.permissions - {"clients.manage"})
    r = client_as(restricted)
    assert r.patch(f"/api/v1/clients/{client_id}/deactivate").status_code == 403
    c.patch(f"/api/v1/clients/{client_id}/deactivate")
    assert client_as(restricted).patch(f"/api/v1/clients/{client_id}/activate").status_code == 403


def test_isolamento_multitenant_outra_org_nao_arquiva_nem_restaura(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    client_id = c_a.post("/api/v1/clients", json={"name": "Da Org A"}).json()["id"]

    c_b = client_as(org_b_actor)
    assert c_b.patch(f"/api/v1/clients/{client_id}/deactivate").status_code == 404
    c_a = client_as(org_a_actor)  # client_as troca o override global — reatribui ao alternar
    assert c_a.get(f"/api/v1/clients/{client_id}").json()["is_active"] is True

    c_a.patch(f"/api/v1/clients/{client_id}/deactivate")
    c_b = client_as(org_b_actor)
    assert c_b.patch(f"/api/v1/clients/{client_id}/activate").status_code == 404
    c_a = client_as(org_a_actor)
    assert c_a.get(f"/api/v1/clients/{client_id}").json()["is_active"] is False


def test_arquivar_nao_apaga_linha_nem_libera_cpf(client_as, org_a_actor):
    """Nenhuma exclusão física: a linha continua existindo, e o CPF
    segue reservado pra organização (política já existente — ver
    `test_clients.py::test_cpf_duplicado_bloqueia_mesmo_se_cliente_original_estiver_desativado`)."""
    c = client_as(org_a_actor)
    before = _client_row_count(org_a_actor.organization_id)
    client_id = c.post("/api/v1/clients", json={"name": "Com CPF", "cpf": "111.444.777-35"}).json()["id"]
    c.patch(f"/api/v1/clients/{client_id}/deactivate")

    assert _client_row_count(org_a_actor.organization_id) == before + 1
    assert c.post("/api/v1/clients", json={"name": "Outra", "cpf": "111.444.777-35"}).status_code == 409


def test_auditoria_registra_estado_anterior_novo_e_usuario(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Auditada"}).json()["id"]
    c.patch(f"/api/v1/clients/{client_id}/deactivate")
    c.patch(f"/api/v1/clients/{client_id}/activate")

    logs = _audit_logs(org_a_actor.organization_id, uuid.UUID(client_id))
    assert [log.action for log in logs] == [AuditAction.UPDATE, AuditAction.UPDATE]
    assert logs[0].old_values == {"is_active": True}
    assert logs[0].new_values == {"change_type": "set_active", "is_active": False}
    assert logs[1].old_values == {"is_active": False}
    assert logs[1].new_values == {"change_type": "set_active", "is_active": True}
    assert all(log.user_id == org_a_actor.user_id for log in logs)


# ---------------------------------------------------------------------
# Histórico permanece (comandas, pagamentos, Extrato, histórico do cliente)
# ---------------------------------------------------------------------


def _actor(session, org_id) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Rafael")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="OWNER", permissions=frozenset({"finance.view", "finance.manage", "clients.manage", "clients.view"}),
    )


def test_arquivar_preserva_comanda_pagamento_extrato_e_nome_historico(org_a_actor):
    org_id = org_a_actor.organization_id
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        actor = _actor(session, org_id)
        branch = Branch(organization_id=org_id, name="Unidade", slug=f"unid-{uuid.uuid4().hex[:6]}")
        session.add(branch)
        session.flush()
        client = Client(organization_id=org_id, name="Cliente Histórica")
        session.add(client)
        professional = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional")
        session.add(professional)
        service = Service(organization_id=org_id, name="Serviço", default_duration_minutes=60, default_price=Decimal("100"))
        session.add(service)
        session.flush()

        register = cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
        start = _PAST
        appt = Appointment(
            organization_id=org_id, branch_id=branch.id, client_id=client.id, status=AppointmentStatus.FINISHED,
            source=AppointmentSource.INTERNAL, starts_at=start, ends_at=start + timedelta(hours=1),
        )
        session.add(appt)
        session.flush()
        order = order_repo.create(
            session, org_id, appointment_id=appt.id, branch_id=branch.id, client_id=client.id, created_by=actor.user_id,
        )
        order_item_repo.create(
            session, org_id, order_id=order.id, appointment_item_id=None, service_id=service.id,
            professional_id=professional.id, duration_minutes=60, price=Decimal("100.00"),
            service_name="Serviço", professional_name="Profissional",
        )
        payment_repo.create(
            session, org_id, order_id=order.id, cash_register_id=register.id, method=PaymentMethod.PIX,
            card_brand=None, installments=None, amount=Decimal("100.00"),
            created_by=actor.user_id, created_by_name="Rafael",
        )
        order.status = OrderStatus.CLOSED
        order.closed_at = start
        session.flush()

        revenue_before = extract.get_extract(session, actor, date_from=None, date_to=None).revenue_total
        assert revenue_before == Decimal("100.00")

        clients_service.set_client_active(session, org_id, client.id, False, user_id=actor.user_id)
        session.flush()

        summary = extract.get_extract(session, actor, date_from=None, date_to=None)
        assert summary.revenue_total == revenue_before  # faturamento histórico não muda
        assert len(summary.sales) == 1
        assert summary.client_names[client.id] == "Cliente Histórica"  # nome histórico preservado
        assert len(summary.sales[0].payments) == 1  # pagamento permanece
        assert summary.sales[0].status == OrderStatus.CLOSED

        history = clients_service.get_client_history(session, org_id, client.id)
        assert len(history.orders) == 1  # comanda antiga permanece no histórico do cliente
        assert clients_service.get_client(session, org_id, client.id).is_active is False  # leitura histórica segue disponível
        session.rollback()
