"""Catálogo de roles/permissions da organização — usado pela tela
Configurações > Acessos para montar o seletor de "Perfil de acesso" e o
editor de permissões do perfil "Personalizado". Gate por `users.manage`,
mesma permission que já protege `api/v1/users.py` (é o mesmo domínio
administrativo)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.rbac import PermissionRead, RoleRead
from nexasalon_api.services import user_management as user_management_service

router = APIRouter(tags=["roles"])

_require_users_manage = require_permission("users.manage")


@router.get(
    "/roles",
    response_model=list[RoleRead],
    summary="Listar roles disponíveis (de sistema + customizadas) para esta organização",
)
def list_roles(
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> list[RoleRead]:
    roles = user_management_service.list_roles(session, actor.organization_id)
    return [
        RoleRead(id=r.id, name=r.name, description=r.description, is_system=r.is_system) for r in roles
    ]


@router.get(
    "/permissions",
    response_model=list[PermissionRead],
    summary="Catálogo global de permissions (chave técnica + módulo) — frontend traduz para linguagem humana",
)
def list_permissions(
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> list[PermissionRead]:
    permissions = user_management_service.list_permissions(session)
    return [
        PermissionRead(key=p.key, module=p.module, description=p.description) for p in permissions
    ]
