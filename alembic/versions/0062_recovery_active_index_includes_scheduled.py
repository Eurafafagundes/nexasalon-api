"""Corrige integridade dos ciclos ativos da Central de Recuperação

A rodada V3 (migration 0061 + ajustes de serviço) passou a tratar
`scheduled` como um status ATIVO na camada de aplicação (um retorno já
marcado, mas ainda não realizado, continua em Acompanhamento — nunca
Concluídos — e `log_contact`/`link_return_appointment` passaram a
localizar um ciclo `scheduled` como o ciclo ativo do cliente). O índice
único parcial `uq_client_recovery_cycles_org_client_active`, porém,
continuou com o predicado original (`status IN ('pending',
'in_progress')`) — nunca foi ampliado para acompanhar essa mudança.
Resultado: o BANCO deixou de ser a última linha de defesa contra um
cliente com `scheduled` + `pending`/`in_progress` simultâneos (a
aplicação já impede isso hoje, mas só ela).

Esta migration substitui o índice para contemplar os 3 status ativos
(`pending`, `in_progress`, `scheduled`), numa única transação (DROP +
CREATE do mesmo índice, sem janela em que a tabela fique sem nenhuma
constraint de unicidade — `CREATE INDEX` sem `CONCURRENTLY` toma lock
de escrita na tabela durante toda a operação, e qualquer gravação
concorrente simplesmente espera essa transação terminar).

Antes de substituir, verifica se já existe algum cliente com mais de 1
ciclo entre os 3 status ativos — se existir, interrompe SEM alterar
nada (nunca apaga/edita automaticamente; exige correção manual).

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-25
"""
import sqlalchemy as sa

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_client_recovery_cycles_org_client_active"
_TABLE_NAME = "client_recovery_cycles"
_OLD_PREDICATE = "status IN ('pending', 'in_progress')"
_NEW_PREDICATE = "status IN ('pending', 'in_progress', 'scheduled')"


def upgrade() -> None:
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            f"""
            SELECT organization_id, client_id, COUNT(*) AS n
            FROM {_TABLE_NAME}
            WHERE {_NEW_PREDICATE}
            GROUP BY organization_id, client_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if duplicates:
        details = "; ".join(
            f"organization_id={row.organization_id} client_id={row.client_id} ciclos_ativos={row.n}"
            for row in duplicates
        )
        raise RuntimeError(
            f"Migration 0062 interrompida: {len(duplicates)} cliente(s) já possuem mais de 1 ciclo "
            f"de recuperação ativo (pending/in_progress/scheduled) simultaneamente — o índice não foi "
            f"alterado. Corrija manualmente (encerrar/consolidar os ciclos duplicados) antes de tentar "
            f"de novo. Detalhe: {details}"
        )

    # DROP + CREATE do MESMO índice dentro da mesma transação da
    # migration — nenhuma janela em que a tabela fique sem constraint de
    # unicidade: concorrência de escrita na tabela espera esta
    # transação terminar, nunca vê um estado intermediário.
    op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
    op.create_index(
        _INDEX_NAME, _TABLE_NAME, ["organization_id", "client_id"],
        unique=True, postgresql_where=sa.text(_NEW_PREDICATE),
    )


def downgrade() -> None:
    # Nunca precisa do mesmo pré-check: voltar ao predicado antigo só
    # AMPLIA o conjunto de combinações permitidas (scheduled deixa de
    # ser coberto), nunca pode violar a constraint resultante.
    op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
    op.create_index(
        _INDEX_NAME, _TABLE_NAME, ["organization_id", "client_id"],
        unique=True, postgresql_where=sa.text(_OLD_PREDICATE),
    )
