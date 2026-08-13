"""Configuração mínima de logging (Etapa 3C).

Sem framework de log estruturado — `logging.basicConfig` pro stdout já
é suficiente pra staging: o provedor de deploy (Render) coleta stdout
como log stream automaticamente, sem precisar escrever em arquivo nem
integrar nada além disso.

REGRA ABSOLUTA, vale pra qualquer `logger.*` chamado em qualquer lugar
do projeto: NUNCA logar senha, JWT (access ou refresh), o valor do
cookie de refresh, `invite_token`/`org_selection_token`, ou o header
`Authorization` inteiro. Onde for útil correlacionar uma falha com uma
sessão/usuário, logar `user_id`/`organization_id`/`request_id` — nunca
o segredo em si. `core/security.py` e `api/deps.py` não logam nenhum
desses valores hoje; ao adicionar um `logger.*` novo em qualquer rota,
reconferir esta regra antes.
"""
import logging
import sys

from .config import settings


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,  # idempotente: seguro chamar de novo (ex.: em testes)
    )
