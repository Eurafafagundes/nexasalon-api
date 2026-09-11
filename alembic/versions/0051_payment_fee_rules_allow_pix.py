"""payment_fee_rules: allow pix with sentinel card_brand

Etapa N3.1 — segundo passo (ver docstring de 0050, que só adiciona o
valor `not_applicable` ao enum `card_brand`; esta migration é quem de
fato o USA, num CHECK constraint — precisa ser uma transação separada,
já com o valor do enum commitado).

Duas alterações estruturais em `payment_fee_rules`, nenhuma coluna nova:

  1. `method_is_card` (só aceitava débito/crédito) vira
     `method_is_card_or_pix` — passa a aceitar também `pix`.
  2. `card_brand_matches_method` (CHECK novo) — cartão (débito/crédito)
     NUNCA pode ter `card_brand='not_applicable'`, Pix SEMPRE precisa
     ter `card_brand='not_applicable'`. Garante na borda do banco que
     uma linha de Pix nunca carregue uma bandeira de verdade nem
     vice-versa, mesmo se algum caminho futuro pular a validação do
     Pydantic (`schemas/payment_fee_rule.py`).

`UniqueConstraint(organization_id, method, card_brand, installments)`
(já existente, sem alteração) continua sendo suficiente pra impedir
duas regras de Pix "duplicadas" pra mesma organização — como
`card_brand` é sempre `not_applicable` (nunca `NULL`) pras linhas de
Pix, o Postgres já trata isso como um valor concreto igual a qualquer
outro na constraint (ao contrário do problema de `NULL <> NULL` que a
própria normalização de `installments` já evita — ver docstring do
model).

Nenhum dado existente é reescrito — só troca a definição das duas
constraints; todas as linhas atuais (débito/crédito) já satisfazem as
duas automaticamente (nunca têm `card_brand='not_applicable')`.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-11
"""
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

# Nomes com o prefixo `ck_payment_fee_rules_` — mesma naming_convention
# de `models/base.py::NAMING_CONVENTION` (`"ck":
# "ck_%(table_name)s_%(constraint_name)s"`), que é o nome REAL gravado
# no Postgres desde a criação da tabela em 0034 (`op.f("ck_payment_fee_rules_method_is_card")`).
# Usar o nome curto sem prefixo aqui faria o DROP CONSTRAINT falhar
# (confirmado rodando esta migration: `UndefinedObject: constraint
# "method_is_card" ... does not exist` — o nome curto nunca existiu no
# banco, só no argumento `name=` do model).
OLD_METHOD_CHECK_NAME = "ck_payment_fee_rules_method_is_card"
NEW_METHOD_CHECK_NAME = "ck_payment_fee_rules_method_is_card_or_pix"
BRAND_MATCHES_METHOD_CHECK_NAME = "ck_payment_fee_rules_card_brand_matches_method"

OLD_METHOD_CHECK_SQL = "method = 'debit' OR method = 'credit'"
NEW_METHOD_CHECK_SQL = "method IN ('debit', 'credit', 'pix')"
BRAND_MATCHES_METHOD_CHECK_SQL = (
    "(method IN ('debit', 'credit') AND card_brand <> 'not_applicable') OR "
    "(method = 'pix' AND card_brand = 'not_applicable')"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE payment_fee_rules DROP CONSTRAINT {OLD_METHOD_CHECK_NAME}")
    op.execute(
        f"ALTER TABLE payment_fee_rules ADD CONSTRAINT {NEW_METHOD_CHECK_NAME} CHECK ({NEW_METHOD_CHECK_SQL})"
    )
    op.execute(
        f"ALTER TABLE payment_fee_rules ADD CONSTRAINT {BRAND_MATCHES_METHOD_CHECK_NAME} "
        f"CHECK ({BRAND_MATCHES_METHOD_CHECK_SQL})"
    )


def downgrade() -> None:
    # Falha (erro nativo do Postgres) se alguma linha de Pix ainda
    # existir — mesma postura de 0012/0024: não apagar dado
    # silenciosamente num downgrade, deixar o CHECK antigo recusar.
    op.execute(f"ALTER TABLE payment_fee_rules DROP CONSTRAINT {BRAND_MATCHES_METHOD_CHECK_NAME}")
    op.execute(f"ALTER TABLE payment_fee_rules DROP CONSTRAINT {NEW_METHOD_CHECK_NAME}")
    op.execute(
        f"ALTER TABLE payment_fee_rules ADD CONSTRAINT {OLD_METHOD_CHECK_NAME} CHECK ({OLD_METHOD_CHECK_SQL})"
    )
