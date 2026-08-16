"""cash registers — Caixa Diário / Financeiro

Item "Implementar Caixa Diário / Financeiro". Verificado antes de
implementar (ver docstring de `models/cash_register.py`): não havia
nenhuma tabela/model/endpoint de caixa — só as permissions
`finance.view`/`finance.manage` (catálogo desde 0007), nunca usadas por
nenhuma rota até agora. Reaproveitadas aqui em vez de criar chaves
novas.

Fluxo: Comanda -> Pagamento -> Caixa -> Fechamento diário.

`payments.cash_register_id` passa a ser OBRIGATÓRIO no domínio (item
"pagamento obrigatoriamente vinculado ao caixa"), mas esta migration
NÃO pode simplesmente criar a coluna como `NOT NULL` direto: ao
contrário do que a primeira versão desta migration assumia, `payments`
(criada em 0013) **já foi aplicada em staging e já tem pagamento real
registrado** (confirmado em produção/staging antes desta correção — 1
linha em `orders`/`order_items`/`payments` no momento da revisão). Um
`ALTER TABLE ... ADD COLUMN ... NOT NULL` sem default falha imediatamente
nesse cenário (Postgres recusa NOT NULL sem valor pra linha já
existente). Por isso o caminho aqui é em 5 passos, todos dentro da
mesma transação da migration (Alembic/Postgres: se o passo de validação
falhar, a transação inteira dá rollback — nada fica pela metade):

  1. Cria a coluna `cash_register_id` como NULLABLE (sem FK ainda).
  2. Para cada organização que já tem pagamento sem caixa, cria UM
     caixa "histórico" sintético (fechado na hora, com notas explícitas
     dizendo que foi gerado por esta migration) e usa esse caixa pra
     preencher `cash_register_id` de todos os pagamentos legados
     daquela organização — não duplica um caixa por pagamento, agrupa
     por organização (mais simples, mesmo espírito de "não duplicar
     conceito" do item 27). O responsável do caixa histórico é
     resolvido em cascata: (a) `payments.created_by` do pagamento mais
     antigo sem caixa, se houver; senão (b) o usuário com role OWNER
     mais antigo da organização; senão (c) qualquer membership da
     organização. Se nenhuma das três encontrar um usuário (não deveria
     ser possível — toda organização tem pelo menos um OWNER), a
     migration falha alto e explícito em vez de inserir um caixa sem
     responsável.
  3. Valida com uma query que não sobrou nenhum `payments.cash_register_id
     IS NULL` — se sobrar, `RuntimeError` aborta a migration (rollback)
     em vez de seguir pro passo 4 com dado inconsistente.
  4. Só então altera a coluna pra `NOT NULL`.
  5. Cria a FK (`RESTRICT`) e o índice — depois do backfill, nunca antes
     (uma FK antes do backfill não teria efeito prático diferente aqui,
     mas manter a ordem "dado primeiro, constraint depois" é a mesma
     disciplina de qualquer migration com backfill).

`payments.created_by_name` (novo, snapshot do nome de quem registrou o
pagamento) é preenchido no mesmo passo de backfill, a partir do nome
atual de `payments.created_by` — continua NULLABLE depois (pagamentos
com `created_by` nulo, hoje permitido pelo model, ficam com nome nulo
mesmo, não é bloqueante).

Dois métodos de pagamento novos no enum `payment_method` (item "não
deixar métodos de pagamento importantes hardcoded"): `transfer`
(Transferência) e `bank_slip` (Boleto) — mesmo padrão de
`ALTER TYPE ... ADD VALUE` já usado em 0012 pro `appointment_status`.

Ver `tests/test_migration_0014_backfill.py` para o teste que cobre
exatamente este caminho com um pagamento legado real (schema aplicado
até 0013, INSERT de um payment "antigo", upgrade pra 0014, e assert de
que o payment sobrevive com `cash_register_id` preenchido e a coluna
virou NOT NULL).

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-15
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

NEW_PAYMENT_METHOD_VALUES = ["transfer", "bank_slip"]
OLD_PAYMENT_METHOD_VALUES = (
    "pix", "cash", "debit", "credit", "loyalty_card", "voucher", "barter",
)

ENUMS = {
    "cash_register_status": ["open", "closed"],
    "cash_movement_type": ["withdrawal", "supply", "reversal"],
}

DIRECT_ORG_TABLES = ["cash_registers", "cash_movements"]

_LEGACY_NOTE = (
    "Caixa criado automaticamente pela migration 0014 para preservar a "
    "vinculação de pagamento(s) registrados antes da existência do Caixa "
    "Diário. Fechado imediatamente — não é um caixa operacional."
)


def upgrade() -> None:
    for value in NEW_PAYMENT_METHOD_VALUES:
        # Mesma ressalva de 0012: só é seguro fora de uma transação que
        # também CONSOME o valor novo — esta migration não insere
        # nenhuma linha usando 'transfer'/'bank_slip', só adiciona o
        # valor ao tipo.
        op.execute(f"ALTER TYPE payment_method ADD VALUE IF NOT EXISTS '{value}'")

    for name, values in ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")

    op.create_table(
        "cash_registers",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("opened_by", sa.UUID(), nullable=False),
        sa.Column("opened_by_name", sa.String(length=255), nullable=False),
        sa.Column("initial_amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("opening_notes", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("open", "closed", name="cash_register_status", create_type=False),
            server_default="open",
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.UUID(), nullable=True),
        sa.Column("closed_by_name", sa.String(length=255), nullable=True),
        sa.Column("closing_notes", sa.Text(), nullable=True),
        sa.Column("expected_amount", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("counted_amount", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("difference", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)",
            name=op.f("ck_cash_registers_closed_at_matches_status"),
        ),
        sa.CheckConstraint("initial_amount >= 0", name=op.f("ck_cash_registers_initial_amount_not_negative")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_cash_registers_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opened_by"], ["users.id"], name=op.f("fk_cash_registers_opened_by_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_cash_registers_closed_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_registers")),
    )
    op.create_index("ix_cash_registers_status", "cash_registers", ["status"], unique=False)
    op.create_index("ix_cash_registers_opened_by", "cash_registers", ["opened_by"], unique=False)

    op.create_table(
        "cash_movements",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("cash_register_id", sa.UUID(), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM("withdrawal", "supply", "reversal", name="cash_movement_type", create_type=False),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_by_name", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_cash_movements_amount_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_cash_movements_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cash_register_id"], ["cash_registers.id"],
            name=op.f("fk_cash_movements_cash_register_id_cash_registers"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_cash_movements_created_by_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_movements")),
    )
    op.create_index("ix_cash_movements_cash_register_id", "cash_movements", ["cash_register_id"], unique=False)

    for table in DIRECT_ORG_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {_ORG_ID})")

    # payments -> caixa: coluna nasce NULLABLE (ver docstring do módulo
    # — só vira NOT NULL depois do backfill, no passo 4 abaixo). FK e
    # índice também ficam pra depois do backfill (passo 5).
    op.add_column("payments", sa.Column("cash_register_id", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("created_by_name", sa.String(length=255), nullable=True))

    _backfill_legacy_payments(op.get_bind())

    op.alter_column("payments", "cash_register_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_payments_cash_register_id_cash_registers"),
        "payments", "cash_registers", ["cash_register_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_payments_cash_register_id", "payments", ["cash_register_id"], unique=False)

    # finance.view pra RECEPTIONIST: quem já registra pagamento
    # (payments.register, migration 0013) precisa listar/ver caixas
    # abertos pra poder selecionar um — sem isso não conseguiria nem
    # completar o próprio fluxo de pagamento que já tinha permissão pra
    # fazer. finance.manage (abrir/fechar/sangria/suprimento) continua
    # só com OWNER/ADMIN (0007), decisão conservadora igual às demais
    # ações financeiras sensíveis do projeto.
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO role_permissions (role_id, permission_key) "
        "SELECT id, 'finance.view' FROM roles WHERE name = 'RECEPTIONIST' AND organization_id IS NULL"
    )


def _backfill_legacy_payments(conn: sa.engine.Connection) -> None:
    """Passos 2 e 3 da docstring do módulo — um caixa histórico por
    organização, cobrindo todo pagamento legado sem `cash_register_id`."""
    org_ids = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT DISTINCT organization_id FROM payments WHERE cash_register_id IS NULL")
        )
    ]

    for org_id in org_ids:
        user_id = _resolve_legacy_responsible(conn, org_id)
        user_name = conn.execute(
            sa.text("SELECT name FROM users WHERE id = :uid"), {"uid": user_id}
        ).scalar_one()

        # `cash_registers` tem RLS + FORCE — setamos o contexto de
        # organização igual a qualquer request real faria (mesmo padrão
        # de `set_config` usado em `cli/bootstrap_owner.py` e
        # `services/auth.py` fora do ciclo normal de request), em vez
        # de depender de o role de migration ignorar RLS por ser dono
        # do schema. Escopo `true` = só durante esta transação.
        conn.execute(
            sa.text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
        )

        register_id = conn.execute(
            sa.text(
                """
                INSERT INTO cash_registers (
                    organization_id, opened_by, opened_by_name, initial_amount,
                    opening_notes, status, closed_at, closed_by, closed_by_name, closing_notes
                ) VALUES (
                    :org, :uid, :uname, 0,
                    :note, 'closed', now(), :uid, :uname, :note
                )
                RETURNING id
                """
            ),
            {"org": org_id, "uid": user_id, "uname": user_name, "note": _LEGACY_NOTE},
        ).scalar_one()

        conn.execute(
            sa.text(
                """
                UPDATE payments
                SET cash_register_id = :register_id,
                    created_by_name = COALESCE(created_by_name, (SELECT name FROM users WHERE id = payments.created_by))
                WHERE organization_id = :org AND cash_register_id IS NULL
                """
            ),
            {"register_id": register_id, "org": org_id},
        )

    remaining = conn.execute(
        sa.text("SELECT COUNT(*) FROM payments WHERE cash_register_id IS NULL")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"Migration 0014: sobraram {remaining} pagamento(s) sem cash_register_id após o backfill "
            "— abortando antes de travar a coluna como NOT NULL (dado legado não coberto pela "
            "resolução de responsável; revisar antes de tentar de novo)."
        )


def _resolve_legacy_responsible(conn: sa.engine.Connection, org_id: uuid.UUID) -> uuid.UUID:
    """Cascata: (a) created_by do pagamento legado mais antigo da org;
    (b) OWNER mais antigo da org; (c) qualquer membership da org.
    Levanta erro explícito se nenhuma das três encontrar alguém — uma
    organização sem nenhum usuário não deveria existir, e preferimos
    falhar alto a inventar um responsável."""
    row = conn.execute(
        sa.text(
            """
            SELECT created_by FROM payments
            WHERE organization_id = :org AND cash_register_id IS NULL AND created_by IS NOT NULL
            ORDER BY created_at
            LIMIT 1
            """
        ),
        {"org": org_id},
    ).first()
    if row is not None:
        return row[0]

    row = conn.execute(
        sa.text(
            """
            SELECT om.user_id FROM organization_memberships om
            JOIN roles r ON r.id = om.role_id
            WHERE om.organization_id = :org AND r.name = 'OWNER'
            ORDER BY om.created_at
            LIMIT 1
            """
        ),
        {"org": org_id},
    ).first()
    if row is not None:
        return row[0]

    row = conn.execute(
        sa.text(
            "SELECT user_id FROM organization_memberships WHERE organization_id = :org ORDER BY created_at LIMIT 1"
        ),
        {"org": org_id},
    ).first()
    if row is not None:
        return row[0]

    raise RuntimeError(
        f"Migration 0014: organização {org_id} tem pagamento legado mas nenhum usuário/membership "
        "encontrado pra ser responsável pelo caixa histórico — abortando em vez de inventar um valor."
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key = 'finance.view' "
        "AND role_id IN (SELECT id FROM roles WHERE name = 'RECEPTIONIST' AND organization_id IS NULL)"
    )

    op.drop_index("ix_payments_cash_register_id", table_name="payments")
    op.drop_constraint(op.f("fk_payments_cash_register_id_cash_registers"), "payments", type_="foreignkey")
    op.drop_column("payments", "created_by_name")
    op.drop_column("payments", "cash_register_id")

    for table in reversed(DIRECT_ORG_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_table("cash_movements")
    op.drop_table("cash_registers")

    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")

    # payment_method: mesma técnica de 0012 (rename -> recria -> troca
    # o tipo da coluna -> apaga o antigo). Falha por design se alguma
    # linha ainda usar 'transfer'/'bank_slip' — não há migração segura
    # automática desses dados num downgrade.
    values_sql = ", ".join(f"'{v}'" for v in OLD_PAYMENT_METHOD_VALUES)
    conn.exec_driver_sql("ALTER TYPE payment_method RENAME TO payment_method_old")
    conn.exec_driver_sql(f"CREATE TYPE payment_method AS ENUM ({values_sql})")
    conn.exec_driver_sql(
        "ALTER TABLE payments ALTER COLUMN method TYPE payment_method USING method::text::payment_method"
    )
    conn.exec_driver_sql("DROP TYPE payment_method_old")
