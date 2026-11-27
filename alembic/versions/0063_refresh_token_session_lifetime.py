"""Adiciona limite absoluto de sessão e expiração por inatividade ao refresh token

Contexto: recepcionistas relatavam logout prematuro durante o uso normal
do sistema. Auditoria mostrou que a causa raiz não era o TTL do access
token (15 min, mantido sem alteração nesta migration), e sim uma corrida
de concorrência no fluxo de rotação do refresh token — múltiplas abas ou
requisições simultâneas apresentando o MESMO refresh token ainda não
rotacionado, disparando o mecanismo de detecção de roubo
(`revoke_all_for_user`) contra a própria sessão legítima. A correção de
concorrência (camadas de mutex/lock no frontend + janela de graça de
reuso no backend) é implementada em `services/auth.py`; esta migration
só prepara os 2 campos novos que ela precisa:

- `session_started_at`: gravado uma única vez no login, propagado sem
  alteração por toda a cadeia de rotações — permite um limite ABSOLUTO
  de sessão de verdade (7 dias), diferente de `issued_at`/`expires_at`,
  que hoje são recalculados a cada rotação e nunca acumulam um limite
  real.
- `last_used_at`: atualizado a cada rotação com sucesso — mede
  inatividade (12h) independente do limite absoluto acima.

Backfill (decisão já confirmada): sessões de refresh token já existentes
recebem `session_started_at = last_used_at = now()` — ganham um ciclo
novo de 12h/7 dias a partir do deploy, em vez de forçar um relogin em
massa imediato.

Coordenação migration/deploy (regra 4.1 do CLAUDE.md — precedente das
migrations 0058/0059): as 2 colunas levam `server_default=now()`,
mesmo sendo preenchidas explicitamente pelo código novo em TODO
INSERT. Sem o default, esta migration deixaria de ser "aditiva e
retrocompatível" — se aplicada ANTES do deploy do código novo (ordem
seguro padrão para mudança de schema aditiva), um login/refresh
servido pelo código ANTIGO ainda em produção (que não conhece estas
colunas) quebraria com violação de NOT NULL no INSERT. Com o default,
esse INSERT antigo continua funcionando (Postgres preenche `now()`
sozinho) durante a janela entre migration e deploy.

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-26
"""
import sqlalchemy as sa

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_TABLE_NAME = "refresh_tokens"


def upgrade() -> None:
    op.add_column(
        _TABLE_NAME,
        sa.Column(
            "session_started_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")
        ),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
    )

    # Backfill: sessões existentes ganham um ciclo novo a partir de
    # agora (opção já aprovada) — nunca força relogin em massa.
    op.execute(
        f"UPDATE {_TABLE_NAME} SET session_started_at = now(), last_used_at = now() "
        f"WHERE session_started_at IS NULL"
    )

    # `server_default` é preservado por este ALTER (só muda a
    # constraint NOT NULL) — continua valendo pra qualquer INSERT do
    # código antigo que não mencione estas colunas.
    op.alter_column(_TABLE_NAME, "session_started_at", nullable=False)
    op.alter_column(_TABLE_NAME, "last_used_at", nullable=False)


def downgrade() -> None:
    op.drop_column(_TABLE_NAME, "last_used_at")
    op.drop_column(_TABLE_NAME, "session_started_at")
