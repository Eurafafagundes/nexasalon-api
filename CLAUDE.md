# NexaSalon API — Contexto de Desenvolvimento (Backend)

## 1. Projeto

Este repositório (`nexasalon-api`) é o backend (FastAPI / Python / PostgreSQL /
Alembic) do NexaSalon, parte do MESMO sistema que o frontend `nexasalon`
(Next.js / React / TypeScript).

O documento mestre completo, com todas as regras que valem para os DOIS
repositórios (filosofia de desenvolvimento, multi-tenancy, UX, testes,
relatório final etc.), vive em `nexasalon/CLAUDE.md`
(`C:\Users\rafaf\nexasalon\CLAUDE.md`). Este arquivo existe para que uma
sessão iniciada diretamente aqui também tenha, de imediato, as regras mais
críticas de segurança/produção — nunca o substitui.

Sempre que uma alteração puder envolver contrato de API, banco de dados,
autenticação, permissões/RBAC, regras de negócio, cálculos financeiros,
estoque, comanda, agenda ou Dashboard: **analisar os dois repositórios**
antes de implementar. Não assumir que o problema pertence só ao backend.

---

## 2. Credenciais locais

As credenciais reais NÃO ficam neste arquivo versionado.

Claude Code está autorizado a utilizar credenciais presentes no ambiente
LOCAL de desenvolvimento (`.env`, `.env.local`, `AGENTS.local.md`,
`CLAUDE.local.md` — todos ignorados pelo Git) para iniciar aplicações,
testar conexão, executar queries de diagnóstico, rodar testes locais e
validar configuração.

Nunca colocar secrets em código, commits, testes, logs ou documentação
versionada. Se precisar indicar uma credencial num relatório, mascarar:
`postgresql://usuario:***@host/database`.

**O `.env` local deste repositório pode apontar para um banco DIFERENTE do
de produção** (ex.: um projeto Neon antigo/outra região) — nunca assumir
que a `NEXASALON_DATABASE_URL` local é a de produção sem confirmação
explícita. Produção roda em Render, conectado ao Neon Virginia
(`us-east-1`); a credencial correta para qualquer operação em produção é
a configurada no próprio ambiente do serviço em produção, nunca a local.

---

## 3. Git e produção — PROIBIDO sem autorização

NUNCA executar sem autorização explícita do usuário: `git push`, deploy,
migration no Neon/produção, alteração de secrets de produção,
alteração/exclusão de dados reais, comandos destrutivos, reset
destrutivo, rebase destrutivo, force push, drop, truncate, alteração
destrutiva de schema.

Pode autonomamente: `git status`/`git diff`/`git log`, criar branch
local, editar arquivos, rodar testes/lint/typecheck/build, criar
migrations localmente, preparar commits locais.

Antes de push/deploy/migration produção: PARAR. Mostrar o que será
enviado, hashes, migrations envolvidas, riscos, comando/operação
necessária. Esperar autorização.

---

## 4. Migrations

Nunca editar migration já existente/aplicada.

Nova alteração de schema:

1. descobrir HEAD real (`alembic heads`, nunca assumir pela
   documentação nem por migrations locais não commitadas — ex.:
   Comissão Condicional);
2. criar nova migration linear a partir do HEAD real;
3. revisar upgrade/downgrade/constraints/índices/RLS/dados existentes;
4. testar quando o ambiente permitir; se não puder, documentar.

Não aplicar automaticamente no Neon.

### 4.1 Coordenação obrigatória entre Migration e Deploy

Regra de segurança OBRIGATÓRIA para qualquer mudança que envolva schema
(nova coluna, nova tabela, nova constraint, novo índice, nova permissão
seedada por migration):

1. Identificar ANTECIPADAMENTE, ainda durante a implementação, se a
   mudança altera schema — nunca descobrir isso só depois do deploy.
2. Verificar a cadeia de migrations e o HEAD real publicado em
   `origin/main` antes de criar uma migration nova.
3. Confirmar explicitamente qual é o banco de produção correto antes de
   qualquer operação (Neon Virginia, `us-east-1`) — nunca assumir,
   nunca usar `.env`/credencial local de outro projeto/região para
   operação em produção.
4. Garantir um ponto de recuperação (backup/branch/PITR confirmado) no
   Neon antes de qualquer migration em produção.
5. Solicitar autorização explícita do usuário para QUALQUER operação em
   produção — migration, deploy, ou as duas.
6. Coordenar migration e deploy de modo que código incompatível com o
   schema NUNCA fique rodando em produção, mesmo que brevemente:
   - Migration aditiva e retrocompatível (nova coluna nullable/com
     default, nova tabela, novo índice, nova permissão): aplicar a
     migration ANTES do deploy do código que depende dela.
   - Migration que remove/renomeia algo que o código antigo ainda usa:
     deploy do código que para de usar o campo ANTES da migration que
     o remove.
   - Nunca fazer push do código dependente e deixar a migration "para
     depois" sem coordenação explícita — foi exatamente isso que
     causou o incidente de 24/11/2026 (ver precedente abaixo).
7. Migration NUNCA roda automaticamente no startup da aplicação —
   permanece operação administrativa explícita, mesmo quando aditiva e
   de baixo risco.
8. Após qualquer deploy/migration em produção, validar os ENDPOINTS
   REAIS afetados pela mudança (requisição/consulta funcional de
   verdade, contra dado real, inclusive casos antigos/históricos) —
   nunca considerar `healthz`/`readyz` suficientes; eles não exercitam
   o código que mudou.
9. Uma implementação NUNCA é considerada concluída em produção enquanto
   houver migration pendente de aplicação — "commitado e com push" não
   é "funcionando em produção".
10. A Comissão Condicional (migrations locais 0053/0054, ainda não
    commitadas) nunca pode ser aplicada/deployada como efeito colateral
    de nenhuma operação acima — sempre preservada isolada.

#### Precedente técnico: incidente das migrations 0058/0059 (24/11/2026)

A Central de Observações do Cliente foi publicada (push do backend
alterando `Appointment`/`AuditLog` e adicionando `client_observations`)
sem que as migrations 0058/0059 tivessem sido aplicadas em produção como
passo separado. O Render fez deploy automático do código novo a partir
do push em `main`; o ORM passou a declarar colunas (`notes_version`,
`notes_updated_at`, `notes_updated_by_name`) inexistentes no banco real
— toda consulta de `Appointment` (inclusive a Agenda) passou a falhar,
dando a impressão de que agendamentos históricos haviam desaparecido.
Nenhum dado foi perdido; o problema era puramente código-vs-schema fora
de sincronia.

Diagnóstico e correção (mesmo dia): confirmado via SSH no Render que o
commit deployado era o esperado e que `alembic_version` ainda apontava
para a revisão anterior (`0057`); criado um ponto de recuperação (branch
Neon a partir de `production`, com dado e schema) antes de qualquer
ação; aplicado `alembic upgrade 0059` diretamente no ambiente do Render
(mesma conexão administrativa já usada em runtime); validado `alembic
current`, as colunas/tabela/RLS/permissão novas, a contagem de
agendamentos antes/depois (sem perda), a serialização real de
agendamentos antigos e recentes, e a leitura da Central de Observações
— só então a Agenda foi confirmada restabelecida.

Lição fixada nas regras acima: migration e deploy do código dependente
precisam ser tratados como UMA operação coordenada, nunca dois passos
independentes que podem ficar dessincronizados.

---

## 5. Banco de dados

Runtime usa o role de aplicação (`NEXASALON_DATABASE_URL`);
migration/administração usa role administrativo/owner
(`NEXASALON_MIGRATIONS_DATABASE_URL`). Nunca usar o owner como conexão
normal da aplicação sem necessidade. Multi-tenant: toda query nova revisa
isolamento por `organization_id`/RLS; nunca aceitar `organization_id`
arbitrário vindo do frontend.

---

## 6. Qualidade

Antes de concluir, conforme disponível: `ruff`, `mypy`,
`py_compile`/import sanity, `pytest`. Não atribuir erros antigos à
implementação nova — distinguir erro novo de baseline pré-existente.

Para o restante das regras (filosofia de desenvolvimento, estoque,
comandas, financeiro, comissões, UX, formato do relatório final etc.),
ver `nexasalon/CLAUDE.md`.
