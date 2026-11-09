"""Произвольный промпт к LLM поверх транскрипта: для записей, которые не дейли."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .ports import LLMClient

TRANSCRIPT_PLACEHOLDER = "{transcript}"


@dataclass
class PromptResult:
    text: str
    model: str
    duration_ms: float


def model_name_of(llm) -> str:
    return getattr(llm, "_model", None) or getattr(llm, "model_name", "") or ""


class PromptRunner:
    def __init__(self, llm: LLMClient):
        self._llm = llm

    def run(self, prompt: str, transcript_text: str | None = None, system: str | None = None,
            json_schema: dict | None = None) -> PromptResult:
        """`{transcript}` в prompt заменяется текстом; нет метки -> текст дописывается в конце.
        Без transcript_text промпт уходит как есть. Замена через replace, а не format:
        в промпте могут быть фигурные скобки (JSON-примеры)."""
        if not prompt.strip():
            raise ValueError("Пустой промпт")
        user = prompt
        if transcript_text is not None:
            user = (prompt.replace(TRANSCRIPT_PLACEHOLDER, transcript_text)
                    if TRANSCRIPT_PLACEHOLDER in prompt
                    else f"{prompt}\n\nТранскрипт:\n{transcript_text}")
        started = time.perf_counter()
        system = system or ""
        text = (self._llm.generate(system, user) if json_schema is None
                else self._llm.generate(system, user, json_schema=json_schema))
        return PromptResult(text, model_name_of(self._llm), round((time.perf_counter() - started) * 1000, 2))
