"""CRUD administrativo de usuários/memberships de UMA organização — toda
rota exige a permission `users.manage` (RBAC real, não "esconder botão no
frontend"). `organization_id` sempre vem de `actor.organization_id`
(contexto autenticado), nunca de parâmetro de URL/body."""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership
from nexasalon_api.repositories import professional_repo
from nexasalon_api.schemas.rbac import (
    AgendaAccessRead,
    PermissionOverrideRead,
    SetAgendaAccessRequest,
    SetPermissionOverridesRequest,
)
from nexasalon_api.schemas.user_management import (
    AssignRoleRequest,
    EmployeeInviteRequest,
    EmployeeInviteResponse,
    LinkProfessionalRequest,
    MembershipRead,
    ResendInviteResponse,
    ResetPasswordLinkResponse,
    SetPasswordRequest,
)
from nexasalon_api.services import agenda_access as agenda_access_service
from nexasalon_api.services import user_management as user_management_service

router = APIRouter(prefix="/users", tags=["users"])

_require_users_manage = require_permission("users.manage")


def _to_membership_read(session: Session, membership: OrganizationMembership) -> MembershipRead:
    professional = professional_repo.get_by_user(session, membership.organization_id, membership.user_id)
    return MembershipRead(
        id=membership.id,
        user_id=membership.user_id,
        user_email=membership.user.email,
        user_name=membership.user.name,
        organization_id=membership.organization_id,
        role_id=membership.role_id,
        role_name=membership.role.name,
        branch_id=membership.branch_id,
        status=membership.status,
        professional_id=professional.id if professional is not None else None,
        last_login_at=membership.user.last_login_at,
        created_at=membership.created_at,
        updated_at=membership.updated_at,
    )


@router.get("", response_model=list[MembershipRead], summary="Listar funcionários (memberships) da organização")
def list_employees(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> list[MembershipRead]:
    memberships = user_management_service.list_employees(session, actor.organization_id, include_inactive)
    return [_to_membership_read(session, m) for m in memberships]


@router.post(
    "",
    response_model=EmployeeInviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Adicionar/convidar funcionário (novo usuário ou vincular um já existente por e-mail)",
)
def add_employee(
    payload: EmployeeInviteRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> EmployeeInviteResponse:
    result = user_management_service.add_or_invite_employee(
        session,
        actor.organization_id,
        email=payload.email,
        name=payload.name,
        role_id=payload.role_id,
        branch_id=payload.branch_id,
        password=payload.password,
        actor_user_id=actor.user_id,
    )
    return EmployeeInviteResponse(
        membership=_to_membership_read(session, result.membership),
        invite_token=result.invite_token,
        credential_mode=result.credential_mode,
    )


@router.post(
    "/{membership_id}/resend-invite",
    response_model=ResendInviteResponse,
    summary="Gerar um novo link de convite para uma membership ainda pendente",
)
def resend_invite(
    membership_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> ResendInviteResponse:
    invite_token = user_management_service.resend_invite(session, actor.organization_id, membership_id)
    return ResendInviteResponse(invite_token=invite_token)


@router.patch(
    "/{membership_id}/activate", response_model=MembershipRead, summary="Ativar membership"
)
def activate_membership(
    membership_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> MembershipRead:
    membership = user_management_service.set_membership_status(
        session, actor.organization_id, membership_id, MembershipStatus.ACTIVE
    )
    return _to_membership_read(session, membership)


@router.patch(
    "/{membership_id}/deactivate", response_model=MembershipRead, summary="Desativar membership"
)
def deactivate_membership(
    membership_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> MembershipRead:
    membership = user_management_service.set_membership_status(
        session, actor.organization_id, membership_id, MembershipStatus.SUSPENDED
    )
    return _to_membership_read(session, membership)


@router.put(
    "/{membership_id}/role", response_model=MembershipRead, summary="Trocar o role da membership"
)
def assign_role(
    membership_id: uuid.UUID,
    payload: AssignRoleRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> MembershipRead:
    membership = user_management_service.assign_role(
        session, actor.organization_id, membership_id, payload.role_id
    )
    return _to_membership_read(session, membership)


@router.put(
    "/{membership_id}/professional",
    response_model=MembershipRead,
    summary="Vincular a membership a um Professional existente na mesma organização",
)
def link_professional(
    membership_id: uuid.UUID,
    payload: LinkProfessionalRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> MembershipRead:
    membership = user_management_service.link_professional(
        session, actor.organization_id, membership_id, payload.professional_id
    )
    return _to_membership_read(session, membership)


@router.post(
    "/{membership_id}/reset-password",
    response_model=ResetPasswordLinkResponse,
    summary="Gerar link de redefinição de senha (administrador nunca vê/define a senha)",
)
def reset_password(
    membership_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> ResetPasswordLinkResponse:
    reset_token = user_management_service.admin_reset_password(session, actor.organization_id, membership_id)
    return ResetPasswordLinkResponse(reset_token=reset_token)


@router.patch(
    "/{membership_id}/set-password",
    response_model=MembershipRead,
    summary="Definir a senha do funcionário diretamente (Etapa G — caminho principal, substitui o link)",
)
def set_password(
    membership_id: uuid.UUID,
    payload: SetPasswordRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> MembershipRead:
    membership = user_management_service.admin_set_password(
        session, actor.organization_id, membership_id, actor.user_id, payload.password
    )
    return _to_membership_read(session, membership)


@router.get(
    "/{membership_id}/permission-overrides",
    response_model=list[PermissionOverrideRead],
    summary="Listar overrides de permissão (perfil Personalizado) desta membership",
)
def get_permission_overrides(
    membership_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> list[PermissionOverrideRead]:
    overrides = user_management_service.get_permission_overrides(session, actor.organization_id, membership_id)
    return [
        PermissionOverrideRead(permission_key=o.permission_key, effect=o.effect) for o in overrides
    ]


@router.put(
    "/{membership_id}/permission-overrides",
    response_model=list[PermissionOverrideRead],
    summary="Substituir (PUT) todos os overrides de permissão desta membership",
)
def set_permission_overrides(
    membership_id: uuid.UUID,
    payload: SetPermissionOverridesRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> list[PermissionOverrideRead]:
    overrides = user_management_service.set_permission_overrides(
        session,
        actor.organization_id,
        membership_id,
        actor.user_id,
        [(o.permission_key, o.effect) for o in payload.overrides],
    )
    return [
        PermissionOverrideRead(permission_key=o.permission_key, effect=o.effect) for o in overrides
    ]


@router.get(
    "/{membership_id}/agenda-access",
    response_model=AgendaAccessRead,
    summary="Ver o escopo de visualização/edição de agenda desta membership",
)
def get_agenda_access(
    membership_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> AgendaAccessRead:
    summary = agenda_access_service.get_agenda_access(session, actor.organization_id, membership_id)
    return AgendaAccessRead(
        view_scope=summary.view_scope,
        edit_scope=summary.edit_scope,
        grants=[
            {"professional_id": g.professional_id, "can_view": g.can_view, "can_edit": g.can_edit}
            for g in summary.grants
        ],
    )


@router.put(
    "/{membership_id}/agenda-access",
    response_model=AgendaAccessRead,
    summary="Definir o escopo de visualização/edição de agenda desta membership",
)
def set_agenda_access(
    membership_id: uuid.UUID,
    payload: SetAgendaAccessRequest,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_users_manage),
) -> AgendaAccessRead:
    summary = agenda_access_service.set_agenda_access(
        session,
        actor.organization_id,
        membership_id,
        view_scope=payload.view_scope,
        edit_scope=payload.edit_scope,
        viewable_professional_ids=payload.viewable_professional_ids,
        editable_professional_ids=payload.editable_professional_ids,
    )
    return AgendaAccessRead(
        view_scope=summary.view_scope,
        edit_scope=summary.edit_scope,
        grants=[
            {"professional_id": g.professional_id, "can_view": g.can_view, "can_edit": g.can_edit}
            for g in summary.grants
        ],
    )
