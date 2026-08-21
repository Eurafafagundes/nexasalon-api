"""dashboard.view permission

Etapa G — item "completar permissões faltantes no Gerenciador de Acessos
para Agenda, Clientes, Comandas, Estoque, Caixa, Dashboard e
Configurações". Auditoria do catálogo atual (ver `rbac-labels.ts` no
frontend, que já tinha um `MODULE_LABELS["dashboard"] = "Dashboard"` órfão
— sinal de que esse módulo sempre foi planejado, só nunca ganhou
permission própria): a ÚNICA lacuna real encontrada foi o Dashboard, que
até aqui pedia `reports.view` emprestado (`api/v1/dashboard.py`, comentário
"nenhuma permission nova precisa nascer" — decisão revertida agora que o
Gerenciador de Acessos precisa expor um toggle específico "Dashboard",
independente de um futuro módulo de Relatórios).

Os outros 6 módulos citados (Agenda, Clientes, Comandas, Estoque, Caixa,
Configurações) já têm cobertura completa e adequada:
  - Agenda: 7 keys (0007 + 0011).
  - Clientes: clients.view/manage (0007).
  - Comandas: orders.view/manage/edit_price/cancel (0013 + 0025).
  - Estoque: inventory.view/view_cost/manage (0020).
  - Caixa: reaproveita finance.view/finance.manage (0007, decisão
    explícita da migration 0014) — junto com o Extrato financeiro, sob o
    mesmo guarda-chuva "Financeiro"; criar um módulo `cash_register.*`
    separado exigiria re-testar todo o fluxo financeiro (`test_cash_register.py`,
    `test_cash_registers_api.py`) por um ganho puramente cosmético de
    rótulo — fora do escopo desta etapa.
  - Configurações: settings.manage (0007); leitura é intencionalmente
    aberta a qualquer membro autenticado (ver `appointment_status_styles.py`
    docstring), não uma lacuna.

`reports.view` permanece no catálogo (não removida — pode servir um
futuro módulo de Relatórios distinto de Dashboard) mas deixa de gatear
`/dashboard/*`.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-21
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

PERMISSION_KEY = "dashboard.view"
PERMISSION_MODULE = "dashboard"
PERMISSION_DESCRIPTION = "Visualizar o Dashboard (indicadores e gráficos da organização)"
GRANTED_TO = ["OWNER", "ADMIN"]


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
        (PERMISSION_KEY, PERMISSION_MODULE, PERMISSION_DESCRIPTION),
    )
    for role_name in GRANTED_TO:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
            (PERMISSION_KEY, role_name),
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DELETE FROM role_permissions WHERE permission_key = %s", (PERMISSION_KEY,))
    conn.exec_driver_sql("DELETE FROM permissions WHERE key = %s", (PERMISSION_KEY,))
