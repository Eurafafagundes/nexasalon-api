"""
Primitivas de segurança: hash de senha, JWT de access token, tokens
opacos (refresh). Nada de lógica de negócio aqui — só criptografia.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHash

from .config import settings

# ---------------------------------------------------------------------
# Senha — Argon2id (recomendação atual da OWASP; memory-hard, resistente
# a cracking em GPU). Senha NUNCA é armazenada nem logada em texto puro,
# só o hash.
# ---------------------------------------------------------------------
_password_hasher = PasswordHasher()


def hash_password(plain_password: str) -> str:
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        _password_hasher.verify(password_hash, plain_password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


# ---------------------------------------------------------------------
# Access token — JWT de vida curta. Claims mínimos de propósito: NUNCA
# carrega senha, role ou lista de permissions — essas são sempre
# recalculadas a partir do banco a cada request (ver api/deps.py), pra
# que desativar uma membership corte o acesso na hora, não só quando o
# token expirar.
# ---------------------------------------------------------------------
class TokenType(str, Enum):
    ACCESS = "access"
    ORG_SELECTION = "org_selection"
    INVITE = "invite"
    PASSWORD_RESET = "password_reset"


def create_access_token(*, user_id: uuid.UUID, organization_id: uuid.UUID, membership_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "org_id": str(organization_id),
        "membership_id": str(membership_id),
        "type": TokenType.ACCESS.value,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_org_selection_token(*, user_id: uuid.UUID) -> str:
    """Token de vida bem curta emitido quando o usuário tem mais de uma
    membership ativa — só serve pra chamar /auth/select-organization."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": TokenType.ORG_SELECTION.value,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.org_selection_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_invite_token(*, user_id: uuid.UUID, membership_id: uuid.UUID) -> str:
    """Token de convite — permite ao próprio funcionário definir a
    própria senha e ativar UMA membership específica (a referenciada
    aqui). Não é um token de sessão: `POST /auth/accept-invite` é a
    única rota que aceita `type=invite`. JWT normal (não opaco) porque é
    de uso único por natureza — aceitar o convite já muda o estado da
    membership pra ACTIVE, então uma segunda tentativa com o mesmo token
    falha na checagem de estado (`status != INVITED`), não precisa de
    revogação própria como o refresh token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "membership_id": str(membership_id),
        "type": TokenType.INVITE.value,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(days=settings.invite_token_ttl_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_password_reset_token(*, user_id: uuid.UUID, membership_id: uuid.UUID) -> str:
    """Redefinição de senha ACIONADA POR ADMIN pra uma membership já
    ACTIVE (`resend_invite`/`accept_invite` só funcionam pra INVITED —
    ver services/user_management.py::admin_reset_password). Mesma
    filosofia do convite: o administrador nunca define nem vê a nova
    senha, só recebe este link/token pra repassar ao funcionário. Vida
    curta (mesma janela do convite) porque, diferente do invite token, o
    estado da membership não muda ao gerar este token (continua ACTIVE)
    — não há "já foi usado" pra invalidar um token velho sozinho, então
    a expiração curta é a única barreira contra um link antigo esquecido
    em algum lugar."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "membership_id": str(membership_id),
        "type": TokenType.PASSWORD_RESET.value,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(days=settings.invite_token_ttl_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


class InvalidTokenError(Exception):
    pass


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc


# ---------------------------------------------------------------------
# Refresh token — opaco (não é JWT), alta entropia, guardado no banco só
# como hash (SHA-256 é suficiente aqui — o token já tem entropia alta
# por construção, isso não é uma senha de usuário). Cada uso gera um
# novo token e revoga o anterior (rotação) — ver services/auth.py.
# ---------------------------------------------------------------------
def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
