"""Testes de `core/storage.py::S3StorageBackend` — a implementação
REAL do backend S3-compatível (os demais testes de upload usam sempre
`_FakeStorageBackend` via dependency override, nunca exercitam esta
classe). Cobre a correção desta rodada: compatibilidade de ACL entre
provedores (`storage_use_object_acl`) e a mensagem de erro correta
("foto do profissional" vs "logo") de `require_storage_backend`."""
from unittest.mock import MagicMock, patch

from nexasalon_api.core.exceptions import ServiceUnavailableError
from nexasalon_api.core.storage import S3StorageBackend, require_storage_backend


def _backend(**overrides) -> S3StorageBackend:
    defaults = dict(
        endpoint_url="https://s3.example.com",
        region="auto",
        bucket="nexasalon-uploads",
        access_key_id="fake-key",
        secret_access_key="fake-secret",
        public_base_url=None,
    )
    defaults.update(overrides)
    return S3StorageBackend(**defaults)


def test_upload_manda_acl_public_read_por_padrao_compativel_com_aws_s3_e_spaces():
    with patch("boto3.client") as boto_client:
        fake_client = MagicMock()
        boto_client.return_value = fake_client
        backend = _backend()

        url = backend.upload(key="professionals/p1/photo-abc.png", content=b"conteudo", content_type="image/png")

        fake_client.put_object.assert_called_once_with(
            Bucket="nexasalon-uploads",
            Key="professionals/p1/photo-abc.png",
            Body=b"conteudo",
            ContentType="image/png",
            ACL="public-read",
        )
        assert url == "https://s3.example.com/nexasalon-uploads/professionals/p1/photo-abc.png"


def test_upload_com_use_object_acl_false_nunca_manda_acl_compativel_com_cloudflare_r2():
    """Correção de compatibilidade: Cloudflare R2 não aceita ACL por
    objeto do mesmo jeito que a AWS S3 — com
    `NEXASALON_STORAGE_USE_OBJECT_ACL=false`, o parâmetro nunca é
    enviado, evitando um erro do PROVEDOR (não do NexaSalon)."""
    with patch("boto3.client") as boto_client:
        fake_client = MagicMock()
        boto_client.return_value = fake_client
        backend = _backend(use_object_acl=False)

        backend.upload(key="professionals/p1/photo-abc.png", content=b"conteudo", content_type="image/png")

        fake_client.put_object.assert_called_once_with(
            Bucket="nexasalon-uploads",
            Key="professionals/p1/photo-abc.png",
            Body=b"conteudo",
            ContentType="image/png",
        )
        assert "ACL" not in fake_client.put_object.call_args.kwargs


def test_upload_usa_public_base_url_quando_configurada():
    with patch("boto3.client") as boto_client:
        boto_client.return_value = MagicMock()
        backend = _backend(public_base_url="https://cdn.nexasalon.com.br/")

        url = backend.upload(key="professionals/p1/photo-abc.png", content=b"x", content_type="image/png")

        assert url == "https://cdn.nexasalon.com.br/professionals/p1/photo-abc.png"


def test_require_storage_backend_mensagem_correta_por_label():
    """Bug real corrigido: a mensagem sempre dizia "logo", mesmo pra
    foto de profissional."""
    try:
        require_storage_backend(None, label="foto do profissional")
        raise AssertionError("deveria ter levantado ServiceUnavailableError")
    except ServiceUnavailableError as exc:
        assert "foto do profissional" in str(exc)
        assert "logo" not in str(exc).lower()

    try:
        require_storage_backend(None)  # default preserva o comportamento de antes (logo)
        raise AssertionError("deveria ter levantado ServiceUnavailableError")
    except ServiceUnavailableError as exc:
        assert "logo" in str(exc)
