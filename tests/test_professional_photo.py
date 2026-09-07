"""Testes de `POST /api/v1/professionals/{id}/photo` (Etapa L, Bloco 3) —
upload REAL de foto do profissional, reaproveitando a MESMA infra de
storage já usada pelo logo do estabelecimento (`core/storage.py`, mesmo
backend fake em memória de `test_organizations.py`). Cobre: sucesso
(substitui `photo_url`), content-type inválido, tamanho excedido, que a
foto aparece no schema PÚBLICO do Agendamento Online (nunca só no
admin), e a correção desta rodada: mensagem de erro correta ("foto do
profissional", nunca "logo"), normalização de imagem
(`core/image_processing.py` — orientação EXIF, redimensionamento sem
upscale, sem distorção), arquivo que finge ser imagem mas não é, e que
uma falha de upload NUNCA apaga/troca a foto anterior."""
from io import BytesIO

from PIL import Image

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


def _real_png_bytes(size: tuple[int, int] = (20, 20), color=(255, 0, 0)) -> bytes:
    """PNG de verdade, mínimo, gerado em memória — necessário desde que
    `upload_professional_photo` passou a de fato DECODIFICAR a imagem
    (`normalize_image`, correção desta rodada); os bytes fixos usados
    antes (`b"fake-png-bytes"`) não são um PNG válido e agora seriam
    corretamente recusados como "arquivo inválido"."""
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _real_jpeg_bytes_with_exif_rotation(size: tuple[int, int] = (400, 200)) -> bytes:
    """JPEG de verdade com a tag EXIF de orientação "rotate 90°"
    (orientation=6) — mesmo cenário real de foto tirada na vertical por
    um celular. Usado para provar que `normalize_image` corrige a
    rotação nos PIXELS em vez de depender de quem exibe respeitar
    EXIF."""
    image = Image.new("RGB", size, color=(0, 128, 255))
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation tag = "Rotate 90 CW"
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_upload_de_foto_com_sucesso_atualiza_photo_url(client_as, org_a_actor):
    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        assert professional["photo_url"] is None

        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", _real_png_bytes(), "image/png")},
        )
        assert resp.status_code == 200, resp.text
        photo_url = resp.json()["photo_url"]
        assert photo_url.startswith(f"https://fake-cdn.example.com/professionals/{professional['id']}/")
        assert len(fake.uploads) == 1
        assert fake.uploads[0]["content_type"] == "image/png"
        # O que sobe pro storage é o PNG já NORMALIZADO (mesmo
        # formato, ainda um PNG válido/decodificável) — nunca os bytes
        # crus sem passar pela normalização.
        Image.open(BytesIO(fake.uploads[0]["content"])).verify()

        reread = c.get(f"/api/v1/professionals/{professional['id']}").json()
        assert reread["photo_url"] == photo_url

        # Substituição segura: novo upload troca o ponteiro pra uma chave
        # NOVA (nunca sobrescreve a anterior in-place, ver docstring de
        # `build_professional_photo_key`).
        resp2 = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto2.png", _real_png_bytes(color=(0, 255, 0)), "image/png")},
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


def test_upload_de_foto_sem_storage_configurado_retorna_503_com_mensagem_correta(client_as, org_a_actor):
    """Bug real corrigido nesta rodada: a mensagem dizia "Upload de
    logo indisponível" mesmo pra foto de profissional — confuso pra
    quem está na aba Profissionais, nunca mexeu em Logo."""
    app.dependency_overrides[get_storage_backend] = lambda: None
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", _real_png_bytes(), "image/png")},
        )
        assert resp.status_code == 503
        message = resp.json()["error"]["message"]
        assert "foto do profissional" in message
        assert "logo" not in message.lower()
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_de_arquivo_que_finge_ser_imagem_mas_nao_e_e_recusado(client_as, org_a_actor):
    """Content-Type e extensão dizem "image/png", mas os bytes não são
    um PNG de verdade — `normalize_image` decodifica de verdade (via
    Pillow) e recusa com uma mensagem clara, em vez de subir um arquivo
    corrompido/ilegível pro storage."""
    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
        resp = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", b"isto-nao-e-um-png-de-verdade", "image/png")},
        )
        assert resp.status_code == 422, resp.text
        assert "foto do profissional" in resp.json()["error"]["message"]
        assert fake.uploads == []
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_upload_malsucedido_preserva_a_foto_anterior(client_as, org_a_actor):
    """Item explícito do pedido: uma falha de upload (arquivo inválido,
    aqui) nunca apaga nem troca `photo_url` — a foto anterior continua
    valendo até um upload que REALMENTE tenha sucesso."""
    fake = _FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake
    try:
        c = client_as(org_a_actor)
        professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()

        first = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", _real_png_bytes(), "image/png")},
        )
        assert first.status_code == 200, first.text
        original_photo_url = first.json()["photo_url"]

        failed = c.post(
            f"/api/v1/professionals/{professional['id']}/photo",
            files={"file": ("foto.png", b"nao-e-uma-imagem", "image/png")},
        )
        assert failed.status_code == 422

        reread = c.get(f"/api/v1/professionals/{professional['id']}").json()
        assert reread["photo_url"] == original_photo_url
        assert len(fake.uploads) == 1  # o upload malsucedido nunca chegou a bater no storage
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
            files={"file": ("foto.png", _real_png_bytes(), "image/png")},
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
            files={"file": ("foto.png", _real_png_bytes(), "image/png")},
        )
        photo_url = resp.json()["photo_url"]

        p = TestClient(app)
        public_professionals = p.get(f"/api/v1/public/booking/{org['slug']}/professionals").json()
        match = next(item for item in public_professionals if item["id"] == professional["id"])
        assert match["photo_url"] == photo_url
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


# ---------------------------------------------------------------------
# `core/image_processing.py::normalize_image` — orientação EXIF,
# redimensionamento sem upscale, sem distorção.
# ---------------------------------------------------------------------


def test_normalize_image_corrige_rotacao_exif_nos_pixels(client_as, org_a_actor):
    """Uma foto de celular com orientation=6 (rotate 90°) na tag EXIF
    precisa sair com os PIXELS já girados — quem for exibir a URL final
    (ex.: um `<img>` cru, sem JS interpretando EXIF) mostra a foto na
    orientação certa mesmo assim."""
    from nexasalon_api.core.image_processing import normalize_image

    original = _real_jpeg_bytes_with_exif_rotation(size=(400, 200))
    with Image.open(BytesIO(original)) as img:
        assert img.getexif().get(0x0112) == 6  # confirma o cenário de teste antes de normalizar

    normalized = normalize_image(original, "image/jpeg", label="foto do profissional")
    with Image.open(BytesIO(normalized)) as result:
        result.load()
        # orientation=6 gira 90° -> dimensões originais (400x200)
        # ficam transpostas (200x400) depois da correção.
        assert result.size == (200, 400)
        assert result.getexif().get(0x0112) in (None, 1)  # tag removida/normalizada, nunca repetida


def test_normalize_image_reduz_imagem_grande_mas_nunca_aumenta_pequena():
    from nexasalon_api.core.image_processing import MAX_DIMENSION, normalize_image

    large = _real_png_bytes(size=(2000, 1000))
    normalized_large = normalize_image(large, "image/png", label="foto do profissional")
    with Image.open(BytesIO(normalized_large)) as result:
        assert max(result.size) == MAX_DIMENSION
        # Proporção preservada (2000x1000 = 2:1) — nunca distorce.
        assert result.size == (MAX_DIMENSION, MAX_DIMENSION // 2)

    small = _real_png_bytes(size=(20, 20))
    normalized_small = normalize_image(small, "image/png", label="foto do profissional")
    with Image.open(BytesIO(normalized_small)) as result:
        # Imagem já pequena não é ampliada — nunca upscale.
        assert result.size == (20, 20)


def test_normalize_image_preserva_o_formato_escolhido():
    from nexasalon_api.core.image_processing import normalize_image

    png = _real_png_bytes()
    normalized = normalize_image(png, "image/png", label="foto do profissional")
    with Image.open(BytesIO(normalized)) as result:
        assert result.format == "PNG"


def test_normalize_image_arquivo_invalido_gera_erro_claro():
    from nexasalon_api.core.exceptions import ValidationDomainError
    from nexasalon_api.core.image_processing import normalize_image

    try:
        normalize_image(b"isto-nao-e-uma-imagem", "image/png", label="foto do profissional")
        raise AssertionError("deveria ter levantado ValidationDomainError")
    except ValidationDomainError as exc:
        assert "foto do profissional" in str(exc)


def test_normalize_image_transparencia_png_preservada_ao_nao_forcar_jpeg():
    """PNG com transparência precisa continuar PNG com o canal alfa
    intacto — a conversão pra RGB (composição sobre fundo branco) só
    acontece quando o formato de SAÍDA é JPEG, nunca pra PNG/WebP."""
    from nexasalon_api.core.image_processing import normalize_image

    buffer = BytesIO()
    Image.new("RGBA", (20, 20), color=(255, 0, 0, 128)).save(buffer, format="PNG")
    normalized = normalize_image(buffer.getvalue(), "image/png", label="foto do profissional")
    with Image.open(BytesIO(normalized)) as result:
        assert result.mode in ("RGBA", "LA", "P")
        if result.mode == "RGBA":
            assert result.getpixel((0, 0))[3] < 255  # ainda tem transparência real, não opaco


def test_normalize_image_exif_transpose_nunca_ignora_ausencia_de_orientation():
    """Sem tag EXIF de orientação — a imagem sai idêntica (nenhuma
    rotação indevida aplicada a uma foto que já estava reta)."""
    from nexasalon_api.core.image_processing import normalize_image

    original = _real_png_bytes(size=(40, 20))
    normalized = normalize_image(original, "image/png", label="foto do profissional")
    with Image.open(BytesIO(normalized)) as result:
        assert result.size == (40, 20)
