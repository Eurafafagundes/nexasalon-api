"""`ActorContext` — quem está fazendo a requisição, já resolvido.

Era definida em `dev_auth.py` (Etapa 2C, quando só existia o ator DEV).
Movida para cá porque agora é usada por AMBOS os caminhos (DEV e real);
`dev_auth.py` reexporta o nome por compatibilidade — nada que já importa
`ActorContext` de lá precisa mudar.

Campos novos nesta etapa (`role_name`, `permissions`, `professional_id`)
têm default para não quebrar código/testes existentes que construíam
`ActorContext` só com os 4 campos originais.
"""
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActorContext:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    membership_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str = ""
    # Recalculado do zero a cada request (Role.permissions ∪ overrides GRANT
    # − overrides DENY) — nunca lido de dentro do JWT. Ver services/auth.py.
    permissions: frozenset[str] = field(default_factory=frozenset)
    # None quando a membership não está ligada a nenhum Professional
    # (Professional.user_id é a FK canônica — ver models/identity.py).
    professional_id: uuid.UUID | None = None
    # Escopo granular de agenda (ver models/agenda_access.py). `None` =
    # SEM restrição adicional além de agenda.view_own/agenda.view_all
    # (equivalente ao AgendaAccessScope.ALL, que é o default de toda
    # membership) — só vira um `frozenset` concreto quando o escopo
    # correspondente está em SELECTED. Recalculado a cada request em
    # `api/deps.py`, nunca cacheado no token — mesma regra de
    # `permissions` acima.
    agenda_viewable_professional_ids: frozenset[uuid.UUID] | None = None
    agenda_editable_professional_ids: frozenset[uuid.UUID] | None = None
