import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nexasalon_api.models.client import Client

_DIGITS = re.compile(r"\D+")


def get(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> Client | None:
    stmt = select(Client).where(Client.id == client_id, Client.organization_id == organization_id)
    return session.scalars(stmt).first()


def get_by_cpf(session: Session, organization_id: uuid.UUID, cpf: str) -> Client | None:
    """`cpf` já deve vir NORMALIZADO (só dígitos — ver
    `schemas/client.py::ClientBase._normalize_and_validate_cpf`).
    Considera clientes ativos E inativos (item "analise a unicidade" —
    um CPF já usado por um cadastro desativado ainda é um conflito de
    identidade real, não deveria poder ser reaproveitado silenciosamente
    por um cadastro novo)."""
    stmt = select(Client).where(Client.organization_id == organization_id, Client.cpf == cpf)
    return session.scalars(stmt).first()


def get_by_phone(session: Session, organization_id: uuid.UUID, phone: str) -> Client | None:
    """`phone` já deve vir NORMALIZADO (só dígitos — ver
    `core/normalize.py::normalize_phone`) — usado pelo Agendamento
    Online público (Etapa K) pra achar a cliente existente pelo
    telefone/WhatsApp DENTRO DA MESMA organização antes de criar um
    cadastro novo (item explícito do pedido "procurar cliente existente
    pelo telefone dentro da organização"). Considera ativos E inativos,
    mesmo raciocínio de `get_by_cpf` — um telefone de um cadastro
    desativado ainda identifica a mesma pessoa."""
    stmt = select(Client).where(Client.organization_id == organization_id, Client.phone == phone)
    return session.scalars(stmt).first()


def list_all(
    session: Session,
    organization_id: uuid.UUID,
    include_inactive: bool = False,
    search: str | None = None,
) -> list[Client]:
    stmt = select(Client).where(Client.organization_id == organization_id)
    if not include_inactive:
        stmt = stmt.where(Client.is_active.is_(True))
    if search:
        # Campo único (item "não quero seletor Nome/CPF/Telefone"): o
        # texto digitado é comparado ao nome (ilike, texto livre) e,
        # quando contém algum dígito, também ao telefone/CPF já
        # NORMALIZADOS no banco (só dígitos) — "11 99999-0000",
        # "11999990000" e "111.444.777-35" encontram o cliente sem
        # precisar de nenhum modo de busca separado.
        conditions = [Client.name.ilike(f"%{search}%")]
        digits = _DIGITS.sub("", search)
        if digits:
            digit_pattern = f"%{digits}%"
            conditions.append(Client.phone.ilike(digit_pattern))
            conditions.append(Client.cpf.ilike(digit_pattern))
        stmt = stmt.where(or_(*conditions))
    return list(session.scalars(stmt.order_by(Client.name)).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> Client:
    client = Client(organization_id=organization_id, **fields)
    session.add(client)
    session.flush()
    return client


def save(session: Session, client: Client) -> Client:
    session.flush()
    return client
