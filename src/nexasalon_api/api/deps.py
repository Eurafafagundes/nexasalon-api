"""Dependencies compartilhadas das rotas.

`get_current_actor` é hoje um alias direto de `get_current_actor_DEV_ONLY`
(core/dev_auth.py) — de propósito: é o ÚNICO lugar que vai mudar quando
autenticação real existir. Nenhuma rota deve importar `dev_auth` direto.
"""
from collections.abc import Generator

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from nexasalon_api.core.dev_auth import ActorContext, get_current_actor_DEV_ONLY
from nexasalon_api.core.db import SessionLocal

# Seam único de troca para autenticação real (Etapa futura): troque esta
# linha por uma dependency real de sessão/JWT — nenhuma rota precisa mudar.
get_current_actor = get_current_actor_DEV_ONLY


def get_db(actor: ActorContext = Depends(get_current_actor)) -> Generator[Session, None, None]:
    """Sessão por request. Seta `app.current_org_id` via `SET LOCAL` —
    escopado à transação da request, nunca vaza pra outra request que
    reuse a mesma conexão do pool. RLS é a segunda barreira: toda query
    de repository também filtra `organization_id` explicitamente."""
    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(actor.organization_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
