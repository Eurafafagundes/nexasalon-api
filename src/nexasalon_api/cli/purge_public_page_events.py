"""CLI administrativo — retenção de eventos do NexaLink (LGPD).

Remove linhas de `public_page_events` mais antigas que
`settings.public_page_events_retention_days` (90 por padrão). Sem
scheduler automático nesta rodada (nenhuma infra de cron existe no
projeto ainda) — chamável manualmente, ou agendado depois por uma
tarefa cron/Render externa, sem precisar mudar este script.

Uso:

    NEXASALON_DATABASE_URL=<url-do-ambiente-alvo> \\
        python -m nexasalon_api.cli.purge_public_page_events [--days N]

`--days` sobrescreve `settings.public_page_events_retention_days` só
nesta execução — nunca grava um novo default em lugar nenhum."""
import argparse
import sys

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.services import public_page_events as public_page_events_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purga eventos antigos de public_page_events (NexaLink).")
    parser.add_argument(
        "--days", type=int, default=None, help="Sobrescreve a retenção padrão (settings.public_page_events_retention_days)."
    )
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        deleted = public_page_events_service.purge_events_older_than(session, retention_days=args.days)
        session.commit()

    print(f"public_page_events: {deleted} linha(s) removida(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
