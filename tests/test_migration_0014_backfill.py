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
    """Monta a cadeia mínima de domínio pra satisfazer as FKs. `Client`,
    `Order`/`OrderItem`, `Payment` e (a partir da 0019, Etapa A)
    `OrganizationMembership` ganharam colunas novas em rodadas
    posteriores à 0013 — inserir/consultar essas tabelas via ORM ou via
    services que as tocam (ex.: `appointments.create_appointment`, que
    faz `client_repo.get`) falharia com "column does not exist" nesta
    revisão congelada do schema. Por isso TODA a cadeia (org, role,
    user, membership, branch, client, professional, service, agendamento
    + item, comanda + item, pagamento) é montada via SQL cru, restrita
    às colunas que já existiam na 0013 — só os models que realmente não
    mudaram desde então (`Organization`, `Branch`, `Professional`,
    `Service`, `ProfessionalService`, `WorkingHours`, `Role`) continuam
    via ORM. `User` também continua via ORM (não ganhou coluna nova).
    Devolve os ids relevantes pra validação depois do upgrade."""
    # Import tardio: só depois que `NEXASALON_DATABASE_URL` do processo
    # de teste principal já foi fixada pelo `conftest.py` (não usamos
    # esse valor aqui — construímos nossa própria engine/session — mas
    # os models em si não têm estado de engine, então importar aqui ou
    # no topo do arquivo dá no mesmo; mantido aqui só por clareza de que
    # este helper é o único lugar que efetivamente usa os models).
    from nexasalon_api.models.identity import User
    from nexasalon_api.models.organization import Branch, Organization
    from nexasalon_api.models.professional import Professional, WorkingHours
    from nexasalon_api.models.rbac import Role
    from nexasalon_api.models.service import ProfessionalService, Service

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

        # SQL cru, de propósito: `OrganizationMembership` ganhou
        # `agenda_view_scope`/`agenda_edit_scope` na 0019 (Etapa A) — não
        # existem ainda nesta revisão do schema (0013), então inserir via
        # ORM (que sempre inclui todas as colunas mapeadas, inclusive as
        # com server_default, no RETURNING) falharia com "column
        # organization_memberships.agenda_view_scope does not exist".
        # Mesma lógica já documentada acima para `clients`/`orders`/
        # `order_items`/`payments`.
        session.execute(
            text(
                "INSERT INTO organization_memberships (id, user_id, organization_id, role_id, status, "
                "created_at, updated_at) "
                "VALUES (:id, :user_id, :org, :role, 'active', now(), now())"
            ),
            {"id": uuid.uuid4(), "user_id": user.id, "org": org.id, "role": owner_role.id},
        )

        branch = Branch(organization_id=org.id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
        session.add(branch)
        session.flush()
        # SQL cru, de propósito: `Client` ganhou colunas novas na 0015
        # (cpf/cep/state/...) que não existem ainda nesta revisão do
        # schema (0013) — inserir via ORM tentaria incluí-las e falharia
        # com "column does not exist". Mesma lógica do `payments` abaixo.
        client_id = uuid.uuid4()
        session.execute(
            text("INSERT INTO clients (id, organization_id, name, created_at, updated_at) "
                 "VALUES (:id, :org, :name, now(), now())"),
            {"id": client_id, "org": org.id, "name": "Cliente Legado"},
        )
        session.flush()
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

        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org.id)})

        # Agendamento + item via SQL cru (mesma razão do `clients`
        # acima: evita qualquer caminho de código — ORM ou service —
        # que leia/escreva `Client` por baixo).
        start_at = datetime(2026, 8, 13, 9, 0, tzinfo=timezone(timedelta(hours=-3)))
        end_at = start_at + timedelta(minutes=60)
        appt_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO appointments (id, organization_id, branch_id, client_id, status, source, "
                "created_at, updated_at) "
                "VALUES (:id, :org, :branch, :client, 'finished', 'internal', now(), now())"
            ),
            {"id": appt_id, "org": org.id, "branch": branch.id, "client": client_id},
        )
        appt_item_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO appointment_items (id, organization_id, appointment_id, service_id, "
                "professional_id, start_at, end_at, duration_minutes, price, created_at, updated_at) "
                "VALUES (:id, :org, :appt, :service, :prof, :start_at, :end_at, :duration, :price, now(), now())"
            ),
            {
                "id": appt_item_id, "org": org.id, "appt": appt_id, "service": service.id, "prof": professional.id,
                "start_at": start_at, "end_at": end_at, "duration": 60, "price": Decimal("100.00"),
            },
        )
        session.flush()

        # Comanda montada via SQL cru, de propósito: `Order` ganhou
        # `order_number` e `OrderItem` ganhou `service_name`/
        # `professional_name` na 0015 — nenhuma dessas colunas existe
        # ainda nesta revisão do schema (0013), então construir via ORM
        # (que sempre inclui todas as colunas mapeadas no INSERT)
        # falharia com "column does not exist", igual ao caso do
        # `clients`/`payments` acima. Também evita o eager-load de
        # `Order.payments` (que já tocaria em `cash_register_id`).
        order_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO orders (id, organization_id, appointment_id, branch_id, client_id, status, "
                "created_by, created_at, updated_at) "
                "VALUES (:id, :org, :appt, :branch, :client, 'open', :uid, now(), now())"
            ),
            {"id": order_id, "org": org.id, "appt": appt_id, "branch": branch.id, "client": client_id, "uid": user.id},
        )
        total = Decimal("100.00")
        session.execute(
            text(
                "INSERT INTO order_items (id, organization_id, order_id, appointment_item_id, service_id, "
                "professional_id, duration_minutes, price, created_at, updated_at) "
                "VALUES (:id, :org, :order, :appt_item, :service, :prof, :duration, :price, now(), now())"
            ),
            {
                "id": uuid.uuid4(), "org": org.id, "order": order_id, "appt_item": appt_item_id,
                "service": service.id, "prof": professional.id, "duration": 60, "price": total,
            },
        )
        session.execute(
            text("UPDATE orders SET status = 'closed', closed_at = now(), closed_by = :uid WHERE id = :id"),
            {"uid": user.id, "id": order_id},
        )
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
            {"id": payment_id, "org": org.id, "order": order_id, "amount": total, "uid": user.id},
        )
        session.commit()

        ids = {
            "org": org.id, "user": user.id, "order": order_id, "payment": payment_id, "amount": total,
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
