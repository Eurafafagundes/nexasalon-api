"""performance etapa 2 — índices (só leitura, sem mudança de dado/coluna)

Migration EXCLUSIVAMENTE de índices — nenhuma coluna, FK, constraint ou
tabela é criada/alterada aqui. Cada índice abaixo foi verificado contra
o código real (`repositories/*.py`) que executa a query beneficiada, e
contra TODAS as migrations existentes até `0045` pra confirmar que não
há sobreposição com um índice já criado (nenhum tem os mesmos
`table+columns`, nem é subconjunto redundante de um composto já
existente).

Nenhuma comparação com `list_for_org` de `stock_movements` foi incluída
aqui, de propósito: essa tabela JÁ TEM `ix_stock_movements_organization_id`
(migration 0020) — um índice novo ali seria uma melhoria incremental
(índice composto evitaria um sort explícito de `created_at`), não a
lacuna total das 9 tabelas abaixo (nenhuma tinha QUALQUER índice
cobrindo o filtro real que sua query faz). Ficou de fora desta rodada
por não ter a mesma urgência/evidência que as outras — decisão
documentada aqui, não um esquecimento.

1. `ix_orders_org_status_created_at` em
   `orders(organization_id, status, created_at DESC)` — Comandas
   Abertas/Finalizadas (`order_repo.py::list_for_org`, Financeiro >
   Comandas), filtra org+status opcional+`created_at` range,
   `ORDER BY created_at DESC`. Índices existentes em `orders`
   (`ix_orders_client_id`/`ix_orders_status`, migration 0013;
   `ix_orders_org_branch_closed_at`/`ix_orders_org_client_closed_at`,
   migration 0018) usam `closed_at`, nunca `created_at` — uma comanda
   ABERTA não tem `closed_at` preenchido, então a aba "Abertas" nunca
   se beneficiava de nenhum índice composto existente.

2. `ix_orders_org_status_closed_at` em
   `orders(organization_id, status, closed_at)` — todo o Dashboard/BI
   (`services/dashboard.py::_fetch_period_data`) e o Extrato
   (`services/extract.py::get_extract` via `order_repo.list_for_org`)
   filtram exatamente `organization_id + status=CLOSED + closed_at
   BETWEEN`, SEM filtro de unidade quando o usuário escolhe "Todas as
   unidades" (o caso mais comum). `ix_orders_org_branch_closed_at`
   (migration 0018) tem `branch_id` como 2ª coluna — sem esse filtro, o
   planner não consegue usar esse índice de forma seletiva pra
   `closed_at`.

3. `ix_appointment_items_org_range` em
   `appointment_items(organization_id, start_at, end_at)` — carga
   principal da Agenda (`appointment_item_repo.py::list_agenda`) quando
   NENHUM profissional específico é filtrado (`agenda.view_all`, o caso
   comum de recepção/gerência). Único índice de range hoje é
   `ix_appointment_items_professional_range` (migration 0002,
   `professional_id` como coluna líder) — não ajuda quando não há
   filtro de profissional.

4. `ix_appointment_items_appointment_id` em
   `appointment_items(appointment_id)` — FK sem NENHUM índice (Postgres
   nunca indexa FK automaticamente). Usado por
   `appointment_item_repo.py::delete_for_appointment`, chamado em toda
   edição de agendamento.

5. `ix_audit_logs_org_entity_created_at` em
   `audit_logs(organization_id, entity_type, entity_id, created_at)` —
   `audit_log_repo.py::list_for_entity` (histórico de auditoria de
   qualquer entidade). Tabela sem NENHUM índice além da PK até agora.

6. `ix_cash_movements_org_created_at` em
   `cash_movements(organization_id, created_at DESC)` —
   `cash_movement_repo.py::list_for_org` (Extrato > Movimentações).
   Único índice existente é `ix_cash_movements_cash_register_id`
   (migration 0014) — `organization_id` não tinha índice nenhum nesta
   tabela.

7. `ix_order_items_pending_commission` (parcial) em
   `order_items(professional_id, commission_status) WHERE
   commission_settlement_id IS NULL` —
   `order_item_repo.py::lock_pending_commission_items` (candidatos a
   "Registrar pagamento" de comissão) filtra exatamente
   `professional_id + commission_status='calculated' +
   commission_settlement_id IS NULL`. Único índice existente é
   `ix_order_items_order_id` (migration 0013) — sem sobreposição.

8. `ix_org_memberships_org_status` em
   `organization_memberships(organization_id, status)` —
   `membership_repo.py::list_for_organization` (Configurações >
   Acessos). Único índice hoje é o
   `UniqueConstraint(user_id, organization_id)` (migration 0002), com
   `user_id` como coluna líder — não serve pra filtro só por
   organização.

9. `ix_commission_settlements_org_prof_paid_at` em
   `commission_settlements(organization_id, professional_id, paid_at
   DESC)` — `commission_settlement_repo.py::list_all` (histórico de
   pagamentos de comissão). Zero índices além da PK até agora.

10. `ix_commission_adjustments_org_prof_pending` (parcial) em
    `commission_adjustments(organization_id, professional_id) WHERE
    commission_settlement_id IS NULL` —
    `commission_adjustment_repo.py::list_pending_for_professional`
    (ajustes pendentes pra próxima liquidação). Zero índices além da PK
    até agora.

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-06
"""
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_orders_org_status_created_at", "orders",
        ["organization_id", "status", "created_at"], unique=False,
    )
    op.create_index(
        "ix_orders_org_status_closed_at", "orders",
        ["organization_id", "status", "closed_at"], unique=False,
    )
    op.create_index(
        "ix_appointment_items_org_range", "appointment_items",
        ["organization_id", "start_at", "end_at"], unique=False,
    )
    op.create_index(
        "ix_appointment_items_appointment_id", "appointment_items",
        ["appointment_id"], unique=False,
    )
    op.create_index(
        "ix_audit_logs_org_entity_created_at", "audit_logs",
        ["organization_id", "entity_type", "entity_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_cash_movements_org_created_at", "cash_movements",
        ["organization_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_order_items_pending_commission", "order_items",
        ["professional_id", "commission_status"], unique=False,
        postgresql_where="commission_settlement_id IS NULL",
    )
    op.create_index(
        "ix_org_memberships_org_status", "organization_memberships",
        ["organization_id", "status"], unique=False,
    )
    op.create_index(
        "ix_commission_settlements_org_prof_paid_at", "commission_settlements",
        ["organization_id", "professional_id", "paid_at"], unique=False,
    )
    op.create_index(
        "ix_commission_adjustments_org_prof_pending", "commission_adjustments",
        ["organization_id", "professional_id"], unique=False,
        postgresql_where="commission_settlement_id IS NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_commission_adjustments_org_prof_pending", table_name="commission_adjustments",
        postgresql_where="commission_settlement_id IS NULL",
    )
    op.drop_index("ix_commission_settlements_org_prof_paid_at", table_name="commission_settlements")
    op.drop_index("ix_org_memberships_org_status", table_name="organization_memberships")
    op.drop_index(
        "ix_order_items_pending_commission", table_name="order_items",
        postgresql_where="commission_settlement_id IS NULL",
    )
    op.drop_index("ix_cash_movements_org_created_at", table_name="cash_movements")
    op.drop_index("ix_audit_logs_org_entity_created_at", table_name="audit_logs")
    op.drop_index("ix_appointment_items_appointment_id", table_name="appointment_items")
    op.drop_index("ix_appointment_items_org_range", table_name="appointment_items")
    op.drop_index("ix_orders_org_status_closed_at", table_name="orders")
    op.drop_index("ix_orders_org_status_created_at", table_name="orders")
