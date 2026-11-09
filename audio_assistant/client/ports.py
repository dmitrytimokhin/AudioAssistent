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
    def generate(self, system: str, user: str, json_schema: dict | None = None,
                 images: list[bytes] | None = None) -> str:
        """json_schema задан -> ответ строго по схеме (структурированный вывод).
        images -- готовые к отправке байты картинок (модель должна уметь vision)."""
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

    def start(self, day: date, source: str, recorded_at: datetime | None = None,
              kind: str | None = None, tags: list[str] | None = None) -> int | None: ...

    def run_day(self, run_id: int) -> date: ...

    def run_info(self, run_id: int) -> dict: ...

    def update_meta(self, run_id: int, **fields) -> dict: ...

    def list_runs(self, limit: int | None = 20, **filters) -> list[dict]: ...

    def save_analysis(self, run_id: int, prompt_name: str, source: str, model: str,
                      chunks: int, text_: str) -> None: ...

    def find_active(self, source: str) -> int | None: ...

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

    def start(self, day: date, source: str, recorded_at: datetime | None = None,
              kind: str | None = None, tags: list[str] | None = None) -> int | None:
        return None

    def run_day(self, *args, **kwargs):
        raise NotImplementedError("Стадии требуют ASSISTANT_HISTORY_BACKEND=postgres")

    load_raw = load_attributed = find_active = run_info = update_meta = run_day
    list_runs = save_analysis = run_day

    def table_counts(self) -> dict:
        raise NotImplementedError("Просмотр таблиц требует ASSISTANT_HISTORY_BACKEND=postgres")

    def table_rows(self, *args, **kwargs) -> dict:
        return self.table_counts()

    def delete(self, **kwargs) -> dict:
        raise NotImplementedError("Удаление требует ASSISTANT_HISTORY_BACKEND=postgres")

    def save_raw(self, run_id: int | None, transcript: Transcript) -> None: ...

    def save_attributed(self, run_id: int | None, transcript: Transcript) -> None: ...

    def set_stage(self, run_id: int | None, stage: str) -> None: ...

    def save_report(self, run_id: int | None, audience: str, text: str) -> None: ...

    def fail(self, run_id: int | None, error: str) -> None: ...


class NullCallLog:
    def record(self, call: LLMCallRecord) -> None: ...
