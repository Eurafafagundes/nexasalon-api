"""order sale competence override (regularizacao temporaria de vendas antigas)

Rodada "Correcao de competencia de venda" — Faturamento/Extrato/Dashboard
passaram a usar `Order.created_at` como competencia de venda (nunca mais
`Order.closed_at`, nunca `CashRegister`). Mas durante a transicao pra o
fluxo definitivo (abrir caixa do dia -> operar -> fechar comandas -> fechar
caixa do dia), o usuario esta fechando HOJE comandas que na pratica
pertencem a dias anteriores (um caixa ficou aberto varios dias) -- e
`Order.created_at` NUNCA pode ser reescrito pra "consertar" isso, porque e
dado de auditoria real (quando a linha foi de fato criada).

Esta migration adiciona uma coluna NULLABLE, `sale_competence_override`,
que funciona como uma camada OPCIONAL por cima de `created_at` -- nunca o
substitui, nunca o edita:

    competencia efetiva = COALESCE(sale_competence_override, created_at)

`NULL` (todo `Order` ja existente, e todo `Order` novo do fluxo normal
daqui pra frente) = comportamento IDENTICO ao ja aprovado hoje. So fica
preenchida quando alguem escolhe explicitamente uma "Data da venda"
diferente no fechamento (tela de regularizacao) -- nunca em massa, nunca
silenciosamente.

SEM backfill -- nenhuma comanda existente precisa de valor nenhum aqui, o
bug nunca esteve nos timestamps armazenados, so na agregacao (ja corrigida
em rodada anterior).

Reversivel por natureza: e uma coluna aditiva (nunca reescreve
`created_at`/`closed_at`/`Payment.created_at`/`CashRegister`). O downgrade
abaixo so existe pra simetria do Alembic -- na pratica, a orientacao do
produto e NUNCA rodar este downgrade depois que a coluna tiver valores
reais gravados (dropar a coluna apagaria a correcao das comandas
regularizadas). "Desligar" a funcionalidade mais adiante significa parar
de oferecer o seletor na tela (frontend) e/ou bloquear a permissao de
enviar um novo valor -- nunca dropar esta coluna.

Revision ID: 0055
Revises: 0052
Create Date: 2026-09-25

Nota (2026-09-25): originalmente encadeada em 0054, mas 0053/0054
("Comissão condicional", feature não relacionada) ainda não foram
commitadas neste momento -- repontada para 0052 (o head real já
commitado em origin/main) para não travar o deploy desta feature numa
outra, independente, ainda em andamento. Quando 0053/0054 forem
finalizadas e commitadas, a cadeia deve ser reordenada manualmente
naquele momento (0055 passa a vir depois delas, ou vice-versa,
dependendo da ordem de deploy).
"""
import sqlalchemy as sa

from alembic import op

revision = "0055"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("sale_competence_override", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "sale_competence_override")
