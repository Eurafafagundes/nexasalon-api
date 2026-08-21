"""add cancelled value to order_status enum

Etapa F (Agenda avançada + Cancelar Comanda) — item "Cancelar/Excluir
Comanda". Primeiro passo de dois: só adiciona o valor `cancelled` ao
enum nativo `order_status` (criado em 0013) — mesmo padrão já usado em
0012 pra `appointment_status`.

Separado em SUA PRÓPRIA migration, sem tocar em mais nada: `ALTER TYPE
... ADD VALUE` não pode ser CONSUMIDO (usado num CHECK/INSERT/
comparação) na MESMA TRANSAÇÃO em que é adicionado — o Postgres recusa
com `UnsafeNewEnumValueUsage` (confirmado rodando esta migration num
banco descartável). Isso vale mesmo entre migrations DIFERENTES: por
padrão o Alembic roda toda a cadeia de um `upgrade head` numa única
transação (`transaction_per_migration=False`, o default, nunca mudado
em `alembic/env.py`), então só separar em arquivos não bastaria — a
migration seguinte (0025, que usa o valor num CHECK/índice) ainda
correria na mesma transação.

IMPORTANTE (bug corrigido nesta versão): a primeira tentativa usava um
`op.get_bind().commit()` cru no fim do `upgrade()`. Isso quebra o
controle de transação do PRÓPRIO Alembic — o `context.begin_transaction()`
em `env.py` guarda uma referência à transação original; um commit cru
por fora desse controle a torna órfã, e o Alembic passa a operar sobre
uma NOVA transação implícita (auto-aberta pelo SQLAlchemy) da qual ele
não sabe. Resultado observado em teste: os DDLs de 0024/0025 executam
sem erro (por isso os logs mostram "Running upgrade ... -> 0025" e o
processo termina com exit code 0), mas como o Alembic nunca dá o commit
final NESSA transação órfã, TUDO depois do commit cru — inclusive o
UPDATE da própria tabela `alembic_version` — é perdido quando a conexão
fecha. `alembic upgrade head` "roda" mas o banco fica preso em 0023 e
sem a constraint nova.

Correção: usar o mecanismo oficial do Alembic pra isso,
`op.get_context().autocommit_block()` — ele comita a transação corrente
E religa o bookkeeping interno do Alembic numa transação nova logo em
seguida (`self._transaction = self.connection.begin()`), então o
restante da cadeia de migrations (0025) e o commit final continuam
funcionando normalmente.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-21
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

ENUM_NAME = "order_status"
NEW_VALUE = "cancelled"
OLD_VALUES = ("open", "closed")


def upgrade() -> None:
    # Ver docstring do módulo: `autocommit_block()` é o mecanismo oficial
    # do Alembic para DDL que precisa rodar fora de uma transação E ainda
    # assim deixar o restante da cadeia de migrations (0025) funcionando —
    # ao contrário de um `commit()` cru, ele reabre o bookkeeping interno
    # do Alembic numa transação nova ao sair do bloco.
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    # Postgres não tem "DROP VALUE" de enum — recria o tipo sem
    # 'cancelled'. Falha (erro nativo) se alguma linha ainda estiver
    # com status='cancelled' — mesma postura de 0012. A migration 0025
    # (que usa o valor num CHECK/índice) já precisa ter sido revertida
    # antes desta, pela própria ordem sequencial de downgrade.
    #
    # Diferença em relação ao downgrade de 0012 (appointment_status):
    #
    # 1. `orders.status` tem `server_default="open"` (0013) — Postgres
    #    recusa `ALTER COLUMN ... TYPE` enquanto o DEFAULT ainda
    #    referencia o tipo antigo (`DatatypeMismatch: default for column
    #    "status" cannot be cast automatically to type order_status`,
    #    confirmado rodando este downgrade num banco descartável). Por
    #    isso o DROP DEFAULT antes da troca de tipo e o SET DEFAULT
    #    depois, já no tipo novo.
    # 2. `orders` tem `ck_orders_closed_at_matches_status`, um CHECK que
    #    compara `status` com literais do enum (recriado pelo downgrade
    #    de 0025, que já roda antes deste, na forma de 2 ramos). Esse
    #    CHECK fica amarrado ao OID do tipo *atual* — ao renomear o tipo
    #    pra `order_status_old` e trocar a coluna pro tipo novo
    #    `order_status` (OID diferente, mesmo nome), o Postgres recusa a
    #    troca com `operator does not exist: order_status =
    #    order_status_old` (confirmado num banco descartável), porque o
    #    CHECK ainda compara contra o tipo velho. Por isso o DROP
    #    CONSTRAINT antes da troca de tipo e o ADD CONSTRAINT (mesmo
    #    texto, 2 ramos — já é o que 0025.downgrade() deixou) depois, já
    #    ligado ao tipo novo. Nenhuma das colunas que usam
    #    `appointment_status` em 0012 tem CHECK constraint, por isso
    #    aquele downgrade não precisa disso.
    conn = op.get_bind()
    values_sql = ", ".join(f"'{v}'" for v in OLD_VALUES)
    two_branch_check_sql = "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)"
    conn.exec_driver_sql("ALTER TABLE orders DROP CONSTRAINT ck_orders_closed_at_matches_status")
    conn.exec_driver_sql("ALTER TABLE orders ALTER COLUMN status DROP DEFAULT")
    conn.exec_driver_sql(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    conn.exec_driver_sql(f"CREATE TYPE {ENUM_NAME} AS ENUM ({values_sql})")
    conn.exec_driver_sql(f"ALTER TABLE orders ALTER COLUMN status TYPE {ENUM_NAME} USING status::text::{ENUM_NAME}")
    conn.exec_driver_sql(f"ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'open'::{ENUM_NAME}")
    conn.exec_driver_sql(
        f"ALTER TABLE orders ADD CONSTRAINT ck_orders_closed_at_matches_status CHECK ({two_branch_check_sql})"
    )
    conn.exec_driver_sql(f"DROP TYPE {ENUM_NAME}_old")
