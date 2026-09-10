"""Testes de `services/financial_categories.py` — categoria de
lançamento financeiro manual (painel "Resultado disponível"). Mesmo
padrão de `test_dashboard_bi_update.py`: fixtures duplicadas
localmente, direto no service layer."""
import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.enums import (
    CashMovementType,
    CashRegisterStatus,
    ExpenseNature,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.schemas.financial_category import (
    FinancialCategoryCreate,
    FinancialCategoryUpdate,
)
from nexasalon_api.schemas.fixed_expense import FixedExpenseCreate
from nexasalon_api.services import financial_categories as financial_categories_service
from nexasalon_api.services import fixed_expenses as fixed_expenses_service


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org financeira", slug=f"org-fin-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=frozenset({"organization.manage"})) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def test_criar_listar_e_editar_categoria(org_session):
    session, org_id = org_session
    created = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Aluguel", nature=ExpenseNature.FIXED, display_order=0)
    )
    assert created.nature == ExpenseNature.FIXED

    categories = financial_categories_service.list_categories(session, org_id)
    assert len(categories) == 1

    updated = financial_categories_service.update_category(
        session, org_id, created.id,
        FinancialCategoryUpdate(name="Aluguel do salão", nature=ExpenseNature.FIXED, display_order=0),
    )
    assert updated.name == "Aluguel do salão"


def test_reclassificar_nature_muda_categoria_sem_tocar_lancamentos(org_session):
    """Reclassificar `nature` é uma edição da CATEGORIA — nunca reescreve
    nenhum `CashMovement` já criado (a natureza é lida por JOIN no
    momento do cálculo, nunca denormalizada no lançamento)."""
    session, org_id = org_session
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Produtos", nature=ExpenseNature.VARIABLE, display_order=0)
    )
    financial_categories_service.update_category(
        session, org_id, category.id,
        FinancialCategoryUpdate(name="Produtos", nature=ExpenseNature.FIXED, display_order=0),
    )
    reloaded = financial_categories_service.get_category(session, org_id, category.id)
    assert reloaded.nature == ExpenseNature.FIXED


def test_reclassificar_nature_bloqueada_quando_usada_por_despesa_fixa(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = Branch(
        organization_id=org_id, name="Matriz", slug=f"matriz-{uuid.uuid4().hex[:8]}"
    )
    session.add(branch)
    session.flush()
    category = financial_categories_service.create_category(
        session, org_id,
        FinancialCategoryCreate(name="Estrutura", nature=ExpenseNature.FIXED, display_order=0),
    )
    fixed_expenses_service.create_expense(
        session, actor,
        FixedExpenseCreate(
            name="Aluguel", financial_category_id=category.id, amount=Decimal("2420.00"),
            recurrence="monthly", due_day=10,
            start_month=fixed_expenses_service.current_month(), branch_id=branch.id,
        ),
    )

    with pytest.raises(ConflictError):
        financial_categories_service.update_category(
            session, org_id, category.id,
            FinancialCategoryUpdate(
                name="Estrutura", nature=ExpenseNature.VARIABLE, display_order=0
            ),
        )


def test_desativar_nao_apaga_nem_desvincula(org_session):
    session, org_id = org_session
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Internet", nature=ExpenseNature.FIXED, display_order=0)
    )
    deactivated = financial_categories_service.set_category_active(session, org_id, category.id, False)
    assert deactivated.is_active is False
    assert financial_categories_service.get_category(session, org_id, category.id) is not None


def test_excluir_categoria_sem_lancamentos_funciona(org_session):
    session, org_id = org_session
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Descartáveis", nature=ExpenseNature.VARIABLE, display_order=0)
    )
    financial_categories_service.delete_category(session, org_id, category.id)
    with pytest.raises(NotFoundError):
        financial_categories_service.get_category(session, org_id, category.id)


def test_excluir_categoria_com_lancamentos_vinculados_e_bloqueado(org_session):
    session, org_id = org_session
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Teste")
    session.add(user)
    session.flush()
    register = CashRegister(
        organization_id=org_id, opened_by=user.id, opened_by_name="Teste",
        initial_amount=Decimal("0"), status=CashRegisterStatus.OPEN,
    )
    session.add(register)
    session.flush()
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Produtos", nature=ExpenseNature.VARIABLE, display_order=0)
    )
    session.add(
        CashMovement(
            organization_id=org_id, cash_register_id=register.id, type=CashMovementType.WITHDRAWAL,
            amount=Decimal("50.00"), description="Compra", financial_category_id=category.id,
            created_by=user.id, created_by_name="Teste",
        )
    )
    session.flush()

    with pytest.raises(ConflictError):
        financial_categories_service.delete_category(session, org_id, category.id)


def test_isolamento_entre_organizacoes(org_session):
    session, org_a = org_session
    category = financial_categories_service.create_category(
        session, org_a, FinancialCategoryCreate(name="Aluguel", nature=ExpenseNature.FIXED, display_order=0)
    )

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B financeira", slug=f"org-b-fin-{org_b.hex[:8]}"))
    session.flush()

    with pytest.raises(NotFoundError):
        financial_categories_service.get_category(session, org_b, category.id)


def test_http_sem_organization_manage_recebe_403(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"dashboard.view"}))
    client = client_as(restricted)
    resp = client.post("/api/v1/financial-categories", json={"name": "Aluguel", "nature": "fixed", "display_order": 0})
    assert resp.status_code == 403


def test_http_com_organization_manage_cria_e_lista(org_a_actor, client_as):
    client = client_as(org_a_actor)
    resp = client.post(
        "/api/v1/financial-categories", json={"name": "Aluguel", "nature": "fixed", "display_order": 0}
    )
    assert resp.status_code == 201
    assert resp.json()["nature"] == "fixed"

    listed = client.get("/api/v1/financial-categories")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
