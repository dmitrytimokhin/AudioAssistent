"""Текст встречи для промпта: выбор источника, подстановка полей, нарезка под контекст модели."""
from __future__ import annotations

import re

from .domain import Transcript

PLACEHOLDERS = ("transcript", "date", "kind", "kind_title", "file", "tags", "participants")


def render_transcript(transcript: Transcript, source: str) -> str:
    """dialogue: «[Имя] реплика» по говорящим; raw: фразы Whisper по строке."""
    if source == "dialogue":
        return transcript.render()
    if source == "raw":
        return "\n".join(s.text for s in transcript.segments)
    raise ValueError(f"Неизвестный source: {source!r} (dialogue | raw)")


def fill(template: str, **values: str) -> str:
    """Подставляет {transcript}, {date}, ... Именно replace, а не format: в промптах бывают JSON-примеры с { }.
    Нет {transcript} -- текст дописывается в конец, чтобы промпт без метки не терял встречу."""
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    if "{transcript}" not in template and "transcript" in values:
        result = f"{result}\n\nТранскрипт:\n{values['transcript']}"
    return result


_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def split_text(text: str, max_chars: int) -> list[str]:
    """Нарезка по строкам (реплика целиком), слишком длинная строка -- по предложениям, затем по словам.
    Нужна, чтобы встреча не обрезалась молча: Ollama отбрасывает всё, что не влезло в num_ctx."""
    pieces: list[str] = []
    for line in text.splitlines():
        if len(line) <= max_chars:
            pieces.append(line)
            continue
        current = ""
        for sentence in _SENTENCE.split(line):
            for word_block in _hard_split(sentence, max_chars):
                if current and len(current) + 1 + len(word_block) > max_chars:
                    pieces.append(current)
                    current = ""
                current = f"{current} {word_block}".strip()
        if current:
            pieces.append(current)
    chunks, buffer = [], ""
    for piece in pieces:
        if buffer and len(buffer) + 1 + len(piece) > max_chars:
            chunks.append(buffer)
            buffer = ""
        buffer = f"{buffer}\n{piece}" if buffer else piece
    if buffer.strip():
        chunks.append(buffer)
    return chunks or [""]


def _hard_split(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > max_chars:
            parts.append(current)
            current = ""
        current = f"{current} {word}".strip()
    return [*parts, current] if current else parts
