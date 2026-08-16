"""Migração de dado legado — 0013 -> 0014 (Caixa Diário).

Cobre exatamente o cenário que motivou reescrever a 0014: staging já
tinha `payments` (criada pela 0013) aplicada com um pagamento real no
momento em que a primeira versão desta migration (que criava
`cash_register_id` como `NOT NULL` direto, sem backfill) foi revisada —
isso teria derrubado o `alembic upgrade head` ali (Postgres recusa
`ADD COLUMN ... NOT NULL` sem default numa tabela com linha existente).

Este teste recria esse cenário do zero, num Postgres descartável
PRÓPRIO (não o de `conftest.py`, que já sobe migrado até `head` antes
de qualquer teste rodar — não dá pra testar "banco parado em 0013"
reaproveitando aquele fixture):

  1. Sobe migrations só até `0013` (estado real do staging antes desta
     correção).
  2. Monta a cadeia mínima de domínio (organização -> role -> usuário ->
     membership -> unidade -> cliente -> profissional -> serviço ->
     agendamento -> comanda) e insere um "pagamento antigo" via SQL cru,
     só com as colunas que existiam até a 0013 (sem `cash_register_id`/
     `created_by_name` — não existem ainda nesse ponto).
  3. Roda `alembic upgrade head` (aplica a 0014) e confirma que:
     - o pagamento sobrevive, com o mesmo valor/método;
     - `cash_register_id` foi preenchido, apontando pra um caixa
       histórico sintético da mesma organização, com o usuário do
       pagamento como responsável;
     - `created_by_name` foi preenchido via backfill;
     - a coluna `payments.cash_register_id` realmente virou `NOT NULL`
       (não ficou nullable "por segurança");
     - não sobrou nenhum `payments.cash_register_id IS NULL`.
"""
import os
import shutil
import subprocess
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pgserver
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
_PGDATA = str(REPO_ROOT / ".pgdata_migration_test")


@pytest.fixture(scope="module")
def legacy_db():
    shutil.rmtree(_PGDATA, ignore_errors=True)
    srv = pgserver.get_server(_PGDATA)
    try:
        srv.psql("CREATE DATABASE nexasalon_migration_test;")
    except Exception:
        pass

    admin_url = f"postgresql+psycopg://postgres:@/nexasalon_migration_test?host={_PGDATA}"
    env = os.environ.copy()
    env["NEXASALON_DATABASE_URL"] = admin_url
    env.pop("NEXASALON_MIGRATIONS_DATABASE_URL", None)

    # Estado real do staging antes desta correção: só até a 0013.
    result = subprocess.run(
        ["alembic", "upgrade", "0013"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar até 0013:\n{result.stdout}\n{result.stderr}"

    yield admin_url, env

    srv.cleanup()
    shutil.rmtree(_PGDATA, ignore_errors=True)


def _insert_legacy_payment(admin_url: str) -> dict:
    """Monta a cadeia mínima de domínio via ORM (nenhum desses models
    mudou na 0014 — só `Payment` mudou) e insere o `payment` em si via
    SQL cru, restrito às colunas que já existiam na 0013. Devolve os ids
    relevantes pra validação depois do upgrade."""
    # Import tardio: só depois que `NEXASALON_DATABASE_URL` do processo
    # de teste principal já foi fixada pelo `conftest.py` (não usamos
    # esse valor aqui — construímos nossa própria engine/session — mas
    # os models em si não têm estado de engine, então importar aqui ou
    # no topo do arquivo dá no mesmo; mantido aqui só por clareza de que
    # este helper é o único lugar que efetivamente usa os models).
    from nexasalon_api.core.actor import ActorContext
    from nexasalon_api.models.client import Client
    from nexasalon_api.models.enums import AppointmentStatus, MembershipStatus, OrderStatus
    from nexasalon_api.models.identity import OrganizationMembership, User
    from nexasalon_api.models.order import Order, OrderItem
    from nexasalon_api.models.organization import Branch, Organization
    from nexasalon_api.models.professional import Professional, WorkingHours
    from nexasalon_api.models.rbac import Role
    from nexasalon_api.models.service import ProfessionalService, Service
    from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
    from nexasalon_api.services import appointments

    engine = create_engine(admin_url)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        org = Organization(name="Org Legado", slug=f"org-legado-{uuid.uuid4().hex[:8]}")
        session.add(org)
        session.flush()

        owner_role = session.query(Role).filter_by(name="OWNER", organization_id=None).one()

        user = User(email=f"legado-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Responsável Legado")
        session.add(user)
        session.flush()

        session.add(
            OrganizationMembership(
                user_id=user.id, organization_id=org.id, role_id=owner_role.id, status=MembershipStatus.ACTIVE
            )
        )

        branch = Branch(organization_id=org.id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
        session.add(branch)
        client = Client(organization_id=org.id, name="Cliente Legado")
        session.add(client)
        professional = Professional(organization_id=org.id, branch_id=branch.id, name="Profissional Legado")
        session.add(professional)
        service = Service(
            organization_id=org.id, name="Corte", default_duration_minutes=60, default_price=Decimal("100.00")
        )
        session.add(service)
        session.flush()
        session.add(ProfessionalService(professional_id=professional.id, service_id=service.id))
        # 2026-08-13 é quinta (weekday=4 na convenção do projeto:
        # 0=domingo..6=sábado, ver `test_orders.py::_THURSDAY`) — mesma
        # data usada nos outros testes de comanda, só pra evitar
        # recalcular a convenção de novo aqui.
        session.add(
            WorkingHours(
                organization_id=org.id, professional_id=professional.id, weekday=4,
                start_time=time(9, 0), end_time=time(20, 0),
            )
        )
        session.flush()

        actor = ActorContext(
            organization_id=org.id, user_id=user.id, membership_id=uuid.uuid4(), role_id=owner_role.id,
            role_name="OWNER", permissions=frozenset({"agenda.create", "agenda.edit", "orders.manage"}),
        )
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org.id)})

        appt_data = AppointmentCreate(
            branch_id=branch.id,
            client_id=client.id,
            items=[
                AppointmentItemCreate(
                    professional_id=professional.id, service_id=service.id,
                    start_at=datetime(2026, 8, 13, 9, 0, tzinfo=timezone(timedelta(hours=-3))),
                )
            ],
        )
        appt = appointments.create_appointment(session, actor, appt_data)
        appt.status = AppointmentStatus.FINISHED
        session.flush()

        # Comanda montada direto via ORM (não `services/orders.py::create_order`):
        # aquele service devolve o Order recarregado via `order_repo.get`,
        # que faz eager-load de `Order.payments` — e isso dispararia um
        # SELECT em `payments` incluindo `cash_register_id`, coluna que
        # ainda não existe nesta revisão do schema (0013). Construir o
        # Order/OrderItem manualmente evita tocar nesse relationship.
        order = Order(
            organization_id=org.id, appointment_id=appt.id, branch_id=branch.id, client_id=client.id,
            status=OrderStatus.OPEN, created_by=user.id,
        )
        session.add(order)
        session.flush()
        total = Decimal("0")
        for item in appt.items:
            session.add(
                OrderItem(
                    organization_id=org.id, order_id=order.id, appointment_item_id=item.id,
                    service_id=item.service_id, professional_id=item.professional_id,
                    duration_minutes=item.duration_minutes, price=item.price,
                )
            )
            total += item.price
        order.status = OrderStatus.CLOSED
        order.closed_at = datetime.now(timezone.utc)
        order.closed_by = user.id
        session.flush()

        payment_id = uuid.uuid4()
        # SQL cru, de propósito: o model `Payment` atual já tem
        # `cash_register_id` mapeado (não existe ainda nesta revisão do
        # schema) — inserir via ORM falharia com "column does not
        # exist". Isto reproduz fielmente como a linha existia em
        # produção antes da 0014.
        session.execute(
            text(
                "INSERT INTO payments (id, organization_id, order_id, method, amount, created_by, created_at, updated_at) "
                "VALUES (:id, :org, :order, 'pix', :amount, :uid, now(), now())"
            ),
            {"id": payment_id, "org": org.id, "order": order.id, "amount": total, "uid": user.id},
        )
        session.commit()

        ids = {
            "org": org.id, "user": user.id, "order": order.id, "payment": payment_id, "amount": total,
        }

    engine.dispose()
    return ids


def test_migration_0014_preserva_pagamento_legado_e_faz_backfill(legacy_db):
    admin_url, env = legacy_db
    ids = _insert_legacy_payment(admin_url)

    result = subprocess.run(
        ["alembic", "upgrade", "head"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar pra head (0014):\n{result.stdout}\n{result.stderr}"

    engine = create_engine(admin_url)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT cash_register_id, created_by_name, amount, method FROM payments WHERE id = :id"),
            {"id": ids["payment"]},
        ).one()
        assert row.cash_register_id is not None, "payment legado ficou sem cash_register_id após o backfill"
        assert row.created_by_name == "Responsável Legado"
        assert row.amount == ids["amount"]
        assert row.method == "pix"

        register = conn.execute(
            text(
                "SELECT organization_id, opened_by, opened_by_name, status, initial_amount, closed_at "
                "FROM cash_registers WHERE id = :id"
            ),
            {"id": row.cash_register_id},
        ).one()
        assert register.organization_id == ids["org"]
        assert register.opened_by == ids["user"]
        assert register.opened_by_name == "Responsável Legado"
        assert register.status == "closed"
        assert register.closed_at is not None
        assert register.initial_amount == Decimal("0.00")

        is_nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'payments' AND column_name = 'cash_register_id'"
            )
        ).scalar_one()
        assert is_nullable == "NO", "cash_register_id deveria ter virado NOT NULL depois do backfill"

        remaining_null = conn.execute(
            text("SELECT COUNT(*) FROM payments WHERE cash_register_id IS NULL")
        ).scalar_one()
        assert remaining_null == 0
    engine.dispose()


def test_migration_0014_downgrade_reverte_sem_quebrar(legacy_db):
    """Independente do teste anterior de propósito (não assume qual já
    rodou nem em que ordem — só garante que está em `head` antes de
    começar). Não testado com dado legado especificamente aqui: downgrade
    descarta as colunas por definição (não há como preservar
    `cash_register_id` voltando pro schema da 0013, que nunca teve essa
    coluna) — o que importa validar é só que o downgrade em si roda sem
    erro e que dá pra reaplicar a 0014 depois, mesmo se a rodada anterior
    já tiver deixado pagamento(s)/caixa(s) no banco."""
    _, env = legacy_db

    result = subprocess.run(
        ["alembic", "upgrade", "head"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao garantir head antes do downgrade:\n{result.stdout}\n{result.stderr}"

    result = subprocess.run(
        ["alembic", "downgrade", "0013"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao reverter a 0014:\n{result.stdout}\n{result.stderr}"

    result = subprocess.run(
        ["alembic", "upgrade", "head"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao reaplicar a 0014 depois do downgrade:\n{result.stdout}\n{result.stderr}"
