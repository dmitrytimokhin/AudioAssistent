"""Контекст текущего запуска: кто и в рамках какого запуска вызывает LLM.

ContextVar, а не аргументы: порт LLMClient.generate(system, user) остаётся узким,
а журнал вызовов всё равно знает run_id и сотрудника. Контекст копируется
в threadpool (run_in_threadpool), поэтому работает и из FastAPI-эндпоинтов.
"""
from __future__ import annotations

from contextvars import ContextVar

current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)
current_person: ContextVar[str | None] = ContextVar("current_person", default=None)
current_audience: ContextVar[str] = ContextVar("current_audience", default="")
