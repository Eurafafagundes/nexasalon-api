import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError
from nexasalon_api.core.normalize import normalize_slug
from nexasalon_api.core.security import hash_password
from nexasalon_api.models.enums import MembershipStatus, OrganizationStatus
from nexasalon_api.repositories import (
    branch_repo,
    membership_repo,
    organization_repo,
    rbac_repo,
    user_repo,
)
from nexasalon_api.schemas.signup import SignupRequest
from nexasalon_api.services import auth as auth_service

TRIAL_DAYS = 14
TRIAL_PROFESSIONAL_LIMIT = 3
EMAIL_CONFLICT_MESSAGE = "Já existe uma conta com este e-mail."
CPF_CONFLICT_MESSAGE = "Este CPF já possui uma conta no NexaSalon."


@dataclass(frozen=True)
class SignupResult:
    tokens: auth_service.SessionTokens


def _public_slug(name: str) -> str:
    base = normalize_slug(name) or "espaco"
    return f"{base[:105]}-{uuid.uuid4().hex[:8]}"


def create_account(payload: SignupRequest) -> SignupResult:
    """Cria tenant, filial, owner e membership em uma transação única.

    IDs administrativos, role, status, datas e limites nunca vêm do
    payload público. O contexto RLS é fixado no UUID gerado pelo servidor.
    """
    organization_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    with SessionLocal() as session:
        try:
            session.execute(
                text("SELECT set_config('app.current_org_id', :oid, true)"),
                {"oid": str(organization_id)},
            )
            if user_repo.get_by_email(session, str(payload.email)) is not None:
                raise ConflictError(EMAIL_CONFLICT_MESSAGE)
            if user_repo.get_by_cpf(session, payload.cpf) is not None:
                raise ConflictError(CPF_CONFLICT_MESSAGE)

            owner_role = rbac_repo.get_system_role_by_name(session, "OWNER")
            if owner_role is None:
                raise RuntimeError("Role de sistema OWNER não encontrada.")

            organization = organization_repo.create(
                session,
                id=organization_id,
                name=payload.business_name,
                slug=_public_slug(payload.business_name),
                email=str(payload.email).lower(),
                phone=payload.business_phone,
                business_type=payload.business_type,
                city=payload.city,
                state=payload.state,
                status=OrganizationStatus.TRIAL,
                trial_started_at=now,
                trial_ends_at=now + timedelta(days=TRIAL_DAYS),
                professional_limit=TRIAL_PROFESSIONAL_LIMIT,
            )
            branch = branch_repo.create(
                session,
                organization.id,
                name="Matriz",
                slug="matriz",
                city=payload.city,
                state=payload.state.value,
                phone=payload.business_phone,
            )
            user = user_repo.create(
                session,
                email=str(payload.email),
                name=payload.full_name,
                cpf=payload.cpf,
                phone=payload.phone,
                password_hash=hash_password(payload.password),
            )
            membership = membership_repo.create(
                session,
                user_id=user.id,
                organization_id=organization.id,
                role_id=owner_role.id,
                branch_id=branch.id,
                status=MembershipStatus.ACTIVE,
            )
            user.last_login_at = now
            tokens = auth_service.issue_session_tokens(
                session,
                user_id=user.id,
                organization_id=organization.id,
                membership_id=membership.id,
            )
            session.commit()
            return SignupResult(tokens=tokens)
        except ConflictError:
            session.rollback()
            raise
        except IntegrityError as exc:
            session.rollback()
            constraint_name = getattr(
                getattr(exc.orig, "diag", None), "constraint_name", None
            )
            if constraint_name == "uq_users_cpf_not_null":
                raise ConflictError(CPF_CONFLICT_MESSAGE) from exc
            if constraint_name == "uq_users_email":
                raise ConflictError(EMAIL_CONFLICT_MESSAGE) from exc
            raise
        except Exception:
            session.rollback()
            raise
