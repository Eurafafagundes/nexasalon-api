"""professional_services: allow_online_booking (disponibilidade por vinculo no agendamento online)

Etapa "Disponibilidade por profissional x servico no Agendamento Online"
(auditoria aprovada em 2026-09-27). Ate aqui, a elegibilidade de uma
combinacao profissional+servico no fluxo publico era derivada so de DUAS
flags GLOBAIS e independentes (`Professional.allow_online_booking`,
`Service.allow_online_booking`) + `ProfessionalService.is_active`
("realiza o servico") -- nao existia como desligar SO uma combinacao
especifica (ex.: Ingrid continua fazendo "Manutencao 3 Telas"
internamente, mas essa combinacao nao deve aparecer no online) sem
desligar o profissional inteiro ou o servico inteiro (afetando TODOS os
outros vinculos).

Esta migration adiciona uma quarta flag, `allow_online_booking`, agora no
proprio VINCULO (`ProfessionalService`) -- mesmo nome ja usado nas outras
duas camadas (`Professional.allow_online_booking`,
`Service.allow_online_booking`), pelo mesmo motivo: consistencia de
nomenclatura entre as tres camadas da mesma decisao.

Estrategia (aprovada explicitamente, nunca um `server_default` cego):

1. adiciona a coluna NULLABLE;
2. backfill DERIVADO (nunca um "chute"): `allow_online_booking = is_active`
   -- preserva byte a byte a elegibilidade HOJE de cada vinculo (hoje,
   elegibilidade = is_active AND as duas flags globais; depois do
   backfill, a formula final com as 4 condicoes continua equivalente,
   porque `allow_online_booking` reproduz exatamente o `is_active` que ja
   valia). Nenhum profissional/servico desaparece nem aparece a mais no
   online por causa desta migration;
3. `NOT NULL`;
4. `server_default='true'` (dai em diante -- vinculo novo continua
   "realiza = disponivel online" por padrao, mesmo comportamento intuitivo
   de hoje, sem exigir acao extra do usuario ao simplesmente ligar um
   servico novo para um profissional);
5. CHECK constraint proibindo a combinacao inconsistente
   `is_active=false AND allow_online_booking=true` -- um vinculo que o
   profissional nao realiza mais NUNCA pode ficar marcado como disponivel
   online. A ordem (backfill ANTES do CHECK) e proposital: aplicar o CHECK
   antes do backfill falharia contra qualquer linha `is_active=false`
   pre-existente, porque o `server_default='true'` teria acabado de
   marcar TODAS as linhas (inclusive as inativas) como `true`.

Sem downgrade de dado (a coluna e' aditiva) -- `downgrade()` so remove o
CHECK e a coluna, simetria padrao do Alembic.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-27
"""
import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

CHECK_NAME = "online_booking_requires_active_service"


def upgrade() -> None:
    op.add_column(
        "professional_services",
        sa.Column("allow_online_booking", sa.Boolean(), nullable=True),
    )
    op.execute("UPDATE professional_services SET allow_online_booking = is_active")
    op.alter_column(
        "professional_services",
        "allow_online_booking",
        nullable=False,
        server_default="true",
    )
    op.create_check_constraint(
        CHECK_NAME,
        "professional_services",
        "is_active OR NOT allow_online_booking",
    )


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, "professional_services", type_="check")
    op.drop_column("professional_services", "allow_online_booking")
