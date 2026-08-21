"""CLI administrativo de bootstrap (Etapa 3C).

Cria a primeira Organization, Branch, User e OrganizationMembership
OWNER de uma instalação nova (ex.: staging recém-provisionado, ainda
sem nenhum dado). É a ÚNICA forma de criar o primeiro usuário: de
propósito NÃO existe (e não deve existir) um endpoint HTTP tipo `POST
/create-admin` — isso seria uma rota de escalada de privilégio exposta
publicamente, sem nenhuma autenticação prévia possível (é literalmente
o primeiro usuário do sistema).

Uso:

    NEXASALON_DATABASE_URL=<url-do-ambiente-alvo> \\
        python -m nexasalon_api.cli.bootstrap_owner

Todos os dados — inclusive a senha — são pedidos interativamente,
nunca por argumento de linha de comando (ficaria no histórico do shell
e visível em `ps aux` de qualquer outro processo na mesma máquina) e
nunca com valor default. A senha é lida com `getpass` (não ecoa no
terminal) e recebe o mesmo hash Argon2id (`core/security.hash_password`)
de qualquer outro usuário — não existe usuário "especial" nesse
sentido, e a senha não fica sob controle deste script depois que
`hash_password` roda.

Usa o role de sistema OWNER já semeado pela migration `0007` — não
fabrica um role novo, não concede nada além do que qualquer OWNER de
qualquer organização já recebe.
"""
import sys
import uuid
from getpass import getpass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.normalize import normalize_slug
from nexasalon_api.core.security import hash_password
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.repositories import (
    branch_repo,
    membership_repo,
    organization_repo,
    rbac_repo,
    user_repo,
)


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def _prompt_password() -> str:
    while True:
        password = getpass("Senha do OWNER (mínimo 8 caracteres): ")
        if len(password) < 8:
            print("Senha muito curta — mínimo 8 caracteres.", file=sys.stderr)
            continue
        confirm = getpass("Confirme a senha: ")
        if password != confirm:
            print("As senhas não coincidem.", file=sys.stderr)
            continue
        return password


def main() -> int:
    print("=== NexaSalon — bootstrap do primeiro OWNER ===")
    org_name = _prompt("Nome da organização")
    org_slug = _prompt("Slug da organização (ex.: meu-salao)")
    branch_name = _prompt("Nome da primeira unidade", default="Matriz")
    branch_slug = _prompt("Slug da unidade", default="matriz")
    owner_name = _prompt("Nome do OWNER")
    owner_email = _prompt("E-mail do OWNER")

    if not all([org_name, org_slug, branch_name, branch_slug, owner_name, owner_email]):
        print("Todos os campos são obrigatórios. Abortando.", file=sys.stderr)
        return 1

    # Correção pós-publicação (item "Agendamento Online — slug"): esta
    # era a ÚNICA forma de criar uma organização (não existe rota HTTP
    # de criação) e gravava o slug CRU, sem passar por `normalize_slug`
    # — diferente de `PUT /organizations` (`OrganizationUpdate`), que já
    # normalizava. Um slug digitado como "Meu Salão" (espaço, acento,
    # maiúscula) ficava assim gravado, quebrando o link público
    # `/agendar/<slug>`. Normaliza os dois slugs aqui, mesma função
    # usada pelo backend HTTP.
    normalized_org_slug = normalize_slug(org_slug)
    normalized_branch_slug = normalize_slug(branch_slug)
    if not normalized_org_slug or not normalized_branch_slug:
        print("Slug inválido — use letras, números e hífen (ex.: meu-salao). Abortando.", file=sys.stderr)
        return 1
    if normalized_org_slug != org_slug:
        print(f"Slug da organização normalizado para '{normalized_org_slug}'.")
    if normalized_branch_slug != branch_slug:
        print(f"Slug da unidade normalizado para '{normalized_branch_slug}'.")
    org_slug = normalized_org_slug
    branch_slug = normalized_branch_slug

    password = _prompt_password()

    org_id = uuid.uuid4()
    with SessionLocal() as session:
        # A organização ainda não existe: sem `SET LOCAL app.current_org_id`
        # antes do insert, a policy RLS (cláusula WITH CHECK) rejeitaria a
        # escrita mesmo vindo do role restrito `nexasalon_app` — setamos
        # pro id que a própria organização vai ter (mesmo padrão de
        # `seed_organization`, tests/conftest.py). Nenhum bypass de RLS
        # acontece aqui; é a mesma regra que qualquer escrita usa.
        #
        # NOTA: a policy RLS de `organizations` é estritamente
        # `id = app.current_org_id` (isolamento de tenant raiz — nenhuma
        # organização enxerga outra, nem pra checar se um slug já existe).
        # Por isso NÃO dá pra fazer um SELECT de "slug já existe?" antes
        # do insert: sob esse contexto, a query nunca veria uma
        # organização diferente da que estamos prestes a criar. A
        # unicidade de verdade é garantida pela constraint UNIQUE do
        # banco (`organizations.slug`) — deixamos o insert tentar e
        # tratamos a violação abaixo, sem qualquer bypass de RLS.
        session.execute(text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(org_id)})

        # `users` é global, sem RLS (ver `user_repo.get_by_email`) — este
        # check funciona normalmente, independente de contexto de tenant.
        if user_repo.get_by_email(session, owner_email) is not None:
            print(f"Já existe um usuário com e-mail '{owner_email}'. Abortando.", file=sys.stderr)
            return 1

        owner_role = rbac_repo.get_system_role_by_name(session, "OWNER")
        if owner_role is None:
            print("Role de sistema OWNER não encontrado — rode as migrations (0007) antes.", file=sys.stderr)
            return 1

        try:
            organization = organization_repo.create(session, id=org_id, name=org_name, slug=org_slug)
            branch = branch_repo.create(session, organization.id, name=branch_name, slug=branch_slug)
            user = user_repo.create(
                session, email=owner_email, name=owner_name, password_hash=hash_password(password)
            )
            membership_repo.create(
                session,
                user_id=user.id,
                organization_id=organization.id,
                role_id=owner_role.id,
                status=MembershipStatus.ACTIVE,
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            print(
                f"Não foi possível criar: slug '{org_slug}' ou '{branch_slug}' já em uso. Abortando.",
                file=sys.stderr,
            )
            return 1

    print("\nBootstrap concluído:")
    print(f"  organization_id: {org_id}")
    print(f"  unidade: {branch_name} ({branch_slug})")
    print(f"  owner: {owner_email}")
    print("Faça login em POST /api/v1/auth/login com este e-mail e a senha definida agora.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
