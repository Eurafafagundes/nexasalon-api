"""Orquestração da Conta da Cliente (Etapa L, Blocos 5-11) — cadastro
manual, login, login Google, vínculo com `Client` por organização
(Bloco 8) e "Meus agendamentos" (Bloco 10). Nunca usa `User`/
`OrganizationMembership` (Bloco 7, item explícito do pedido) — RBAC
interno e identidade de cliente final são dois mundos separados."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from nexasalon_api.core.config import settings
from nexasalon_api.core.exceptions import (
    ConflictError,
    UnauthorizedError,
    ValidationDomainError,
)
from nexasalon_api.core.security import (
    create_customer_access_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from nexasalon_api.models.customer_account import CustomerAccount
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.models.organization import Organization
from nexasalon_api.repositories import (
    appointment_repo,
    client_repo,
    customer_account_repo,
    customer_refresh_token_repo,
    professional_repo,
    service_repo,
)
from nexasalon_api.schemas.customer_account import (
    CustomerRegisterRequest,
    PublicMyAppointmentRead,
)
from nexasalon_api.services.google_oauth import GoogleIdentity


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def register(session: Session, payload: CustomerRegisterRequest) -> CustomerAccount:
    """Bloco 5 — cadastro manual. E-mail único GLOBALMENTE (o mesmo
    e-mail não pode virar duas CustomerAccount distintas — a pessoa é
    uma só, mesmo que frequente vários salões, ver Bloco 7)."""
    email = _normalize_email(payload.email)
    if customer_account_repo.get_by_email(session, email) is not None:
        raise ConflictError("Já existe uma conta com este e-mail. Tente entrar em vez de criar uma nova conta.")
    account = customer_account_repo.create(
        session,
        name=payload.name.strip(),
        email=email,
        phone=payload.normalized_phone(),
        password_hash=hash_password(payload.password),
    )
    return account


def login(session: Session, *, email: str, password: str) -> CustomerAccount:
    """Bloco 9 — "respostas que não facilitem enumeração de contas": o
    MESMO erro genérico (`UnauthorizedError`, mesma mensagem) tanto pra
    e-mail inexistente quanto pra senha errada quanto pra conta só-Google
    sem senha própria — nenhum dos três casos é distinguível pela
    resposta HTTP."""
    account = customer_account_repo.get_by_email(session, _normalize_email(email))
    if account is None or account.password_hash is None or not account.is_active:
        raise UnauthorizedError("E-mail ou senha inválidos.")
    if not verify_password(password, account.password_hash):
        raise UnauthorizedError("E-mail ou senha inválidos.")
    return account


def login_or_register_with_google(session: Session, identity: GoogleIdentity) -> CustomerAccount:
    """Bloco 6 — depois de VERIFICAR a identidade (nunca antes, ver
    `services/google_oauth.py`): 1) já existe conta ligada a este
    `google_subject`? reusa. 2) já existe conta manual com o MESMO
    e-mail (verificado pelo Google)? liga o `google_subject` a ela — a
    pessoa não fica com duas contas por ter cadastrado manualmente antes
    e entrado via Google depois. 3) nenhuma das duas: cria conta nova,
    sem senha (`password_hash=None`, login futuro só via Google)."""
    existing = customer_account_repo.get_by_google_subject(session, identity.subject)
    if existing is not None:
        return existing

    email = _normalize_email(identity.email)
    by_email = customer_account_repo.get_by_email(session, email)
    if by_email is not None:
        if identity.email_verified:
            by_email.google_subject = identity.subject
            if by_email.email_verified_at is None:
                by_email.email_verified_at = datetime.now(timezone.utc)
            return customer_account_repo.save(session, by_email)
        # E-mail do Google não verificado E já existe conta com esse
        # e-mail: não liga automaticamente (evitaria um sequestro de
        # conta por um Google account com e-mail não confirmado) — cria
        # uma conta nova baseada só no `google_subject`, sem reaproveitar
        # o e-mail já cadastrado por outra pessoa/fluxo.
        raise ConflictError(
            "Já existe uma conta com este e-mail. Entre com e-mail e senha, ou verifique seu e-mail no Google."
        )

    return customer_account_repo.create(
        session,
        name=identity.name.strip() or email.split("@")[0],
        email=email,
        phone=None,
        password_hash=None,
        google_subject=identity.subject,
        email_verified_at=datetime.now(timezone.utc) if identity.email_verified else None,
    )


def update_phone(session: Session, account: CustomerAccount, phone: str) -> CustomerAccount:
    account.phone = phone
    return customer_account_repo.save(session, account)


# ---------------------------------------------------------------------
# Sessão da cliente (ajuste pós-Etapa L: persistência segura, access
# token curto + refresh token opaco rotacionado com cookie HttpOnly).
# Espelha `services/auth.py` (funcionário) na TÉCNICA, sem misturar
# identidade/tabela — ver docstring de
# `models/customer_account.py::CustomerRefreshToken`.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerSessionTokens:
    access_token: str
    refresh_token: str
    customer_account_id: uuid.UUID


def issue_session(session: Session, customer_account_id: uuid.UUID) -> CustomerSessionTokens:
    """Emite um novo par access+refresh — chamado depois de
    register/login/google (Blocos 5/6/9) bem-sucedidos. Mesma técnica de
    `services/auth.py::_issue_session_tokens`, tabela separada."""
    access_token = create_customer_access_token(customer_account_id=customer_account_id)
    raw_refresh = generate_opaque_token()
    now = datetime.now(timezone.utc)
    customer_refresh_token_repo.create(
        session,
        customer_account_id=customer_account_id,
        token_hash=hash_opaque_token(raw_refresh),
        issued_at=now,
        expires_at=now + timedelta(days=settings.customer_refresh_token_ttl_days),
    )
    return CustomerSessionTokens(
        access_token=access_token, refresh_token=raw_refresh, customer_account_id=customer_account_id
    )


def refresh_session(session: Session, raw_refresh_token: str) -> CustomerSessionTokens:
    """Rotação com detecção de reuso — mesma lógica de
    `services/auth.py::refresh` (funcionário): cada uso consome (revoga)
    o token apresentado e emite um novo. Reapresentar um token JÁ
    revogado é tratado como possível vazamento/roubo e revoga TODAS as
    sessões ativas da conta — a próxima tentativa de refresh de
    qualquer sessão antiga (inclusive de outro dispositivo) também
    falha, forçando novo login em todos.

    `session` aqui vem de `api/deps.py::get_customer_db` (commit
    automático só se a função retornar sem levantar) — por isso, no
    ramo de detecção de reuso, commitamos explicitamente ANTES de
    levantar o erro: a revogação em massa precisa persistir mesmo que a
    requisição termine em 401 (senão o rollback automático do `except`
    do dependency desfaria a própria revogação de segurança)."""
    token_hash = hash_opaque_token(raw_refresh_token)
    now = datetime.now(timezone.utc)

    token = customer_refresh_token_repo.get_by_hash(session, token_hash)
    if token is None:
        raise UnauthorizedError("Sessão inválida.")

    if token.revoked_at is not None:
        revoked_count = customer_refresh_token_repo.revoke_all_for_account(
            session, token.customer_account_id, now
        )
        session.commit()
        raise UnauthorizedError(
            f"Sessão já utilizada. {revoked_count} sessão(ões) revogada(s) por segurança."
        )

    if token.expires_at <= now:
        raise UnauthorizedError("Sessão expirada.")

    customer_account_id = token.customer_account_id
    account = customer_account_repo.get(session, customer_account_id)
    if account is None or not account.is_active:
        raise UnauthorizedError("Conta inválida ou inativa.")

    new_raw_refresh = generate_opaque_token()
    new_token = customer_refresh_token_repo.create(
        session,
        customer_account_id=customer_account_id,
        token_hash=hash_opaque_token(new_raw_refresh),
        issued_at=now,
        expires_at=now + timedelta(days=settings.customer_refresh_token_ttl_days),
    )
    customer_refresh_token_repo.mark_rotated(session, token, new_token, now)

    access_token = create_customer_access_token(customer_account_id=customer_account_id)
    return CustomerSessionTokens(
        access_token=access_token, refresh_token=new_raw_refresh, customer_account_id=customer_account_id
    )


def revoke_session(session: Session, raw_refresh_token: str) -> None:
    """Logout — idempotente e silencioso (mesma filosofia de
    `services/auth.py::logout`): token inexistente/já revogado não
    levanta erro (evita confirmar pra quem não tem o token se ele é
    válido ou não). Revoga só a sessão apresentada (este dispositivo),
    nunca as outras — suporta múltiplos dispositivos/sessões
    independentes por conta."""
    token_hash = hash_opaque_token(raw_refresh_token)
    now = datetime.now(timezone.utc)
    token = customer_refresh_token_repo.get_by_hash(session, token_hash)
    if token is not None and token.revoked_at is None:
        customer_refresh_token_repo.revoke(session, token, now)


# ---------------------------------------------------------------------
# Bloco 8 — vinculação com cadastro já existente ("Amanda pode fazer 20
# agendamentos e continuar com uma única ficha").
# ---------------------------------------------------------------------


def resolve_client_for_customer_account(
    session: Session, organization: Organization, account: CustomerAccount
) -> uuid.UUID:
    """Passo a passo EXATO do Bloco 8:

    1. já existe vínculo desta conta com esta organização? reusa o
       MESMO `client_id` sempre — nunca cria um `Client` novo pra quem
       já tem ficha.
    2. sem vínculo ainda: procura `Client` existente pelo telefone
       normalizado dentro da organização (mesma heurística já usada por
       `_get_or_create_public_client` no fluxo anônimo anterior — uma
       cliente que já foi atendida presencialmente e agora está criando
       conta pela primeira vez encontra a própria ficha em vez de
       ganhar uma duplicata).
    3. nem vínculo nem Client por telefone: cria um `Client` novo, só
       com nome/telefone/e-mail (nunca sobrescreve dado sensível
       existente — este é o caminho de CRIAÇÃO, não de atualização).
    4. grava o vínculo — a partir daqui, toda reserva futura desta
       conta nesta organização cai direto no passo 1."""
    link = customer_account_repo.get_link(session, account.id, organization.id)
    if link is not None:
        return link.client_id

    if not account.phone:
        # Ainda não pediu WhatsApp (caminho comum: primeiro login via
        # Google, que não fornece telefone) — sem telefone não dá pra
        # nem tentar casar com um Client existente (passo 4 do Bloco 8)
        # nem preencher o cadastro novo direito. Bloqueia ANTES de criar
        # qualquer coisa; o frontend trata isto pedindo o WhatsApp
        # (`PATCH /customer-auth/me`) antes de deixar confirmar.
        raise ValidationDomainError(
            "Informe seu WhatsApp antes de confirmar o agendamento."
        )

    client_id: uuid.UUID
    existing_client = client_repo.get_by_phone(session, organization.id, account.phone) if account.phone else None
    if existing_client is not None:
        client_id = existing_client.id
    else:
        client = client_repo.create(
            session, organization.id, name=account.name, phone=account.phone, email=account.email
        )
        client_id = client.id

    customer_account_repo.create_link(
        session, customer_account_id=account.id, organization_id=organization.id, client_id=client_id
    )
    return client_id


# ---------------------------------------------------------------------
# Bloco 11 — anti-fake: teto de agendamentos futuros ATIVOS por conta.
# ---------------------------------------------------------------------


def assert_can_book_more(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID, *, limit: int) -> None:
    now = datetime.now(timezone.utc)
    active = appointment_repo.list_active_for_client(session, organization_id, client_id)
    future_active = [
        a
        for a in active
        if a.starts_at is not None and a.starts_at >= now and a.status != AppointmentStatus.CANCELLED
    ]
    if len(future_active) >= limit:
        raise ValidationDomainError(
            "Você já tem o número máximo de agendamentos futuros ativos permitido. "
            "Cancele ou aguarde um deles para reservar um novo horário."
        )


# ---------------------------------------------------------------------
# Bloco 10 — "Meus agendamentos".
# ---------------------------------------------------------------------


def list_my_appointments(
    session: Session, organization: Organization, account: CustomerAccount
) -> list[PublicMyAppointmentRead]:
    """Só os agendamentos FUTUROS (item explícito: "próximos
    agendamentos"), da MESMA organização do link público que a cliente
    está usando — nunca de outra CustomerAccount (garantido por
    construção: `client_id` vem só do vínculo desta conta, nunca de
    input externo) nem de outra organização (RLS + `link.organization_id`
    já resolvido pelo contexto da rota)."""
    link = customer_account_repo.get_link(session, account.id, organization.id)
    if link is None:
        return []

    now = datetime.now(timezone.utc)
    appointments = appointment_repo.list_active_for_client(session, organization.id, link.client_id)
    upcoming = sorted(
        (a for a in appointments if a.starts_at is not None and a.starts_at >= now),
        key=lambda a: a.starts_at,
    )

    rows: list[PublicMyAppointmentRead] = []
    for appointment in upcoming:
        for item in appointment.items:
            service = service_repo.get(session, organization.id, item.service_id)
            professional = professional_repo.get(session, organization.id, item.professional_id)
            rows.append(
                PublicMyAppointmentRead(
                    id=appointment.id,
                    organization_name=organization.name,
                    service_name=service.name if service is not None else "Serviço",
                    professional_name=professional.name if professional is not None else "Profissional",
                    starts_at=item.start_at,
                    ends_at=item.end_at,
                    status=appointment.status,
                )
            )
    return rows
