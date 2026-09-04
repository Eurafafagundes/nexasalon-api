"""payment reversal columns — reabertura de comanda fechada

Rodada "Reabertura e Cancelamento de Comandas". `payments` ganha 3
colunas NULLABLE, sem backfill (mesmo padrão já usado por
`fee_status`/Etapa N3): `reversed_at`/`reversed_by`/`reversed_by_name`.

Nunca editamos/apagamos um `Payment` — o pagamento revertido continua a
MESMA linha, pra sempre consultável (comprovante antigo, auditoria,
histórico da comanda). `reversed_at IS NOT NULL` é o marcador de "este
lançamento não conta mais": faturamento/extrato/comissão já páram de
contar sozinhos (todos filtram `Order.status == CLOSED`, e reabrir tira
a Order desse status); o Caixa é quem realmente PRECISA deste campo,
porque `services/cash_register.py::build_summary` lê `Payment`
diretamente por `cash_register_id`, sem olhar pro status da Order.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-04
"""
import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("reversed_by", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("reversed_by_name", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        op.f("fk_payments_reversed_by_users"),
        "payments",
        "users",
        ["reversed_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_payments_reversed_by_users"), "payments", type_="foreignkey")
    op.drop_column("payments", "reversed_by_name")
    op.drop_column("payments", "reversed_by")
    op.drop_column("payments", "reversed_at")
