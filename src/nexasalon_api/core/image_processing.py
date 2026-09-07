"""Normalização de imagem de upload (foto de profissional/logo) antes
de enviar pro storage — correção de bug real reportado: fotos vêm do
celular do usuário em resolução arbitrária (ex.: 670×452, ou muito
maior em celulares modernos) e às vezes com a rotação certa só numa
tag EXIF (não nos pixels em si) — sem corrigir isso aqui, quem exibe a
imagem ignorando EXIF (a maioria dos navegadores respeita, mas nem
todo consumidor da URL final respeita) mostra a foto deitada ou de
cabeça pra baixo.

Duas correções, SEM MUDAR o formato escolhido pelo usuário (PNG
continua PNG, JPEG continua JPEG, WebP continua WebP — nunca força
conversão):

1. `ImageOps.exif_transpose` — gira/espelha os PIXELS conforme a tag
   EXIF de orientação e remove a tag em seguida (a imagem resultante já
   nasce "reta", sem depender de quem for exibi-la depois interpretar
   EXIF corretamente).
2. Redimensiona (só para BAIXO, nunca upscale — uma foto pequena
   enviada continua do tamanho original) se a maior dimensão passar de
   `MAX_DIMENSION` — generoso o bastante pra continuar nítido em telas
   de alta densidade (um avatar de 40px CSS a 3x de pixel density
   precisa de só 120px reais; 1024px sobra folga considerável mesmo
   pra um uso futuro maior, tipo a Ficha do profissional em tela
   cheia).

Nunca falha silenciosamente: um arquivo que passa pela validação de
`content_type`/tamanho (`core/storage.py::_validate_image_upload`) mas
não é um arquivo de imagem de verdade (extensão/Content-Type
forjados) quebra aqui com uma mensagem clara — a MESMA proteção que a
extensão do arquivo sozinha nunca garantiria.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

from nexasalon_api.core.exceptions import ValidationDomainError

MAX_DIMENSION = 1024
_JPEG_WEBP_QUALITY = 85

_FORMAT_BY_CONTENT_TYPE = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}


def normalize_image(content: bytes, content_type: str, *, label: str) -> bytes:
    """Recebe os bytes originais do upload e devolve os bytes já
    normalizados (orientação corrigida, redimensionado se necessário),
    no MESMO formato de `content_type`. `label` só entra na mensagem de
    erro (ex.: "foto do profissional", "logo"), mesmo padrão de
    `core/storage.py::_validate_image_upload`."""
    pil_format = _FORMAT_BY_CONTENT_TYPE.get(content_type)
    if pil_format is None:
        # Nunca deveria chegar aqui — `_validate_image_upload` já
        # bloqueou antes qualquer `content_type` fora da allowlist.
        # Mantido só como segunda barreira defensiva.
        raise ValidationDomainError(f"Formato de imagem não suportado para {label}.")

    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            normalized = ImageOps.exif_transpose(image)
            if normalized is None:
                normalized = image

            if pil_format == "JPEG" and normalized.mode not in ("RGB", "L"):
                # JPEG não tem canal alfa — compõe sobre fundo branco em
                # vez de deixar o encoder falhar/descartar a
                # transparência de forma imprevisível.
                background = Image.new("RGB", normalized.size, (255, 255, 255))
                rgba = normalized.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[3])
                normalized = background

            width, height = normalized.size
            longest_side = max(width, height)
            if longest_side > MAX_DIMENSION:
                scale = MAX_DIMENSION / longest_side
                new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                normalized = normalized.resize(new_size, Image.Resampling.LANCZOS)

            buffer = BytesIO()
            save_kwargs: dict = {}
            if pil_format in ("JPEG", "WEBP"):
                save_kwargs["quality"] = _JPEG_WEBP_QUALITY
            if pil_format == "JPEG":
                save_kwargs["optimize"] = True
            normalized.save(buffer, format=pil_format, **save_kwargs)
            return buffer.getvalue()
    except ValidationDomainError:
        raise
    except Exception as exc:  # noqa: BLE001 — qualquer falha de decodificação vira erro de domínio claro
        raise ValidationDomainError(
            f"Não foi possível processar o arquivo de {label} — verifique se é uma imagem válida."
        ) from exc
