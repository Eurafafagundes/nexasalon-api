"""dashboard analytics indexes

Item "Dashboard/BI" — nenhuma tabela nova, só dois índices compostos em
`orders` que o novo módulo analítico (`services/dashboard.py`) precisa
de verdade, justificados pelo padrão real de consulta:

1. `ix_orders_org_branch_closed_at` em
   `orders(organization_id, branch_id, closed_at)` — TODA métrica do
   Dashboard baseada em venda (faturamento, ticket médio, clientes
   atendidos, top serviços, desempenho por profissional, formas de
   pagamento) filtra exatamente esses três campos juntos: organização
   (RLS já filtra, mas o índice ainda precisa cobrir a query
   explícita), unidade (filtro opcional "Todas as unidades" ×
   "Unidade específica") e o intervalo de datas do período analisado
   ou comparativo. Hoje só existem `ix_orders_client_id` e
   `ix_orders_status` (migration 0013) — nenhum cobre filtro por data,
   que é o caso de uso central de um dashboard com período/comparação.

2. `ix_orders_org_client_closed_at` em
   `orders(organization_id, client_id, closed_at)` — usado pela
   "primeira visita real" de cada cliente (MIN(closed_at) por
   client_id), base de "Novos Clientes", "Novos × Recorrentes" e
   "Taxa de Retorno em 90 dias". Essa consulta precisa olhar o
   HISTÓRICO COMPLETO de comandas de um cliente (não só o período
   filtrado) para não classificar errado um cliente recorrente como
   novo — sem este índice ela dependeria de `ix_orders_client_id`
   (sem `closed_at`) e teria que reordenar em memória.

Nenhuma tabela de `appointments` precisa de índice novo: as métricas
baseadas em Appointment (Agendamentos, Taxa de Faltas, Distribuição por
Status, Heatmap) reaproveitam `ix_appointments_org_branch_starts`
(criado junto com a tabela), que já cobre exatamente
`(organization_id, branch_id, starts_at)`.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-19
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_orders_org_branch_closed_at", "orders", ["organization_id", "branch_id", "closed_at"], unique=False
    )
    op.create_index(
        "ix_orders_org_client_closed_at", "orders", ["organization_id", "client_id", "closed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_orders_org_client_closed_at", table_name="orders")
    op.drop_index("ix_orders_org_branch_closed_at", table_name="orders")
