"""
DEV ONLY — dependency de autenticação de desenvolvimento.
=============================================================

Autenticação real (login, sessão, JWT) ainda não foi implementada.
Enquanto isso, esta dependency fabrica um ator (organização + usuário +
membership) fixo e conhecido, só para permitir desenvolver/testar as
rotas que exigem contexto multi-tenant.

DUAS barreiras impedem isso de rodar em produção por acidente:
  1. `Settings` recusa iniciar se `environment=production` e
     `dev_auth_enabled=True` ao mesmo tempo (core/config.py).
  2. Esta função verifica de novo, em tempo de execução, e levanta
     `RuntimeError` se `environment == "production"` — mesmo que
     alguém chame `get_current_actor_DEV_ONLY` diretamente sem passar
     pelas Settings (ex.: import direto em outro contexto).

Nenhuma rota de produção deve depender desta função. Quando a
autenticação real existir, ela é substituída inteiramente — não
"desligada por flag" dentro da mesma função.
"""
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text

from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership, User
from nexasalon_api.models.organization import Organization
from nexasalon_api.models.rbac import Role

from .config import settings
from .db import SessionLocal

# UUIDs fixos de propósito — fáceis de reconhecer em logs/dumps de banco
# como dado de desenvolvimento, nunca um tenant real.
DEV_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
DEV_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
DEV_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
DEV_MEMBERSHIP_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f4")
DEV_ORG_SLUG = "dev-only-do-not-use-in-production"


@dataclass(frozen=True)
class ActorContext:
    """Contexto do ator autenticado — organização + usuário + membership.
    Em produção isso virá da sessão/JWT real; aqui vem fixo."""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    membership_id: uuid.UUID
    role_id: uuid.UUID


def _ensure_dev_seed() -> ActorContext:
    """Cria (se ainda não existir) a organização/usuário/role/membership
    de desenvolvimento. Idempotente — seguro chamar em toda request."""
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(DEV_ORGANIZATION_ID)},
        )

        if session.get(Organization, DEV_ORGANIZATION_ID) is None:
            session.add(
                Organization(
                    id=DEV_ORGANIZATION_ID,
                    name="[DEV ONLY] Organização de desenvolvimento",
                    slug=DEV_ORG_SLUG,
                )
            )
            session.flush()

        if session.get(User, DEV_USER_ID) is None:
            session.add(
                User(
                    id=DEV_USER_ID,
                    email="dev-only@nexasalon.local",
                    name="[DEV ONLY] Usuário de desenvolvimento",
                )
            )
            session.flush()

        if session.get(Role, DEV_ROLE_ID) is None:
            session.add(
                Role(
                    id=DEV_ROLE_ID,
                    organization_id=DEV_ORGANIZATION_ID,
                    name="Dev Owner",
                    is_system=False,
                )
            )
            session.flush()

        if session.get(OrganizationMembership, DEV_MEMBERSHIP_ID) is None:
            session.add(
                OrganizationMembership(
                    id=DEV_MEMBERSHIP_ID,
                    user_id=DEV_USER_ID,
                    organization_id=DEV_ORGANIZATION_ID,
                    role_id=DEV_ROLE_ID,
                    status=MembershipStatus.ACTIVE,
                )
            )
            session.flush()

        session.commit()

    return ActorContext(
        organization_id=DEV_ORGANIZATION_ID,
        user_id=DEV_USER_ID,
        membership_id=DEV_MEMBERSHIP_ID,
        role_id=DEV_ROLE_ID,
    )


def get_current_actor_DEV_ONLY() -> ActorContext:
    """DEV ONLY. Nunca usar como dependency de uma rota de produção."""
    if settings.environment == "production":
        raise RuntimeError(
            "get_current_actor_DEV_ONLY foi chamada com environment=production. "
            "Isso não deveria ser possível — bloqueando."
        )
    if not settings.dev_auth_enabled:
        raise HTTPException(
            status_code=500,
            detail=(
                "Autenticação real ainda não implementada e dev_auth_enabled=False. "
                "Defina NEXASALON_DEV_AUTH_ENABLED=true no ambiente de desenvolvimento."
            ),
        )
    return _ensure_dev_seed()
