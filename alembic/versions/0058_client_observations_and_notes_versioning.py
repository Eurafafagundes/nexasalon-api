"""central de observacoes do cliente: client_observations, auditoria com
nome do usuario, versionamento de Appointment.notes

Rodada "Centralização de observações do cliente e auditoria completa".
Três mudanças de schema, agrupadas porque nenhuma precisa de rollback
isolado da outra nesta etapa:

  - `client_observations` (tabela NOVA): observações GERAIS do cliente —
    hoje `Client.notes` é um ÚNICO campo de texto livre, sobrescrito a
    cada PUT /clients/{id} (full-replace), sem versão, sem autor, sem
    auditoria (ver auditoria desta rodada: `services/clients.py::
    update_client` nunca grava `AuditLog`). O pedido exige múltiplas
    observações gerais INDEPENDENTES ("não sobrescrever automaticamente
    a anterior") — incompatível com um único campo, por isso tabela
    própria, nunca um campo a mais em `Client`.

    `created_at`/`created_by`/`created_by_name` e
    `updated_at`/`updated_by`/`updated_by_name` são NULLABLE de propósito
    (diferente de `TimestampMixin`, que é NOT NULL com `server_default
    now()`): uma observação importada do `Client.notes` legado (ver
    backfill abaixo) não tem data/autor de criação conhecidos — "Data de
    criação não registrada"/"Autor não registrado" (item explícito do
    pedido) só são honestos se a coluna puder mesmo ser `NULL`, nunca um
    `now()` inventado no momento do backfill. Para uma observação NOVA
    (criada a partir desta versão), `created_at`/`created_by` são
    sempre preenchidos pelo service (nunca pelo frontend).
    `updated_at/by/by_name` ficam `NULL` até a PRIMEIRA edição real —
    mesmo padrão já usado em `orders.observation_updated_at` (migration
    0039): nasce vazio, só existe depois que algo de fato mudou.

    `version` (INTEGER NOT NULL, nasce em `0`) é o mesmo padrão de
    concorrência otimista de `orders.observation_version` — "nunca
    editada" é um valor real e comparável, nunca um timestamp `NULL`
    inicial (que não distingue "nunca editada" de "sem controle de
    versão").

    `is_legacy_import` (Boolean) distingue uma linha nascida do backfill
    de `Client.notes` de uma observação criada de verdade por um usuário
    nesta versão — nunca os dois tipos poderiam ser confundidos na UI.

    Sem soft-delete/`is_active`: o pedido explicitamente não inclui
    exclusão nesta versão ("Não implementar exclusão física de
    observações nesta versão" — e também nenhuma funcionalidade de
    exclusão lógica foi pedida).

  - `audit_logs.user_name` (coluna NOVA, nullable): `AuditLog` guarda só
    `user_id` (FK `ON DELETE SET NULL`) — um histórico que fizesse JOIN
    a `users.name` ao vivo mudaria de nome se o usuário for renomeado
    depois, e viraria `NULL` se o usuário for desativado/removido,
    quebrando a garantia pedida ("nome histórico preservado"). Mesmo
    padrão já usado em `orders.observation_updated_by_name`/
    `order_items.professional_name`: snapshot capturado no MOMENTO da
    ação, nunca um join. Sem backfill (linhas antigas de `AuditLog`
    ficam com `user_name IS NULL` — não há como reconstruir
    retroativamente o nome que o usuário tinha na época de cada ação
    passada; a partir desta migration, todo NOVO registro de auditoria
    passa a preencher esta coluna).

  - `appointments.notes_version`/`notes_updated_at`/`notes_updated_by`/
    `notes_updated_by_name`: `Appointment.notes` hoje só tem
    `appointments.updated_by` GENÉRICO (muda por QUALQUER edição do
    agendamento — reagendar, trocar status etc. — nunca só pela
    observação; usá-lo como "última edição da observação" violaria o
    item explícito do pedido "não deve representar a última atualização
    genérica"). Réplica EXATA do padrão de `orders.observation_version`
    (migration 0039) para a mesma necessidade: concorrência otimista +
    snapshot de autor dedicados à observação do agendamento, sem
    reaproveitar nenhuma coluna genérica do Appointment.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-24
"""
import sqlalchemy as sa

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    # --- client_observations ---------------------------------------------
    op.create_table(
        "client_observations",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_legacy_import", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=160), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("updated_by_name", sa.String(length=160), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_client_observations_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_client_observations_client_id_clients"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name=op.f("fk_client_observations_created_by_users"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"],
            name=op.f("fk_client_observations_updated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_observations")),
    )
    op.create_index(
        "ix_client_observations_org_client", "client_observations",
        ["organization_id", "client_id"], unique=False,
    )
    op.execute("ALTER TABLE client_observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE client_observations FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON client_observations USING (organization_id = {_ORG_ID})")

    # --- backfill: Client.notes legado vira 1 observação geral "importada" -
    # created_at/created_by ficam NULL de propósito (ver docstring acima
    # — nunca inventar data/autor que não existem). `version=0`: ainda
    # não foi editada por ninguém através da nova interface.
    op.execute(
        """
        INSERT INTO client_observations
            (organization_id, client_id, content, version, is_legacy_import)
        SELECT organization_id, id, TRIM(notes), 0, true
        FROM clients
        WHERE notes IS NOT NULL AND TRIM(notes) <> ''
        """
    )

    # --- audit_logs.user_name ---------------------------------------------
    op.add_column("audit_logs", sa.Column("user_name", sa.String(length=160), nullable=True))

    # --- appointments: versionamento dedicado de notes ---------------------
    op.add_column("appointments", sa.Column("notes_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("appointments", sa.Column("notes_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("appointments", sa.Column("notes_updated_by", sa.UUID(), nullable=True))
    op.add_column("appointments", sa.Column("notes_updated_by_name", sa.String(length=160), nullable=True))
    op.create_foreign_key(
        op.f("fk_appointments_notes_updated_by_users"),
        "appointments", "users", ["notes_updated_by"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_appointments_notes_updated_by_users"), "appointments", type_="foreignkey")
    op.drop_column("appointments", "notes_updated_by_name")
    op.drop_column("appointments", "notes_updated_by")
    op.drop_column("appointments", "notes_updated_at")
    op.drop_column("appointments", "notes_version")

    op.drop_column("audit_logs", "user_name")

    op.execute("DROP POLICY tenant_isolation ON client_observations")
    op.execute("ALTER TABLE client_observations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE client_observations DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_client_observations_org_client", table_name="client_observations")
    op.drop_table("client_observations")
