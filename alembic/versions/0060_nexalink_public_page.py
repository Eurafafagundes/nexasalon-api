"""NexaLink: pagina publica do estabelecimento (config, links, eventos)

Rodada "NexaLink — página pública". Três tabelas novas + uma permission
nova, agrupadas numa única migration porque formam uma única feature
coesa (nenhuma faz sentido isolada das outras duas).

  - `organization_public_pages` (1:1 com `Organization`, FK `unique=True`,
    `ondelete=CASCADE`): config de publicação/identidade visual da
    página NexaLink. Padrão SPARSE deliberado — mesmo raciocínio de
    `CashRegisterConfig`/`AppointmentStatusStyle`: a linha só nasce no
    PRIMEIRO `PUT` (`services/organization_public_page.py`), nunca
    numa leitura; `GET` sem linha ainda devolve defaults EM MEMÓRIA.
    `display_name`/`short_description` nascem `NULL` e caem no fallback
    `Organization.name` em memória — nunca sobrescrevem o nome usado no
    resto do sistema. NÃO duplica `logo_url` (reusa
    `Organization.logo_url` direto, inclusive o endpoint de upload já
    existente, `POST /organization/logo` — nenhum endpoint novo de
    upload nesta migration).

  - `organization_public_links` (N:1 com `Organization`): um ÚNICO
    espaço de ordenação (`display_order`) compartilhado pelos 4 botões
    NATIVOS (`kind` = booking/whatsapp/instagram/location — uma linha
    cada, sempre existente a partir do 1º PUT, nunca deletável, só
    `is_visible=false`) e pelos links PERSONALIZADOS (`kind='custom'`,
    N linhas livres) — satisfaz o item "todos os botões, inclusive os
    nativos, precisam de ordenação configurável" sem duas fontes de
    ordem. Campos específicos de cada canal (`whatsapp_number`,
    `instagram_username`, `maps_url_override`, `title`/`url`/`icon_key`
    pra custom) ficam NULLABLE na mesma linha larga — 4 canais fixos não
    justificam uma tabela por canal.

  - `public_page_events` (append-only, alto volume — mesmo molde
    estrutural de `AuditLog`: `BigInteger` autoincrement, não UUID, de
    propósito, facilita particionar por `created_at` no futuro; sem
    `updated_at`, sem UPDATE/DELETE pela aplicação). `organization_id`
    NOT NULL (sempre conhecida aqui, resolvida por slug ANTES de
    qualquer insert — diferente de `AuditLog`, que tem casos de ação de
    plataforma sem org). NUNCA grava IP bruto (rate limit por IP fica
    só em memória, nunca persistido) nem telefone/CPF — só um cookie
    opaco (`visitor_cookie_id`) e um token opaco por carregamento de
    página (`attribution_token`), nunca fingerprint. Índice único
    PARCIAL em `appointment_id` `WHERE event_type='booking_completed'`
    garante, NO BANCO (não só na aplicação), que um retry de rede do
    cliente na criação do agendamento nunca duplica a conversão
    atribuída — ver `services/appointments.py::
    create_public_appointment_for_customer` e `api/v1/public_booking.py::
    create_booking`, que passam a aceitar `attribution_token` opcional.
    Retenção: 90 dias documentada em
    `services/public_page_events.py::purge_events_older_than` (LGPD) —
    sem scheduler automático nesta rodada (nenhuma infra de cron existe
    no projeto ainda); a função é testada e chamável manualmente/por
    tarefa futura.

  - `organization.manage_public_page` (permission NOVA): editar a
    config/links do NexaLink e ver o painel de Desempenho — DELIBERADAMENTE
    NÃO reaproveita `organization.manage` (que é OWNER-only e também
    controla dados financeiros/estruturais da organização, incluindo o
    upload de logo e o cadastro de endereço/CNPJ). Esta permission nova
    NUNCA é aceita nas rotas existentes `PUT /organization`/
    `POST /organization/logo` — só nas rotas novas de
    `api/v1/organization_public_page.py`. Concedida por padrão a
    OWNER + ADMIN (todo o propósito de existir é liberar isto pro ADMIN
    sem dar `organization.manage` completo).

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

_BUTTON_STYLE_VALUES = ["rounded", "pill", "square"]
_LINK_KIND_VALUES = ["booking", "whatsapp", "instagram", "location", "custom"]
_EVENT_TYPE_VALUES = [
    "page_view",
    "booking_click",
    "whatsapp_click",
    "instagram_click",
    "maps_click",
    "custom_link_click",
    "booking_completed",
]

_PERMISSION_KEY = "organization.manage_public_page"
_PERMISSION_MODULE = "organization"
_PERMISSION_DESCRIPTION = "Editar a página pública (NexaLink) e ver o painel de Desempenho"
_PERMISSION_GRANTED_TO = ["OWNER", "ADMIN"]


def upgrade() -> None:
    # --- Enums ---------------------------------------------------------
    button_style_sql = ", ".join(f"'{v}'" for v in _BUTTON_STYLE_VALUES)
    op.execute(f"CREATE TYPE public_page_button_style AS ENUM ({button_style_sql})")
    link_kind_sql = ", ".join(f"'{v}'" for v in _LINK_KIND_VALUES)
    op.execute(f"CREATE TYPE public_link_kind AS ENUM ({link_kind_sql})")
    event_type_sql = ", ".join(f"'{v}'" for v in _EVENT_TYPE_VALUES)
    op.execute(f"CREATE TYPE public_page_event_type AS ENUM ({event_type_sql})")

    # --- organization_public_pages --------------------------------------
    op.create_table(
        "organization_public_pages",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("display_name", sa.String(length=160), nullable=True),
        sa.Column("short_description", sa.String(length=280), nullable=True),
        sa.Column("primary_color_hex", sa.String(length=7), nullable=False, server_default="#7C3AED"),
        sa.Column("background_color_hex", sa.String(length=7), nullable=False, server_default="#FFFFFF"),
        sa.Column(
            "button_style",
            postgresql.ENUM(*_BUTTON_STYLE_VALUES, name="public_page_button_style", create_type=False),
            nullable=False,
            server_default="rounded",
        ),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "primary_color_hex ~ '^#[0-9A-Fa-f]{6}$'", name=op.f("ck_organization_public_pages_primary_color_format")
        ),
        sa.CheckConstraint(
            "background_color_hex ~ '^#[0-9A-Fa-f]{6}$'",
            name=op.f("ck_organization_public_pages_background_color_format"),
        ),
        sa.UniqueConstraint("organization_id", name=op.f("uq_organization_public_pages_organization_id")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_public_pages_organization_id_organizations"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"],
            name=op.f("fk_organization_public_pages_updated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_public_pages")),
    )
    op.execute("ALTER TABLE organization_public_pages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_public_pages FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON organization_public_pages USING (organization_id = {_ORG_ID})")

    # --- organization_public_links ---------------------------------------
    op.create_table(
        "organization_public_links",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(*_LINK_KIND_VALUES, name="public_link_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("custom_label", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=80), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("icon_key", sa.String(length=40), nullable=True),
        sa.Column("whatsapp_number", sa.String(length=20), nullable=True),
        sa.Column("whatsapp_message", sa.String(length=500), nullable=True),
        sa.Column("instagram_username", sa.String(length=80), nullable=True),
        sa.Column("maps_url_override", sa.String(length=2048), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        # DEFERRABLE INITIALLY DEFERRED — reordenar troca o `display_order`
        # de várias linhas na MESMA transação (ex.: A:0↔B:1 vira A:1,B:0);
        # sem isto, a UPDATE de uma linha por vez colidiria
        # momentaneamente com o valor ainda não atualizado da outra
        # (constraint checada por padrão a cada statement, não só no
        # commit). Com isto, Postgres só valida a unicidade no fim da
        # transação — ver `services/organization_public_page.py::
        # update_public_page`.
        sa.UniqueConstraint(
            "organization_id", "display_order",
            name=op.f("uq_organization_public_links_organization_id_display_order"),
            deferrable=True, initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_public_links_organization_id_organizations"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"],
            name=op.f("fk_organization_public_links_updated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_public_links")),
    )
    # Único índice pra "kind" — também UNIQUE parcial pros 4 nativos
    # (nunca duas linhas 'booking' pra mesma organização; custom fica
    # livre pra repetir, N linhas por organização).
    op.create_index(
        "ux_organization_public_links_org_native_kind", "organization_public_links", ["organization_id", "kind"],
        unique=True, postgresql_where=sa.text("kind != 'custom'"),
    )
    op.execute("ALTER TABLE organization_public_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_public_links FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON organization_public_links USING (organization_id = {_ORG_ID})")

    # --- public_page_events (append-only, molde de AuditLog) --------------
    op.create_table(
        "public_page_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "event_type",
            postgresql.ENUM(*_EVENT_TYPE_VALUES, name="public_page_event_type", create_type=False),
            nullable=False,
        ),
        sa.Column("link_id", sa.UUID(), nullable=True),
        sa.Column("appointment_id", sa.UUID(), nullable=True),
        sa.Column("attribution_token", sa.String(length=64), nullable=True),
        sa.Column("visitor_cookie_id", sa.String(length=64), nullable=True),
        sa.Column("utm_source", sa.String(length=80), nullable=True),
        sa.Column("utm_medium", sa.String(length=80), nullable=True),
        sa.Column("utm_campaign", sa.String(length=80), nullable=True),
        sa.Column("referrer_host", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_public_page_events_organization_id_organizations"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["link_id"], ["organization_public_links.id"],
            name=op.f("fk_public_page_events_link_id_organization_public_links"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["appointments.id"],
            name=op.f("fk_public_page_events_appointment_id_appointments"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_public_page_events")),
    )
    op.create_index(
        "ix_public_page_events_org_type_created", "public_page_events",
        ["organization_id", "event_type", "created_at"], unique=False,
    )
    op.create_index(
        "ux_public_page_events_booking_completed_appointment", "public_page_events", ["appointment_id"],
        unique=True, postgresql_where=sa.text("event_type = 'booking_completed'"),
    )
    # Índice de apoio pra validar attribution_token na criação do
    # agendamento (`services/public_page_events.py::resolve_attribution`)
    # sem scan de tabela inteira.
    op.create_index(
        "ix_public_page_events_org_token", "public_page_events",
        ["organization_id", "attribution_token"], unique=False,
        postgresql_where=sa.text("attribution_token IS NOT NULL"),
    )
    op.execute("ALTER TABLE public_page_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public_page_events FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON public_page_events USING (organization_id = {_ORG_ID})")

    # --- permission nova ---------------------------------------------------
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s) "
        "ON CONFLICT (key) DO NOTHING",
        (_PERMISSION_KEY, _PERMISSION_MODULE, _PERMISSION_DESCRIPTION),
    )
    for role_name in _PERMISSION_GRANTED_TO:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM role_permissions rp "
            "  JOIN roles r ON r.id = rp.role_id "
            "  WHERE r.name = %s AND r.organization_id IS NULL AND rp.permission_key = %s"
            ")",
            (_PERMISSION_KEY, role_name, role_name, _PERMISSION_KEY),
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DELETE FROM role_permissions WHERE permission_key = %s", (_PERMISSION_KEY,))
    conn.exec_driver_sql("DELETE FROM permissions WHERE key = %s", (_PERMISSION_KEY,))

    op.execute("DROP POLICY tenant_isolation ON public_page_events")
    op.execute("ALTER TABLE public_page_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public_page_events DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_public_page_events_org_token", table_name="public_page_events")
    op.drop_index(
        "ux_public_page_events_booking_completed_appointment", table_name="public_page_events",
        postgresql_where=sa.text("event_type = 'booking_completed'"),
    )
    op.drop_index("ix_public_page_events_org_type_created", table_name="public_page_events")
    op.drop_table("public_page_events")
    op.execute("DROP TYPE public_page_event_type")

    op.execute("DROP POLICY tenant_isolation ON organization_public_links")
    op.execute("ALTER TABLE organization_public_links NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_public_links DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ux_organization_public_links_org_native_kind", table_name="organization_public_links",
        postgresql_where=sa.text("kind != 'custom'"),
    )
    op.drop_table("organization_public_links")
    op.execute("DROP TYPE public_link_kind")

    op.execute("DROP POLICY tenant_isolation ON organization_public_pages")
    op.execute("ALTER TABLE organization_public_pages NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_public_pages DISABLE ROW LEVEL SECURITY")
    op.drop_table("organization_public_pages")
    op.execute("DROP TYPE public_page_button_style")
