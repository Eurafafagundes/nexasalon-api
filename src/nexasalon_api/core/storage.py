"""Abstração de storage de arquivos — Etapa D ("Informações do
Estabelecimento", upload de logo).

Nenhuma infraestrutura de storage existia no projeto antes desta etapa
(confirmado por inspeção de `pyproject.toml`/`src/` antes de escrever
este módulo). O pedido foi explícito: "escolha uma solução compatível
com produção/SaaS; não implemente armazenamento local improvisado nem
base64 grande no banco" — então a escolha aqui é um backend
S3-COMPATÍVEL via `boto3`. "S3-compatível" (não "AWS S3" especificamente)
importa: a mesma implementação funciona com AWS S3, Cloudflare R2,
DigitalOcean Spaces ou MinIO trocando só `storage_endpoint_url` — nunca
prende a aplicação a um provedor específico.

`StorageBackend` é um Protocol (não uma classe base abstrata) só pra
manter o mesmo estilo leve já usado em outros pontos do projeto — o
que importa é a assinatura, não a hierarquia de classes.

Padrão de dependency injection espelha `api/deps.py::get_current_actor`:
`get_storage_backend()` é a função usada em `Depends()` nas rotas, e
`tests/conftest.py` pode sobrescrevê-la via `app.dependency_overrides`
com um backend fake em memória — exatamente como já é feito hoje pra
autenticação — sem precisar de credenciais de nuvem reais pra rodar a
suíte de testes.
"""
from __future__ import annotations

import uuid
from typing import Protocol

from nexasalon_api.core.config import settings
from nexasalon_api.core.exceptions import ServiceUnavailableError, ValidationDomainError


class StorageBackend(Protocol):
    def upload(self, *, key: str, content: bytes, content_type: str) -> str:
        """Envia `content` para `key` e retorna a URL pública final do
        arquivo."""
        ...


class S3StorageBackend:
    """Implementação de produção — qualquer serviço compatível com a
    API S3 (AWS S3, Cloudflare R2, DigitalOcean Spaces, MinIO)."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        region: str | None,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        public_base_url: str | None,
    ) -> None:
        import boto3

        self._bucket = bucket
        self._public_base_url = public_base_url
        self._endpoint_url = endpoint_url
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def upload(self, *, key: str, content: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            # ACL pública explícita — a logo do estabelecimento precisa
            # ser exibível direto (comprovante, tela de Configurações)
            # sem exigir URL assinada/expirável.
            ACL="public-read",
        )
        if self._public_base_url:
            return f"{self._public_base_url.rstrip('/')}/{key}"
        return f"{(self._endpoint_url or '').rstrip('/')}/{self._bucket}/{key}"


def _build_storage_backend() -> StorageBackend | None:
    """`None` quando storage não está configurado — a aplicação sobe
    normalmente sem isso (upload de logo fica indisponível, mas
    `Organization.logo_url` é opcional e o salão opera sem logo)."""
    if not (settings.storage_bucket and settings.storage_access_key_id and settings.storage_secret_access_key):
        return None
    return S3StorageBackend(
        endpoint_url=settings.storage_endpoint_url,
        region=settings.storage_region,
        bucket=settings.storage_bucket,
        access_key_id=settings.storage_access_key_id,
        secret_access_key=settings.storage_secret_access_key,
        public_base_url=settings.storage_public_base_url,
    )


def get_storage_backend() -> StorageBackend | None:
    """Dependency (`Depends(get_storage_backend)`) — sobrescrita em
    testes via `app.dependency_overrides` por um fake em memória."""
    return _build_storage_backend()


def _validate_image_upload(*, content_type: str | None, size_bytes: int, label: str) -> None:
    """Validação SERVER-SIDE genérica de upload de imagem — nunca confia
    só no `accept` do `<input type="file">` do frontend (mesmo
    raciocínio já aplicado ao CPF na Etapa C.1: máscara/validação de
    frontend nunca é a garantia real). `label` só entra na mensagem de
    erro (ex.: "logo", "foto do profissional") — as REGRAS (formatos,
    tamanho máximo) são as MESMAS pros dois usos, reaproveitando
    `storage_logo_max_bytes`/`storage_logo_allowed_content_types`
    (Etapa L, Bloco 3: "reutilizar exatamente essa infraestrutura", não
    criar uma segunda configuração paralela de limites)."""
    if content_type not in settings.storage_logo_allowed_content_types:
        allowed = ", ".join(settings.storage_logo_allowed_content_types)
        raise ValidationDomainError(f"Formato de imagem não suportado. Formatos aceitos: {allowed}.")
    if size_bytes <= 0:
        raise ValidationDomainError(f"Arquivo de {label} vazio.")
    if size_bytes > settings.storage_logo_max_bytes:
        max_mb = settings.storage_logo_max_bytes / (1024 * 1024)
        raise ValidationDomainError(f"Arquivo de {label} excede o tamanho máximo permitido ({max_mb:.0f}MB).")


def validate_logo_upload(*, content_type: str | None, size_bytes: int) -> None:
    _validate_image_upload(content_type=content_type, size_bytes=size_bytes, label="logo")


def validate_professional_photo_upload(*, content_type: str | None, size_bytes: int) -> None:
    """Etapa L, Bloco 3 — upload real de foto do profissional, mesma
    validação/limites do logo (ver `_validate_image_upload`)."""
    _validate_image_upload(content_type=content_type, size_bytes=size_bytes, label="foto do profissional")


def build_logo_key(organization_id: uuid.UUID, content_type: str) -> str:
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(content_type, "bin")
    return f"organizations/{organization_id}/logo-{uuid.uuid4().hex}.{ext}"


def build_professional_photo_key(professional_id: uuid.UUID, content_type: str) -> str:
    """Chave sempre NOVA (UUID aleatório, nunca reaproveita a anterior) —
    troca de foto vira "gravar um objeto novo + trocar o ponteiro
    `Professional.photo_url`", nunca um overwrite in-place (Bloco 3:
    "substituir foto anterior de forma segura"). Limitação honesta: o
    objeto antigo no bucket não é apagado automaticamente (ver relatório
    final) — o ponteiro no banco está sempre correto, mas um
    housekeeping de storage (lifecycle rule no bucket, ou um job de
    limpeza) fica fora do escopo desta etapa."""
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(content_type, "bin")
    return f"professionals/{professional_id}/photo-{uuid.uuid4().hex}.{ext}"


def require_storage_backend(backend: StorageBackend | None) -> StorageBackend:
    if backend is None:
        raise ServiceUnavailableError(
            "Upload de logo indisponível: nenhum storage está configurado neste ambiente."
        )
    return backend
