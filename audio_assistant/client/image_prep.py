"""Подготовка фото для vision-модели: поворот по EXIF, уменьшение, перекодирование.

Снимок с телефона (12 Мп, 4-8 МБ) модели не нужен: картинка всё равно приводится к её размеру, а токенов и времени
уходит в разы больше. Поэтому длинная сторона ограничивается (ASSISTANT_IMAGE_MAX_SIDE).
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_HEIC_HINT = "HEIC/HEIF не поддерживается: на iPhone выберите «Наиболее совместимый» формат или конвертируйте в JPEG"


@dataclass
class PreparedImage:
    data: bytes
    width: int
    height: int
    original_bytes: int


def prepare_image(raw: bytes, max_side: int, name: str = "") -> PreparedImage:
    """Байты исходного файла -> байты для отправки модели. Не картинка -> ValueError."""
    try:
        image = Image.open(io.BytesIO(raw))
        original_format = image.format
        image = ImageOps.exif_transpose(image)       # фото с телефона лежат «на боку» + тег поворота
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        hint = f". {_HEIC_HINT}" if name.lower().endswith((".heic", ".heif")) else ""
        raise ValueError(f"{name or 'Файл'} не читается как изображение ({type(exc).__name__}){hint}") from exc

    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.LANCZOS)
    if image.mode in ("RGBA", "LA", "P"):            # прозрачность -> белый фон, иначе JPEG станет чёрным
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    if original_format == "PNG":                     # скриншоты и текст: PNG не размывает буквы
        image.save(buffer, "PNG", optimize=True)
    else:
        image.save(buffer, "JPEG", quality=90)
    return PreparedImage(buffer.getvalue(), image.width, image.height, len(raw))
