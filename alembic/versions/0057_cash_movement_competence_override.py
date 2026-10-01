"""cash movement competence override (data da despesa)

Rodada "Data da despesa" — hoje uma Entrada/Despesa manual
(`CashMovement`) só tem `created_at` (quando o lançamento foi
REGISTRADO no sistema). Precisamos separar isso de QUANDO a despesa
REALMENTE ocorreu: o usuário pode estar em 01/10 e precisar lançar uma
despesa que ocorreu de fato em 25/09 — ela precisa contar no Extrato/
Dashboard/Resultado disponível de setembro, nunca de outubro.

Mesmo padrão já aprovado e em produção para `Order.sale_competence_override`
(migration 0055): coluna NULLABLE, camada OPCIONAL por cima de
`created_at` -- nunca o substitui, nunca o edita:

    competencia efetiva = COALESCE(competence_override, created_at)

`NULL` (toda `CashMovement` ja existente, e toda nova do fluxo normal
do dia a dia) = comportamento IDENTICO ao ja aprovado hoje. So fica
preenchida quando o usuario escolhe explicitamente uma "Data da
despesa" diferente de hoje ao registrar o lancamento
(`services/cash_register.py::register_movement`) -- nunca em massa,
nunca silenciosamente.

SEM backfill -- nenhum lancamento existente precisa de valor nenhum
aqui: `NULL` ja significa corretamente "competencia = created_at", que
e exatamente o comportamento historico correto para todo lancamento
anterior a esta feature (nunca houve uma "Data da despesa" diferente
de created_at antes de hoje existir essa opcao).

Reversivel por natureza: e uma coluna aditiva (nunca reescreve
`created_at`/`cash_register_id`). O downgrade abaixo so existe pra
simetria do Alembic -- na pratica, NUNCA rodar este downgrade depois
que a coluna tiver valores reais gravados (dropar a coluna apagaria a
competencia escolhida de despesas regularizadas). "Desligar" a
funcionalidade mais adiante significa parar de oferecer o campo na
tela (frontend) -- nunca dropar esta coluna.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-01

Nota sobre a cadeia: 0053/0054 ("Comissao condicional") seguem locais/
nao publicadas neste momento (fork separado a partir de 0052, ver nota
em 0055). Esta migration encadeia em 0056 -- o HEAD real ja publicado
em origin/main -- para nao se misturar com essa feature independente
ainda em andamento, mesma decisao que 0055 ja tomou.
"""
import sqlalchemy as sa

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cash_movements",
        sa.Column("competence_override", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cash_movements", "competence_override")
