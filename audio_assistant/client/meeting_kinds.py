"""Типы встреч (config/meeting_kinds.yaml) и теги."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_TAG_LENGTH = 64


@dataclass(frozen=True)
class MeetingKind:
    name: str
    title: str
    prompt: str   # папка в prompts/meetings/


class MeetingKinds:
    def __init__(self, kinds: list[MeetingKind]):
        self._kinds = {k.name: k for k in kinds}

    @property
    def names(self) -> list[str]:
        return list(self._kinds)

    def get(self, name: str) -> MeetingKind:
        return self._kinds[name]

    def validate(self, kind: str | None) -> str | None:
        """None (тип не указан) допустим; опечатка в типе -- нет: иначе встреча выпадет из выборок."""
        if kind is None or kind == "":
            return None
        if kind not in self._kinds:
            raise ValueError(f"Неизвестный тип встречи {kind!r}. Доступно: {self.names}")
        return kind

    @staticmethod
    def normalize_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
        """Обрезка пробелов, без пустых и повторов (регистр сохраняется, порядок -- как указан)."""
        result: list[str] = []
        for tag in tags or ():
            tag = str(tag).strip()
            if len(tag) > MAX_TAG_LENGTH:
                raise ValueError(f"Тег длиннее {MAX_TAG_LENGTH} символов: {tag[:20]!r}...")
            if tag and tag not in result:
                result.append(tag)
        return result


def load_meeting_kinds(path: Path) -> MeetingKinds:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    kinds = [MeetingKind(name, (cfg or {}).get("title", name), (cfg or {}).get("prompt", name))
             for name, cfg in (raw.get("kinds") or {}).items()]
    if not kinds:
        raise ValueError(f"В {path} нет ни одного типа встречи")
    return MeetingKinds(kinds)
