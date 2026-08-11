"""organization_memberships self-access policy (auth bootstrap)

Etapa 2D - Autenticacao.

DECISAO DE MODELAGEM (ajuste em policy RLS ja existente, criada na 0003):

Problema (bootstrap): no login, antes de sabermos em qual organizacao o
usuario vai operar, ainda NAO existe app.current_org_id setado na sessao.
Mas para decidir o fluxo pos-login (entrar direto se so ha 1 org, ou pedir
selecao se ha varias), o backend precisa listar TODAS as memberships ATIVAS
do usuario autenticado, atravessando organizacoes - algo que a policy
tenant_isolation original (organization_id = app.current_org_id) proibe por
definicao.

Solucao: adicionar uma nova variavel de sessao, app.current_user_id, setada
pelo dependency de auth a partir do token (nunca vinda do cliente), e
estender a policy de organization_memberships com uma clausula OR de
auto-acesso: o usuario sempre pode ver as PROPRIAS linhas de membership
(user_id = app.current_user_id), independente da organizacao. Isso NAO
enfraquece o isolamento entre usuarios diferentes: continua impossivel ver
memberships de outro user_id sem o current_org_id da organizacao dele
tambem estar setado.

app.current_user_id fica com default '' (nao 00000000-...) e o cast para
uuid so acontece dentro do NULLIF abaixo, para nao quebrar sessoes que nunca
setam essa variavel (ex.: contexto DEV_ONLY ou scripts internos).

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-11
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON organization_memberships")
    op.execute(
        "CREATE POLICY tenant_isolation ON organization_memberships "
        "USING ("
        "  organization_id = current_setting('app.current_org_id', true)::uuid "
        "  OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON organization_memberships")
    op.execute(
        "CREATE POLICY tenant_isolation ON organization_memberships "
        "USING (organization_id = current_setting('app.current_org_id', true)::uuid)"
    )
