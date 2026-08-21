"""Normaliza slugs de organização já persistidos — correção pós-publicação

`normalize_slug` (`core/normalize.py`, Etapa K) só era chamada no
`PATCH /organizations` (`OrganizationUpdate`) — nunca na CRIAÇÃO, porque
não existe rota HTTP de criação de organização; a única forma de criar
uma é `cli/bootstrap_owner.py`, que pedia o slug ao operador e gravava
CRU (sem normalizar). Nenhum dado de teste/seed tem slug inválido hoje
(confirmado por grep), mas o gap existia e uma organização causada por
digitação livre no CLI (ex. "Meu Salão", com espaço e acento) ficaria
com o link público quebrado (`/agendar/Meu Salão` — a normalização do
lado da leitura não "conserta" o slug ARMAZENADO, só o que o backend
aceitaria salvar dali em diante). Esta migration:

  1. Corrige `cli/bootstrap_owner.py` para normalizar na criação (feito
     no mesmo commit, fora desta migration — aqui é só o dado já
     existente).
  2. Normaliza todo `slug` já gravado que não bate com sua própria forma
     normalizada, uma organização por vez (contexto RLS setado por
     linha, mesmo padrão de `0014_cash_registers.py`).
  3. Resolve colisão (duas orgs cuja forma normalizada colide) anexando
     um sufixo numérico incremental (`-2`, `-3`, ...) — `slug` continua
     UNIQUE (constraint de 0002), então uma colisão sem esse passo
     travaria a migration inteira no meio.

Puramente uma correção de DADOS — nenhuma coluna/constraint nova (a
coluna `slug` já é UNIQUE NOT NULL desde 0002). `downgrade` é
propositalmente NO-OP: não existe "forma anterior" segura pra
reverter (o slug normalizado continua um slug válido e visitável).

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-21
"""
import re
import unicodedata

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _normalize_slug(raw: str) -> str:
    """Cópia INTENCIONAL de `core/normalize.py::normalize_slug` — uma
    migration não deve importar código da aplicação (o código da app
    muda ao longo do tempo; a migration precisa continuar reproduzível
    do jeito que foi escrita no dia em que rodou)."""
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, slug FROM organizations")).fetchall()

    taken = {row[1] for row in rows}
    changed = 0
    for org_id, slug in rows:
        normalized = _normalize_slug(slug) or f"organizacao-{str(org_id)[:8]}"
        if normalized == slug:
            continue
        candidate = normalized
        suffix = 2
        while candidate in taken and candidate != slug:
            candidate = f"{normalized}-{suffix}"
            suffix += 1
        taken.discard(slug)
        taken.add(candidate)

        # `organizations` tem RLS + FORCE (migration 0003) — mesmo padrão
        # de contexto por-linha já usado em `0014_cash_registers.py`.
        conn.execute(sa.text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)})
        conn.execute(
            sa.text("UPDATE organizations SET slug = :slug WHERE id = :org"),
            {"slug": candidate, "org": org_id},
        )
        changed += 1


def downgrade() -> None:
    # Data-only, sem "forma anterior" registrada em lugar nenhum — nada
    # a desfazer com segurança. Um slug já normalizado continua válido.
    pass
