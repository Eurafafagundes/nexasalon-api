"""Escopo granular de agenda por membership (ver `models/agenda_access.py`).

Duas responsabilidades distintas neste módulo:

1. `resolve_viewable_ids`/`resolve_editable_ids` — chamadas em TODA
   requisição autenticada (`api/deps.py`), pra popular
   `ActorContext.agenda_viewable_professional_ids`/
   `agenda_editable_professional_ids`. Retornam `None` quando o escopo é
   ALL (sem restrição adicional — o resto do sistema continua decidindo
   por `agenda.view_own`/`agenda.view_all`) ou um `frozenset[UUID]`
   quando é SELECTED.

2. `get_agenda_access`/`set_agenda_access` — usadas pela tela
   Configurações > Acessos (`api/v1/users.py`) pra ler/gravar a
   configuração de UMA membership. `set_agenda_access` é quem valida a
   regra "editar exige visualizar" no nível de APLICAÇÃO (mensagem de
   erro clara) — o CHECK do banco (migration 0019) é a segunda barreira,
   nunca a única.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import AgendaAccessScope
from nexasalon_api.models.identity import OrganizationMembership
from nexasalon_api.repositories import agenda_access_repo, membership_repo, professional_repo

VIEW_ALL_PERMISSION = "agenda.view_all"
VIEW_OWN_PERMISSION = "agenda.view_own"


def can_view_professional(actor: ActorContext, professional_id: uuid.UUID) -> bool:
    """Verdade única de "este ator pode ver a agenda DESTE profissional
    específico" — usada tanto por leitura de UM appointment
    (`services/appointments.py::get_appointment`) quanto, indiretamente,
    pela versão em lote em `services/agenda.py::list_agenda` (que
    resolve o mesmo `actor.agenda_viewable_professional_ids` para uma
    listagem em vez de um único id)."""
    if actor.agenda_viewable_professional_ids is not None:
        return professional_id in actor.agenda_viewable_professional_ids
    if VIEW_ALL_PERMISSION in actor.permissions:
        return True
    return VIEW_OWN_PERMISSION in actor.permissions and actor.professional_id == professional_id


def can_edit_professional(actor: ActorContext, professional_id: uuid.UUID) -> bool:
    """"Editar" é sempre um subconjunto de "visualizar" (item explícito
    do pedido). Quando o escopo de EDIÇÃO é ALL (`agenda_editable_
    professional_ids is None` — o default de toda membership, preserva
    o comportamento anterior a esta feature), a regra é IDÊNTICA à de
    visualização: quem pode ver, pode editar, exatamente como o sistema
    já se comportava antes de existir este escopo granular. Só quando o
    escopo é SELECTED a lista explícita de edição passa a valer, e
    mesmo assim é sempre um subconjunto do que `set_agenda_access` já
    valida na escrita."""
    if actor.agenda_editable_professional_ids is not None:
        return professional_id in actor.agenda_editable_professional_ids
    return can_view_professional(actor, professional_id)


def resolve_viewable_ids(session: Session, membership: OrganizationMembership) -> frozenset[uuid.UUID] | None:
    if membership.agenda_view_scope == AgendaAccessScope.ALL:
        return None
    grants = agenda_access_repo.list_for_membership(session, membership.id)
    return frozenset(g.professional_id for g in grants if g.can_view)


def resolve_editable_ids(session: Session, membership: OrganizationMembership) -> frozenset[uuid.UUID] | None:
    if membership.agenda_edit_scope == AgendaAccessScope.ALL:
        return None
    grants = agenda_access_repo.list_for_membership(session, membership.id)
    return frozenset(g.professional_id for g in grants if g.can_edit)


@dataclass(frozen=True)
class AgendaAccessGrant:
    professional_id: uuid.UUID
    can_view: bool
    can_edit: bool


@dataclass(frozen=True)
class AgendaAccessSummary:
    view_scope: AgendaAccessScope
    edit_scope: AgendaAccessScope
    grants: list[AgendaAccessGrant]


def _get_membership_in_org(
    session: Session, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> OrganizationMembership:
    membership = membership_repo.get(session, membership_id)
    if membership is None or membership.organization_id != organization_id:
        raise NotFoundError("Membership não encontrada.")
    return membership


def get_agenda_access(
    session: Session, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> AgendaAccessSummary:
    membership = _get_membership_in_org(session, organization_id, membership_id)
    grants = agenda_access_repo.list_for_membership(session, membership_id)
    return AgendaAccessSummary(
        view_scope=membership.agenda_view_scope,
        edit_scope=membership.agenda_edit_scope,
        grants=[AgendaAccessGrant(g.professional_id, g.can_view, g.can_edit) for g in grants],
    )


def set_agenda_access(
    session: Session,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    *,
    view_scope: AgendaAccessScope,
    edit_scope: AgendaAccessScope,
    viewable_professional_ids: list[uuid.UUID],
    editable_professional_ids: list[uuid.UUID],
) -> AgendaAccessSummary:
    """Substitui a configuração inteira desta membership. Regras
    validadas aqui (além do que o banco já garante por CHECK/FK):

    - `edit_scope == ALL` só é aceito se `view_scope == ALL` também —
      não faz sentido "editar todo mundo" sem "ver todo mundo" (item
      explícito: editar é sempre um subconjunto de visualizar).
    - Quando `edit_scope == SELECTED`, todo profissional em
      `editable_professional_ids` precisa também constar em
      `viewable_professional_ids` (se `view_scope` também for SELECTED)
      — mesma regra, aplicada linha a linha.
    - Todo `professional_id` referenciado precisa existir NESTA
      organização (defesa em profundidade — RLS/FK já impediriam
      referenciar outra organização, mas a mensagem de erro fica clara
      em vez de um 500 de FK).
    """
    membership = _get_membership_in_org(session, organization_id, membership_id)

    if edit_scope == AgendaAccessScope.ALL and view_scope != AgendaAccessScope.ALL:
        raise ValidationDomainError(
            "Não é possível permitir editar TODAS as agendas sem também permitir visualizar todas."
        )

    view_ids = set(viewable_professional_ids)
    edit_ids = set(editable_professional_ids)

    if edit_scope == AgendaAccessScope.SELECTED and view_scope == AgendaAccessScope.SELECTED:
        missing = edit_ids - view_ids
        if missing:
            raise ValidationDomainError(
                "Todo profissional com permissão de EDIÇÃO precisa também ter permissão de VISUALIZAÇÃO."
            )

    all_referenced_ids = view_ids | edit_ids
    if all_referenced_ids:
        existing = {
            p.id for p in professional_repo.list_by_ids(session, organization_id, list(all_referenced_ids))
        }
        unknown = all_referenced_ids - existing
        if unknown:
            raise ValidationDomainError("Um ou mais profissionais informados não existem nesta organização.")

    membership.agenda_view_scope = view_scope
    membership.agenda_edit_scope = edit_scope
    membership_repo.save(session, membership)

    rows: list[tuple[uuid.UUID, bool, bool]] = []
    if view_scope == AgendaAccessScope.SELECTED or edit_scope == AgendaAccessScope.SELECTED:
        for professional_id in view_ids | edit_ids:
            can_view = professional_id in view_ids
            can_edit = professional_id in edit_ids
            if can_view or can_edit:
                rows.append((professional_id, can_view, can_edit))

    agenda_access_repo.replace_for_membership(session, organization_id, membership_id, rows)

    return get_agenda_access(session, organization_id, membership_id)
