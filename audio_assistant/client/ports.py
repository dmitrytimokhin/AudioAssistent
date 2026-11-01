"""Узкие интерфейсы (ISP). Пайплайн зависит только от них (DIP)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from .domain import DailySummary, LLMCallRecord, Transcript


class Transcriber(Protocol):
    def transcribe(self, audio: Path, prompt_hint: str = "") -> Transcript: ...


class SpeakerAttributor(Protocol):
    def attribute(self, transcript: Transcript) -> Transcript: ...


class LLMClient(Protocol):
    def generate(self, system: str, user: str, json_schema: dict | None = None) -> str:
        """json_schema задан -> ответ строго по схеме (структурированный вывод)."""
        ...


class HistoryRepository(Protocol):
    def save(self, summary: DailySummary) -> None: ...

    def previous_status(self, full_name: str, before: date, audience: str) -> str | None: ...

    def load_summary(self, run_id: int, audience: str) -> DailySummary | None: ...


class RunStore(Protocol):
    """Хранилище запусков по стадиям: created -> transcribed -> attributed -> summarized -> reported.

    Каждая стадия сохраняет свой результат, поэтому её можно перезапустить отдельно
    (например, пересчитать атрибуцию после правки алиасов или суммаризацию другим промптом).
    """

    supports_stages: bool

    def start(self, day: date, source: str, recorded_at: datetime | None = None) -> int | None: ...

    def run_day(self, run_id: int) -> date: ...

    def save_raw(self, run_id: int | None, transcript: Transcript) -> None: ...

    def load_raw(self, run_id: int) -> Transcript: ...

    def save_attributed(self, run_id: int | None, transcript: Transcript) -> None: ...

    def load_attributed(self, run_id: int) -> Transcript: ...

    def set_stage(self, run_id: int | None, stage: str) -> None: ...

    def save_report(self, run_id: int | None, audience: str, text: str) -> None: ...

    def fail(self, run_id: int | None, error: str) -> None: ...

    def delete(self, *, run_id: int | None = None, file_name: str | None = None,
               meeting_date: date | None = None, everything: bool = False,
               dry_run: bool = False) -> dict: ...


class CallLog(Protocol):
    def record(self, call: LLMCallRecord) -> None: ...


class NullRunStore:
    """Ничего не пишет: CLI, ноутбук, тесты. Стадии по run_id недоступны."""

    supports_stages = False

    def start(self, day: date, source: str, recorded_at: datetime | None = None) -> int | None:
        return None

    def run_day(self, run_id: int) -> date:
        raise NotImplementedError("Стадии требуют ASSISTANT_HISTORY_BACKEND=postgres")

    load_raw = load_attributed = run_day

    def delete(self, **kwargs) -> dict:
        raise NotImplementedError("Удаление требует ASSISTANT_HISTORY_BACKEND=postgres")

    def save_raw(self, run_id: int | None, transcript: Transcript) -> None: ...

    def save_attributed(self, run_id: int | None, transcript: Transcript) -> None: ...

    def set_stage(self, run_id: int | None, stage: str) -> None: ...

    def save_report(self, run_id: int | None, audience: str, text: str) -> None: ...

    def fail(self, run_id: int | None, error: str) -> None: ...


class NullCallLog:
    def record(self, call: LLMCallRecord) -> None: ...
