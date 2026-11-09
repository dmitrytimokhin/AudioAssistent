"""Контекст текущего запуска: кто и в рамках какого запуска вызывает LLM.

ContextVar, а не аргументы: порт LLMClient.generate(system, user) остаётся узким,
а журнал вызовов всё равно знает run_id и сотрудника. Контекст копируется
в threadpool (run_in_threadpool), поэтому работает и из FastAPI-эндпоинтов.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)
current_person: ContextVar[str | None] = ContextVar("current_person", default=None)
current_audience: ContextVar[str] = ContextVar("current_audience", default="")


@contextmanager
def llm_context(run_id: int | None, audience: str, person: str | None = None):
    """Метки для журнала llm_calls на время блока (разбор встречи, сводка)."""
    tokens = (current_run_id.set(run_id), current_audience.set(audience), current_person.set(person))
    try:
        yield
    finally:
        current_run_id.reset(tokens[0])
        current_audience.reset(tokens[1])
        current_person.reset(tokens[2])


# Прогресс долгих стадий (транскрибация): run_id -> (обработано секунд аудио, всего секунд).
# В памяти процесса: после рестарта сервиса прерванные запуски помечаются ошибкой, а не продолжаются.
_progress: dict[int, tuple[float, float]] = {}


def report_progress(done: float, total: float) -> None:
    run_id = current_run_id.get()
    if run_id is not None and total > 0:
        _progress[run_id] = (done, total)


def get_progress(run_id: int) -> float | None:
    """Доля 0..1 или None, если стадия не идёт (или бэкенд не умеет сообщать прогресс)."""
    done, total = _progress.get(run_id, (0.0, 0.0))
    return round(min(done / total, 1.0), 3) if total else None


def clear_progress(run_id: int) -> None:
    _progress.pop(run_id, None)
