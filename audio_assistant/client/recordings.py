"""Записи встреч: имя файла несёт время записи (диктофон: YYYYMMDDHHMMSS.WAV)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}
_STAMP = re.compile(r"(?<!\d)(\d{14})(?!\d)")


def parse_recording_datetime(name: str) -> datetime | None:
    """'20261101180957.WAV' -> 2026-11-01 18:09:57; None, если в имени нет корректного штампа."""
    m = _STAMP.search(Path(name).stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def list_recordings(audio_dir: Path) -> list[dict]:
    """Аудио в папке, свежие первыми (по штампу в имени, иначе по mtime)."""
    audio_dir = Path(audio_dir)
    if not audio_dir.is_dir():
        return []
    items = []
    for p in audio_dir.iterdir():
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
            stamp = parse_recording_datetime(p.name)
            items.append({"file_name": p.name, "size_bytes": p.stat().st_size,
                          "recorded_at": stamp, "_key": stamp or datetime.fromtimestamp(p.stat().st_mtime)})
    items.sort(key=lambda i: i["_key"], reverse=True)
    for i in items:
        i.pop("_key")
    return items


def resolve_recording(audio_dir: Path, file_name: str) -> Path:
    """Файл из audio_dir по имени; имя с путём (../) недопустимо."""
    if Path(file_name).name != file_name or not file_name:
        raise ValueError(f"Недопустимое имя файла: {file_name!r}")
    path = Path(audio_dir) / file_name
    if not path.is_file():
        raise FileNotFoundError(f"Нет файла {file_name} в {audio_dir}")
    return path
