import uuid

from pydantic import BaseModel

from nexasalon_api.models.enums import AgendaAccessScope, PermissionEffect


class RoleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_system: bool
    # Etapa G — chaves de permission concedidas por ESTE role (o "padrão"
    # que a aba Permissões usa pra mostrar "herdado do perfil" antes de
    # qualquer override de membership). Vem de `role_permissions`, nunca
    # confundir com os overrides por membership (`PermissionOverrideRead`).
    permissions: list[str]


class PermissionRead(BaseModel):
    """Catálogo técnico — o frontend é responsável por traduzir `key`
    para linguagem humana (item explícito do pedido: nunca mostrar
    `agenda.edit` cru pro usuário final); `description` aqui é só um
    apoio técnico/interno, não o texto final da UI."""

    key: str
    module: str
    description: str | None


class PermissionOverrideRead(BaseModel):
    permission_key: str
    effect: PermissionEffect


class PermissionOverrideInput(BaseModel):
    permission_key: str
    effect: PermissionEffect


class SetPermissionOverridesRequest(BaseModel):
    """Substitui TODOS os overrides desta membership — semântica PUT
    (ver `services/user_management.py::set_permission_overrides`)."""

    overrides: list[PermissionOverrideInput]


class AgendaAccessGrantRead(BaseModel):
    professional_id: uuid.UUID
    can_view: bool
    can_edit: bool


class AgendaAccessRead(BaseModel):
    view_scope: AgendaAccessScope
    edit_scope: AgendaAccessScope
    grants: list[AgendaAccessGrantRead]


class SetAgendaAccessRequest(BaseModel):
    view_scope: AgendaAccessScope
    edit_scope: AgendaAccessScope
    # Só relevantes quando o respectivo scope é SELECTED — ignorados
    # (mas aceitos, pra não forçar o frontend a omitir o campo) quando
    # ALL. Validação de consistência (edit ⊆ view, ids existentes na
    # organização) acontece em `services/agenda_access.py::set_agenda_access`.
    viewable_professional_ids: list[uuid.UUID] = []
    editable_professional_ids: list[uuid.UUID] = []
