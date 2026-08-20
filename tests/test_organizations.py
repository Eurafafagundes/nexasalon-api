"""Testes de `/api/v1/organization` — Etapa D ("Informações do
Estabelecimento" + upload de logo). Cobre o checklist obrigatório do
pedido: estabelecimento sem CNPJ/logo; estabelecimento completo;
atualização dos dados; isolamento entre organizações; usuário sem
`organization.manage`; upload inválido (content-type/tamanho)."""
import dataclasses

from nexasalon_api.core.storage import get_storage_backend
from nexasalon_api.main import app


class _FakeStorageBackend:
    """Fake em memória (`StorageBackend` — protocolo, ver
    `core/storage.py`) — mesmo espírito de `client_as` sobrescrevendo
    `get_current_actor`: nenhum teste depende de credenciais de nuvem
    reais."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []

    def upload(self, *, key: str, content: bytes, content_type: str) -> str:
        self.uploads.append({"key": key, "content": content, "content_type": content_type})
        return f"https://fake-cdn.example.com/{key}"


def _restricted(actor, *, without: str):
    return dataclasses.replace(actor, permissions=actor.permissions - {without})


def test_estabelecimento_sem_cnpj_nem_logo_por_padrao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/api/v1/organization")
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"] is None
    assert body["legal_name"] is None
    assert body["logo_url"] is None
    assert body["cep"] is None
    assert body["state"] is None
    # timezone já tinha um default (reusado, ver models/organization.py)
    assert body["timezone"]


def test_atualizacao_completa_do_estabelecimento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    payload = {
        "name": "Salão Bela Vista",
        "legal_name": "Bela Vista Ltda",
        "document": "11.222.333/0001-81",
        "business_type": "salao_beleza",
        "email": "contato@belavista.com",
        "phone": "6133334444",
        "whatsapp": "61999998888",
        "instagram": "@belavista",
        "website": "https://belavista.com",
        "timezone": "America/Sao_Paulo",
        "cep": "70000-000",
        "state": "DF",
        "city": "Brasília",
        "neighborhood": "Asa Sul",
        "address_line": "SQS 100",
        "address_number": "12",
        "complement": "Bloco A",
    }
    resp = c.put("/api/v1/organization", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Salão Bela Vista"
    assert body["legal_name"] == "Bela Vista Ltda"
    # CNPJ normalizado (só dígitos) — nunca confia na máscara do frontend.
    assert body["document"] == "11222333000181"
    assert body["business_type"] == "salao_beleza"
    assert body["whatsapp"] == "61999998888"
    assert body["instagram"] == "@belavista"
    assert body["website"] == "https://belavista.com"
    assert body["cep"] == "70000000"
    assert body["state"] == "DF"
    assert body["city"] == "Brasília"
    assert body["neighborhood"] == "Asa Sul"
    assert body["address_line"] == "SQS 100"
    assert body["address_number"] == "12"
    assert body["complement"] == "Bloco A"

    # Persistiu de fato — GET subsequente reflete a atualização.
    reread = c.get("/api/v1/organization").json()
    assert reread["document"] == "11222333000181"
    assert reread["city"] == "Brasília"


def test_atualizacao_nao_exige_nenhum_campo_opcional(client_as, org_a_actor):
    """"Não impedir operação do salão porque CNPJ/endereço não foi
    preenchido" — um payload só com `name` é válido."""
    c = client_as(org_a_actor)
    resp = c.put("/api/v1/organization", json={"name": "Só o nome mesmo"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Só o nome mesmo"
    assert resp.json()["document"] is None


def test_cnpj_invalido_e_rejeitado_pelo_backend(client_as, org_a_actor):
    """Nunca confia só na máscara do frontend — checksum validado no
    backend, mesmo padrão já usado pro CPF de Client."""
    c = client_as(org_a_actor)
    resp = c.put("/api/v1/organization", json={"name": "X", "document": "11.111.111/1111-11"})
    assert resp.status_code == 422


def test_isolamento_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    ca = client_as(org_a_actor)
    ca.put("/api/v1/organization", json={"name": "Org A", "document": "11.222.333/0001-81"})

    cb = client_as(org_b_actor)
    resp = cb.get("/api/v1/organization")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] != "Org A"
    assert body["document"] is None  # não vaza o CNPJ cadastrado pela Org A


def test_usuario_sem_permissao_organization_manage_nao_atualiza(client_as, org_a_actor):
    restricted = _restricted(org_a_actor, without="organization.manage")
    c = client_as(restricted)
    resp = c.put("/api/v1/organization", json={"name": "Tentativa Indevida"})
    assert resp.status_code == 403

    # Leitura continua liberada (rota GET não exige permission — ver
    # docstring de `api/v1/organizations.py`).
    assert c.get("/api/v1/organization").status_code == 200


def test_usuario_sem_permissao_nao_faz_upload_de_logo(client_as, org_a_actor):
    restricted = _restricted(org_a_actor, without="organization.manage")
    c = client_as(restricted)
    resp = c.post("/api/v1/organization/logo", files={"file": ("logo.png", b"\x89PNG...", "image/png")})
    assert resp.status_code == 403


def test_upload_de_logo_com_content_type_invalido(client_as, org_a_actor):
    app.dependency_overrides[get_storage_backend] = lambda: _FakeStorageBackend()
    try:
        c = client_as(org_a_actor)
        resp = c.post(
            "/api/v1/organization/logo",
            files={"file": ("logo.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_logo_sem_storage_configurado_retorna_503(client_as, org_a_actor):
    # Sem override: `get_storage_backend` real, sem env de storage
    # configurado no ambiente de teste -> `None` -> 503.
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/organization/logo", files={"file": ("logo.png", b"fake-png-bytes", "image/png")})
    assert resp.status_code == 503


def test_upload_de_logo_com_sucesso_atualiza_logo_url(client_as, org_a_actor):
    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        resp = c.post(
            "/api/v1/organization/logo",
            files={"file": ("logo.png", b"fake-png-bytes", "image/png")},
        )
        assert resp.status_code == 200, resp.text
        logo_url = resp.json()["logo_url"]
        assert logo_url.startswith("https://fake-cdn.example.com/organizations/")
        assert len(fake.uploads) == 1
        assert fake.uploads[0]["content_type"] == "image/png"

        reread = c.get("/api/v1/organization").json()
        assert reread["logo_url"] == logo_url
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_logo_excede_tamanho_maximo(client_as, org_a_actor):
    from nexasalon_api.core.config import settings

    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        too_big = b"0" * (settings.storage_logo_max_bytes + 1)
        resp = c.post("/api/v1/organization/logo", files={"file": ("logo.png", too_big, "image/png")})
        assert resp.status_code == 422
        assert fake.uploads == []
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)
