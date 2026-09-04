"""clients.view_contact_data permission — privacidade de dados de contato

Etapa de melhorias (Agenda/permissões/privacidade de clientes). Problema
real de negócio: um funcionário com acesso à própria Agenda já usou o
telefone de uma cliente pra oferecer serviço por fora (fora do salão).
Hoje `clients.view` (ou o funcional `clients.lookup`, Etapa L/0030) libera
nome+telefone+CPF+e-mail+endereço juntos, sem distinção — não dá pra
deixar alguém trabalhar normalmente na Agenda/Comanda (ver cliente, criar
agendamento, cadastrar cliente novo quando sua permission operacional
permitir) sem também entregar o telefone completo.

`clients.view_contact_data` é uma permission NOVA, ortogonal a
`clients.view`/`clients.manage`/`clients.lookup`/`clients.create`
existentes — libera telefone/WhatsApp/CPF/e-mail/endereço completos
(mascarados por padrão em toda resposta que devolve um Client, ver
`core/client_privacy.py`). Nunca substitui nenhuma permission existente,
só soma: quem já tinha `clients.view`/`clients.manage` continua vendo
NOME normalmente, mas passa a ver contato mascarado a menos que também
tenha esta chave nova.

Concessão (mesmo raciocínio da 0030, "acesso ao módulo != uso operacional
completo do dado" — aqui invertido: acesso operacional != contato
completo):
  OWNER        -> tem (todas as permissions do catálogo, ver 0007)
  ADMIN        -> tem (mesmo padrão da 0030 — módulo completo já implica
                  poder ver o dado que administra)
  RECEPTIONIST -> tem (função operacional de recepção legitimamente
                  precisa ligar/confirmar com a cliente — já tinha
                  `clients.manage` de fábrica desde a 0007, então este
                  ajuste só destrava o que a 0007 sempre pretendeu)
  PROFESSIONAL -> NÃO tem (exatamente o perfil do incidente real que
                  motivou esta permission — continua vendo nome, agenda,
                  consegue cadastrar cliente novo, mas sem telefone/CPF/
                  e-mail/endereço completos por padrão)

Organizações podem conceder a um perfil customizado via ManageAccessDrawer
(override por membership) exatamente como qualquer outra permission.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-04
"""
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_KEY = "clients.view_contact_data"
_MODULE = "clients"
_DESCRIPTION = "Ver dados de contato dos clientes (telefone, WhatsApp, CPF, e-mail, endereço)"
_GRANTED_TO = ["OWNER", "ADMIN", "RECEPTIONIST"]


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s) "
        "ON CONFLICT (key) DO NOTHING",
        (_KEY, _MODULE, _DESCRIPTION),
    )
    for role_name in _GRANTED_TO:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM role_permissions rp "
            "  JOIN roles r ON r.id = rp.role_id "
            "  WHERE r.name = %s AND r.organization_id IS NULL AND rp.permission_key = %s"
            ")",
            (_KEY, role_name, role_name, _KEY),
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DELETE FROM role_permissions WHERE permission_key = %s", (_KEY,))
    conn.exec_driver_sql("DELETE FROM permissions WHERE key = %s", (_KEY,))
