"""Доменные модели. Не зависят ни от Whisper, ни от Ollama, ни от файловой системы."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

UNKNOWN_SPEAKER = None


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = UNKNOWN_SPEAKER  # full_name участника


@dataclass
class Transcript:
    segments: list[Segment]
    source: str = ""
    meta: dict = field(default_factory=dict)  # служебное (метод и статистика разметки); не сериализуется

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def text_by_speaker(self) -> dict[str | None, str]:
        result: dict[str | None, list[str]] = {}
        for seg in self.segments:
            result.setdefault(seg.speaker, []).append(seg.text)
        return {k: " ".join(v) for k, v in result.items()}

    def merge_turns(self) -> "Transcript":
        """Склеивает подряд идущие сегменты одного говорящего."""
        turns: list[Segment] = []
        for seg in self.segments:
            if turns and turns[-1].speaker == seg.speaker:
                prev = turns[-1]
                turns[-1] = Segment(prev.start, seg.end, f"{prev.text} {seg.text}", prev.speaker)
            else:
                turns.append(seg)
        return Transcript(turns, self.source)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        return cls([Segment(**s) for s in data["segments"]], data.get("source", ""))

    @classmethod
    def from_plain_text(cls, text: str, source: str = "") -> "Transcript":
        """Для ручных тестов без аудио: одна непустая строка = один сегмент."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return cls([Segment(float(i), float(i + 1), ln) for i, ln in enumerate(lines)], source)

    def render(self) -> str:
        return "\n".join(f"[{s.speaker or '???'}] {s.text}" for s in self.merge_turns().segments)


@dataclass(frozen=True)
class Participant:
    full_name: str                 # как выводится в статусе: "Гончар Роман"
    project_id: str
    aliases: tuple[str, ...] = ()  # как к человеку обращаются голосом


@dataclass(frozen=True)
class Project:
    id: str
    participants: tuple[Participant, ...]
    title: str = ""


@dataclass(frozen=True)
class Team:
    projects: tuple[Project, ...]

    @property
    def participants(self) -> list[Participant]:
        return [p for proj in self.projects for p in proj.participants]

    def names_hint(self) -> str:
        """Подсказка для Whisper (initial_prompt): повышает точность распознавания имён."""
        names = ", ".join(p.full_name for p in self.participants)
        return f"Дейли. Участники: {names}."

    def validate(self) -> None:
        seen: dict[str, str] = {}
        for p in self.participants:
            for alias in (p.full_name, *p.aliases):
                key = alias.lower().replace("ё", "е")
                if key in seen and seen[key] != p.full_name:
                    raise ValueError(
                        f"Алиас '{alias}' неоднозначен: {seen[key]} и {p.full_name}. "
                        "Сделайте алиасы уникальными (например, фамилией)."
                    )
                seen[key] = p.full_name


@dataclass
class PersonStatus:
    full_name: str
    project_id: str
    text: str
    has_data: bool = True


@dataclass
class DailySummary:
    day: date
    audience: str
    statuses: list[PersonStatus] = field(default_factory=list)

    def by_project(self) -> dict[str, list[PersonStatus]]:
        result: dict[str, list[PersonStatus]] = {}
        for st in self.statuses:
            result.setdefault(st.project_id, []).append(st)
        return result


@dataclass
class LLMCallRecord:
    """Один вызов LLM для журнала: промпты, ответ или ошибка, длительность."""

    audience: str
    model: str
    system_prompt: str
    user_prompt: str
    status: str                      # "success" | "error"
    duration_ms: float
    response: str | None = None
    error: str | None = None
    full_name: str | None = None
    run_id: int | None = None


def iter_names(team: Team) -> Iterable[str]:
    return (p.full_name for p in team.participants)
