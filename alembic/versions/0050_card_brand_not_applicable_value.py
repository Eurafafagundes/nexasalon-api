"""add not_applicable value to card_brand enum

Etapa N3.1 (Pix passa a poder ter taxa) — primeiro passo de dois: só
adiciona o valor `not_applicable` ao enum nativo `card_brand` — mesmo
padrão já usado em 0024 pra `order_status`. `not_applicable` é um
sentinela interno de `PaymentFeeRule.card_brand` pra linhas de Pix (que
não tem bandeira de verdade) — nunca aceito nem exposto pela API como
valor de bandeira (`schemas/payment_fee_rule.py` converte de/para
`None` na borda) e nunca usado em `Payment.card_brand` (esse continua
`NULL` pra Pix).

Ver docstring de 0024 pro raciocínio completo de por que isso precisa
ser uma migration separada: `ALTER TYPE ... ADD VALUE` não pode ser
CONSUMIDO (usado num CHECK/comparação) na MESMA TRANSAÇÃO em que é
adicionado, e o Alembic roda toda a cadeia de `upgrade head` numa
transação só por padrão — por isso `autocommit_block()` aqui, e a
migration 0051 (que usa o valor no CHECK de `payment_fee_rules`) só
roda depois deste valor estar commitado.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-11
"""
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

ENUM_NAME = "card_brand"
NEW_VALUE = "not_applicable"
OLD_VALUES = ("visa", "mastercard", "elo", "amex", "hipercard", "other")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    # Postgres não tem "DROP VALUE" de enum — recria o tipo sem
    # 'not_applicable'. Falha (erro nativo) se alguma `payment_fee_rules`
    # ainda usar o valor — a migration 0051 (que introduz o CHECK que
    # exige esse valor pras linhas de Pix) já precisa ter sido revertida
    # antes desta, pela própria ordem sequencial de downgrade, então
    # nenhuma linha deveria restar com `card_brand='not_applicable'`
    # nesse ponto (mesma postura de 0024: falhar alto em vez de apagar
    # dado silenciosamente).
    #
    # O tipo é usado em DUAS colunas (`payments.card_brand`, nullable, e
    # `payment_fee_rules.card_brand`, NOT NULL) — as duas precisam ser
    # convertidas pro tipo novo na troca.
    conn = op.get_bind()
    values_sql = ", ".join(f"'{v}'" for v in OLD_VALUES)
    conn.exec_driver_sql(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    conn.exec_driver_sql(f"CREATE TYPE {ENUM_NAME} AS ENUM ({values_sql})")
    conn.exec_driver_sql(
        f"ALTER TABLE payments ALTER COLUMN card_brand TYPE {ENUM_NAME} USING card_brand::text::{ENUM_NAME}"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE payment_fee_rules ALTER COLUMN card_brand TYPE {ENUM_NAME} "
        f"USING card_brand::text::{ENUM_NAME}"
    )
    conn.exec_driver_sql(f"DROP TYPE {ENUM_NAME}_old")
