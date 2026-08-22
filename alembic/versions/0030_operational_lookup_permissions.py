"""operational lookup permissions (clients.lookup, clients.create, services.lookup)

Etapa L, Bloco 1 — "acesso ao módulo != uso operacional do dado". Hoje só
existem `clients.view`/`clients.manage` e `services.view`/`services.manage`
(migration 0007): um funcionário sem `clients.view` fica cego para a lista
completa de Clientes (correto, Ficha 360° é dado sensível) mas também não
consegue pesquisar/selecionar um cliente já cadastrado dentro de um fluxo
operacional permitido (Agenda/Comanda) — problema conceitual real quando
uma organização configura um role customizado mais restritivo que os 4
roles de sistema padrão (que hoje já dão `clients.view`/`services.view`
"de brinde" a todo mundo, RECEPTIONIST e PROFESSIONAL inclusive — ver
0007). Sem estas chaves, a única forma de destravar a busca seria conceder
`clients.view` inteiro (abre Ficha 360°/lista completa) só para permitir
pesquisar um nome — exatamente o que o pedido pede para NÃO fazer.

Três chaves novas, deliberadamente ENXUTAS (não substituem `.view`/`.manage`,
só cobrem o mínimo operacional):
  - `clients.lookup`   -> pesquisar/selecionar cliente (nome/telefone) em
                          fluxos como Agenda/Comanda, sem abrir Ficha 360°
                          nem listar a base inteira.
  - `clients.create`   -> cadastrar cliente NOVO a partir desses mesmos
                          fluxos operacionais, sem acesso à administração
                          completa de Clientes.
  - `services.lookup`  -> pesquisar/selecionar serviço ativo (nome, duração,
                          preço) em Agenda/Comanda, sem acessar o catálogo
                          administrativo completo.

Endpoints correspondentes (`GET /clients/lookup`, `GET /services/lookup`)
aceitam a permissão granular OU a de view mais ampla (`require_any_permission`)
— ver `api/v1/clients.py`/`api/v1/services.py`. `POST /clients` passa a
aceitar `clients.manage` OU `clients.create` (mesma lógica).

Concessão: SÓ para OWNER/ADMIN (mesmo padrão conservador da migration
0026/`dashboard.view`) — DELIBERADAMENTE não concedida a RECEPTIONIST
nem PROFESSIONAL por padrão. Dois motivos:

  1. RECEPTIONIST/PROFESSIONAL já têm `clients.view`/`services.view`
     "de fábrica" (0007) — a permissão ampla já cobre tudo que a
     granular cobriria, então não há comportamento novo a destravar
     pra eles.
  2. PROFESSIONAL nunca teve NENHUMA permissão `.manage`/de escrita
     (invariante testado em `test_escrita_dos_recursos_2c_exige_permission_manage`),
     e o DENY de override em `clients.manage` pro RECEPTIONIST precisa
     continuar bloqueando toda criação de cliente
     (`test_override_deny_vence_a_permissao_concedida_pelo_role`) — dar
     `clients.create`/`clients.lookup` de brinde pros 4 roles de
     sistema quebraria os DOIS invariantes já testados, mudando
     silenciosamente a capacidade padrão de roles existentes sem
     pedido explícito para isso.

O objetivo real do Bloco 1 é permitir que uma organização crie/ajuste um
role CUSTOMIZADO mais restritivo (sem `clients.view`/`services.view`) e
ainda assim conceda só o mínimo operacional via
`ManageAccessDrawer > Permissões` — as chaves precisam EXISTIR no
catálogo (`GET /api/v1/permissions`) pra isso ser possível, não precisam
vir pré-concedidas aos 4 roles de sistema.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-22
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

PERMISSIONS = [
    ("clients.lookup", "clients", "Pesquisar/selecionar cliente em fluxos operacionais (Agenda/Comanda)"),
    ("clients.create", "clients", "Cadastrar cliente novo a partir de fluxos operacionais (Agenda/Comanda)"),
    ("services.lookup", "services", "Pesquisar/selecionar serviço ativo em fluxos operacionais (Agenda/Comanda)"),
]
GRANTED_TO = ["OWNER", "ADMIN"]


def upgrade() -> None:
    conn = op.get_bind()
    for key, module, description in PERMISSIONS:
        conn.exec_driver_sql(
            "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
            (key, module, description),
        )
    for role_name in GRANTED_TO:
        for key, _module, _description in PERMISSIONS:
            conn.exec_driver_sql(
                "INSERT INTO role_permissions (role_id, permission_key) "
                "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
                (key, role_name),
            )


def downgrade() -> None:
    conn = op.get_bind()
    keys = [p[0] for p in PERMISSIONS]
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key IN (" + ",".join(["%s"] * len(keys)) + ")",
        tuple(keys),
    )
    conn.exec_driver_sql(
        "DELETE FROM permissions WHERE key IN (" + ",".join(["%s"] * len(keys)) + ")",
        tuple(keys),
    )
