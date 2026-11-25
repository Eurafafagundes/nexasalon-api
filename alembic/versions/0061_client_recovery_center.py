"""Central de Recuperação de Clientes

Rodada "Central de Recuperação de Clientes" (Clientes → Sem retorno).
Transforma o filtro client-side atual numa fila operacional real:
indicadores, subabas (Pendentes/Acompanhamento/Concluídos), registro de
contato, vínculo verificável com agendamento de retorno, histórico
auditável. Schema novo agrupado nesta migration porque forma uma única
feature coesa.

  - `clients.commercial_contact_opt_out_at`/`_reason` (ALTER aditivo):
    preferência de "não receber contatos comerciais" — pertence ao
    CLIENTE, não a um ciclo de recuperação específico (sobrevive a
    qualquer ciclo futuro). Nunca confundir com `is_active`/arquivamento
    nem com bloqueio de mensagens TRANSACIONAIS do próprio atendimento.

  - `client_recovery_cycles`: estado OPERACIONAL ATUAL de um ciclo (1
    linha por ciclo). Índice único PARCIAL garante no BANCO (não só na
    aplicação) que só existe 1 ciclo ATIVO (`pending`/`in_progress`) por
    cliente/organização. Concorrência otimista (`version`, mesmo padrão
    de `ClientObservation`/`Order.observation_version`).

  - `client_recovery_events`: histórico IMUTÁVEL (append-only, mesmo
    molde estrutural de `AuditLog` — `BigInteger`, sem `updated_at`,
    sem UPDATE/DELETE pela aplicação) de cada ação sobre um ciclo.
    `actor_user_name` é snapshot (nunca join a `users.name`).

  - `organization_recovery_settings`: config 1:1 ESPARSA por
    organização, mesmo padrão de `CashRegisterConfig` — linha só nasce
    no 1º PUT, leitura sem linha devolve defaults em memória (60/3/90
    dias + mensagem padrão de WhatsApp).

  - 3 permissions NOVAS (`clients.recovery.view`/`.manage`/
    `.view_team`), concedidas por padrão a OWNER/ADMIN/RECEPTIONIST
    (mesmo precedente de `clients.view`/`clients.manage`) — `view_team`
    concedida junto com `view` por decisão de produto (por padrão
    qualquer papel com acesso à fila vê a fila INTEIRA, não só os
    clientes atribuídos a si; a organização pode restringir depois
    revogando só `view_team` de membros específicos via
    ManageAccessDrawer).

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

_CYCLE_STATUS_VALUES = [
    "pending", "in_progress", "scheduled", "recovered", "lost", "invalid_phone", "opted_out", "cancelled",
]
_EVENT_TYPE_VALUES = [
    "created", "contact_logged", "reassigned", "status_changed", "appointment_linked", "reopened", "closed",
]
_CONTACT_CHANNEL_VALUES = ["whatsapp", "call", "instagram", "in_person", "other"]
_CONTACT_RESULT_VALUES = [
    "awaiting_response", "interested", "call_later", "return_scheduled",
    "not_interested", "invalid_phone", "opted_out", "unreachable",
]

_PERMISSIONS = [
    ("clients.recovery.view", "clients", "Ver a Central de Recuperação de Clientes"),
    ("clients.recovery.manage", "clients", "Registrar contatos e gerenciar ciclos na Central de Recuperação"),
    ("clients.recovery.view_team", "clients", "Ver a fila de recuperação de todos os atendentes, não só a própria"),
]
_GRANTED_TO = ["OWNER", "ADMIN", "RECEPTIONIST"]


def upgrade() -> None:
    # --- Enums -----------------------------------------------------------
    cycle_status_sql = ", ".join(f"'{v}'" for v in _CYCLE_STATUS_VALUES)
    op.execute(f"CREATE TYPE recovery_cycle_status AS ENUM ({cycle_status_sql})")
    event_type_sql = ", ".join(f"'{v}'" for v in _EVENT_TYPE_VALUES)
    op.execute(f"CREATE TYPE recovery_event_type AS ENUM ({event_type_sql})")
    contact_channel_sql = ", ".join(f"'{v}'" for v in _CONTACT_CHANNEL_VALUES)
    op.execute(f"CREATE TYPE recovery_contact_channel AS ENUM ({contact_channel_sql})")
    contact_result_sql = ", ".join(f"'{v}'" for v in _CONTACT_RESULT_VALUES)
    op.execute(f"CREATE TYPE recovery_contact_result AS ENUM ({contact_result_sql})")

    # --- clients: preferência de contato comercial ------------------------
    op.add_column("clients", sa.Column("commercial_contact_opt_out_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("commercial_contact_opt_out_reason", sa.String(length=255), nullable=True))

    # --- client_recovery_cycles -------------------------------------------
    op.create_table(
        "client_recovery_cycles",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_CYCLE_STATUS_VALUES, name="recovery_cycle_status", create_type=False),
            nullable=False, server_default="pending",
        ),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("assigned_user_name", sa.String(length=160), nullable=True),
        sa.Column("reference_appointment_id", sa.UUID(), nullable=True),
        sa.Column("linked_appointment_id", sa.UUID(), nullable=True),
        sa.Column("next_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppressed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            onupdate=sa.text("now()"), nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_client_recovery_cycles_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_client_recovery_cycles_client_id_clients"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_user_id"], ["users.id"],
            name=op.f("fk_client_recovery_cycles_assigned_user_id_users"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reference_appointment_id"], ["appointments.id"],
            name=op.f("fk_client_recovery_cycles_reference_appointment_id_appointments"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["linked_appointment_id"], ["appointments.id"],
            name=op.f("fk_client_recovery_cycles_linked_appointment_id_appointments"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_recovery_cycles")),
    )
    op.create_index(
        "ix_client_recovery_cycles_org_client", "client_recovery_cycles",
        ["organization_id", "client_id"], unique=False,
    )
    op.create_index(
        "ix_client_recovery_cycles_org_assigned", "client_recovery_cycles",
        ["organization_id", "assigned_user_id"], unique=False,
    )
    op.create_index(
        "ix_client_recovery_cycles_org_status", "client_recovery_cycles",
        ["organization_id", "status"], unique=False,
    )
    # 1 ciclo ATIVO por cliente/organização, garantido no banco (não só
    # na service layer) — última linha de defesa contra duplicação de
    # ciclo sob concorrência real.
    op.create_index(
        "uq_client_recovery_cycles_org_client_active", "client_recovery_cycles",
        ["organization_id", "client_id"], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'in_progress')"),
    )
    op.execute("ALTER TABLE client_recovery_cycles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE client_recovery_cycles FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON client_recovery_cycles USING (organization_id = {_ORG_ID})")

    # --- client_recovery_events (append-only) -----------------------------
    op.create_table(
        "client_recovery_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("recovery_cycle_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_name", sa.String(length=160), nullable=True),
        sa.Column(
            "event_type", postgresql.ENUM(*_EVENT_TYPE_VALUES, name="recovery_event_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "contact_channel",
            postgresql.ENUM(*_CONTACT_CHANNEL_VALUES, name="recovery_contact_channel", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "contact_result",
            postgresql.ENUM(*_CONTACT_RESULT_VALUES, name="recovery_contact_result", create_type=False),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "previous_status",
            postgresql.ENUM(*_CYCLE_STATUS_VALUES, name="recovery_cycle_status", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "new_status", postgresql.ENUM(*_CYCLE_STATUS_VALUES, name="recovery_cycle_status", create_type=False),
            nullable=True,
        ),
        sa.Column("next_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("linked_appointment_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_client_recovery_events_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recovery_cycle_id"], ["client_recovery_cycles.id"],
            name=op.f("fk_client_recovery_events_recovery_cycle_id_client_recovery_cycles"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_client_recovery_events_client_id_clients"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"],
            name=op.f("fk_client_recovery_events_actor_user_id_users"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["linked_appointment_id"], ["appointments.id"],
            name=op.f("fk_client_recovery_events_linked_appointment_id_appointments"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_recovery_events")),
    )
    op.create_index(
        "ix_client_recovery_events_org_cycle", "client_recovery_events",
        ["organization_id", "recovery_cycle_id"], unique=False,
    )
    op.create_index(
        "ix_client_recovery_events_org_client", "client_recovery_events",
        ["organization_id", "client_id"], unique=False,
    )
    op.execute("ALTER TABLE client_recovery_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE client_recovery_events FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON client_recovery_events USING (organization_id = {_ORG_ID})")

    # --- organization_recovery_settings (1:1 esparsa) ---------------------
    op.create_table(
        "organization_recovery_settings",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("default_no_return_days", sa.Integer(), nullable=True),
        sa.Column("default_follow_up_days", sa.Integer(), nullable=True),
        sa.Column("default_snooze_days", sa.Integer(), nullable=True),
        sa.Column("whatsapp_message_template", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            onupdate=sa.text("now()"), nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "default_no_return_days IS NULL OR default_no_return_days > 0",
            name="ck_default_no_return_days_positive",
        ),
        sa.CheckConstraint(
            "default_follow_up_days IS NULL OR default_follow_up_days > 0",
            name="ck_default_follow_up_days_positive",
        ),
        sa.CheckConstraint(
            "default_snooze_days IS NULL OR default_snooze_days > 0",
            name="ck_default_snooze_days_positive",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_recovery_settings_organization_id_organizations"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"],
            name=op.f("fk_organization_recovery_settings_updated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_recovery_settings")),
        sa.UniqueConstraint("organization_id", name=op.f("uq_organization_recovery_settings_organization_id")),
    )
    op.execute("ALTER TABLE organization_recovery_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_recovery_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON organization_recovery_settings USING (organization_id = {_ORG_ID})"
    )

    # --- Permissions -------------------------------------------------------
    conn = op.get_bind()
    for key, module, description in _PERMISSIONS:
        conn.exec_driver_sql(
            "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (key, module, description),
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
                (key, role_name, role_name, key),
            )


def downgrade() -> None:
    conn = op.get_bind()
    for key, _module, _description in _PERMISSIONS:
        conn.exec_driver_sql("DELETE FROM role_permissions WHERE permission_key = %s", (key,))
        conn.exec_driver_sql("DELETE FROM permissions WHERE key = %s", (key,))

    op.execute("DROP POLICY tenant_isolation ON organization_recovery_settings")
    op.execute("ALTER TABLE organization_recovery_settings NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_recovery_settings DISABLE ROW LEVEL SECURITY")
    op.drop_table("organization_recovery_settings")

    op.execute("DROP POLICY tenant_isolation ON client_recovery_events")
    op.execute("ALTER TABLE client_recovery_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE client_recovery_events DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_client_recovery_events_org_client", table_name="client_recovery_events")
    op.drop_index("ix_client_recovery_events_org_cycle", table_name="client_recovery_events")
    op.drop_table("client_recovery_events")

    op.execute("DROP POLICY tenant_isolation ON client_recovery_cycles")
    op.execute("ALTER TABLE client_recovery_cycles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE client_recovery_cycles DISABLE ROW LEVEL SECURITY")
    op.drop_index("uq_client_recovery_cycles_org_client_active", table_name="client_recovery_cycles")
    op.drop_index("ix_client_recovery_cycles_org_status", table_name="client_recovery_cycles")
    op.drop_index("ix_client_recovery_cycles_org_assigned", table_name="client_recovery_cycles")
    op.drop_index("ix_client_recovery_cycles_org_client", table_name="client_recovery_cycles")
    op.drop_table("client_recovery_cycles")

    op.drop_column("clients", "commercial_contact_opt_out_reason")
    op.drop_column("clients", "commercial_contact_opt_out_at")

    op.execute("DROP TYPE recovery_contact_result")
    op.execute("DROP TYPE recovery_contact_channel")
    op.execute("DROP TYPE recovery_event_type")
    op.execute("DROP TYPE recovery_cycle_status")
