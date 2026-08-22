"""Testes de `POST /api/v1/professionals/{id}/photo` (Etapa L, Bloco 3) —
upload REAL de foto do profissional, reaproveitando a MESMA infra de
storage já usada pelo logo do estabelecimento (`core/storage.py`, mesmo
backend fake em memória de `test_organizations.py`). Cobre: sucesso
(substitui `photo_url`), content-type inválido, tamanho excedido, e que
a foto aparece no schema PÚBLICO do Agendamento Online (nunca só no
admin)."""
from nexasalon_api.core.storage import get_storage_backend
from nexasalon_api.main import app


class _FakeStorageBackend:
    """Mesmo fake de `test_organizations.py::_FakeStorageBackend` —
    reproduzido aqui (não importado) pra manter os testes deste arquivo
    independentes de mudanças no outro."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []

    def upload(self, *, key: str, content: bytes, content_type: str) -> str:
        self.uploads.append({"key": key, "content": content, "content_type": content_type})
        return f"https://fake-cdn.example.com/{key}"


def test_upload_de_foto_com_sucesso_atualiza_photo_url(client_as, org_a_actor):
    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        assert professional["photo_url"] is None

        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", b"fake-png-bytes", "image/png")},
        )
        assert resp.status_code == 200, resp.text
        photo_url = resp.json()["photo_url"]
        assert photo_url.startswith(f"https://fake-cdn.example.com/professionals/{professional['id']}/")
        assert len(fake.uploads) == 1
        assert fake.uploads[0]["content_type"] == "image/png"

        reread = c.get(f"/api/v1/professionals/{professional['id']}").json()
        assert reread["photo_url"] == photo_url

        # Substituição segura: novo upload troca o ponteiro pra uma chave
        # NOVA (nunca sobrescreve a anterior in-place, ver docstring de
        # `build_professional_photo_key`).
        resp2 = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto2.png", b"outro-conteudo-png", "image/png")},
        )
        assert resp2.status_code == 200, resp2.text
        photo_url_2 = resp2.json()["photo_url"]
        assert photo_url_2 != photo_url
        assert len(fake.uploads) == 2
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_foto_com_content_type_invalido(client_as, org_a_actor):
    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 422
        assert fake.uploads == []
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_foto_excede_tamanho_maximo(client_as, org_a_actor):
    from nexasalon_api.core.config import settings

    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        too_big = b"0" * (settings.storage_logo_max_bytes + 1)
        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", too_big, "image/png")},
        )
        assert resp.status_code == 422
        assert fake.uploads == []
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_foto_sem_storage_configurado_retorna_503(client_as, org_a_actor):
    app.dependency_overrides[get_storage_backend] = lambda: None
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", b"fake-png-bytes", "image/png")},
        )
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_foto_exige_professionals_manage(client_as, org_a_actor):
    import dataclasses

    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()

        restricted = dataclasses.replace(org_a_actor, permissions=org_a_actor.permissions - {"professionals.manage"})
        resp = client_as(restricted).post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", b"fake-png-bytes", "image/png")},
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_foto_do_profissional_aparece_na_listagem_publica_do_agendamento_online(client_as, org_a_actor):
    """Etapa L, Bloco 3 — a foto precisa aparecer onde a cliente escolhe
    o profissional no Agendamento Online público
    (`PublicProfessionalRead.photo_url`), não só no admin."""
    from fastapi.testclient import TestClient

    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        c.put(
            "/api/v1/organization",
            json={
                "online_booking_enabled": True,
                "online_booking_auto_confirm": True,
                "online_booking_min_lead_minutes": 0,
                "online_booking_max_lead_days": 3650,
            },
        )
        org = c.get("/api/v1/organization").json()
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()

        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", b"fake-png-bytes", "image/png")},
        )
        photo_url = resp.json()["photo_url"]

        p = TestClient(app)
        public_professionals = p.get(f"/api/v1/public/booking/{org['slug']}/professionals").json()
        match = next(item for item in public_professionals if item["id"] == professional["id"])
        assert match["photo_url"] == photo_url
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)
