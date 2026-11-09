"""Подключение ollama_client к порту LLMClient + журналирование вызовов."""
from __future__ import annotations

import base64
import time

from observability.logging.setup import get_logger

from .domain import LLMCallRecord
from .ports import CallLog, LLMClient
from .run_context import current_audience, current_person, current_run_id

logger = get_logger(__name__)


class OllamaLLMAdapter:
    """Адаптер ollama_client.OllamaClient -> LLMClient.generate(system, user)."""

    def __init__(self, client, temperature: float, max_tokens: int, model_name: str | None = None):
        self._client = client
        self._temperature, self._max_tokens = temperature, max_tokens
        self.model_name = model_name or client.settings.model_name

    # Структурированные ответы (разметка говорящих) длиннее обычного статуса
    STRUCTURED_MIN_TOKENS = 2048

    def generate(self, system: str, user: str, json_schema: dict | None = None,
                 images: list[bytes] | None = None) -> str:
        # Ollama может быть недоступна на старте: коннект проверяется лениво
        if not self._client.is_connected and not self._client.connect():
            raise RuntimeError("Ollama unavailable")
        max_tokens = self._max_tokens if json_schema is None else max(self._max_tokens, self.STRUCTURED_MIN_TOKENS)
        return self._client.query(
            instruction=system, prompt=user, temperature=self._temperature,
            max_tokens=max_tokens, model_name=self.model_name, json_schema=json_schema,
            images=[base64.b64encode(i).decode() for i in images] if images else None,
        )


class LoggedLLMClient:
    """Декоратор над LLMClient: пишет каждый вызов (успех и ошибку) в CallLog."""

    def __init__(self, inner: LLMClient, call_log: CallLog, model_name: str | None = None):
        self._inner, self._call_log = inner, call_log
        self._model = model_name or getattr(inner, "model_name", "")

    def generate(self, system: str, user: str, json_schema: dict | None = None,
                 images: list[bytes] | None = None) -> str:
        started = time.perf_counter()
        response, error = None, None
        extra = {k: v for k, v in (("json_schema", json_schema), ("images", images)) if v}
        try:
            response = self._inner.generate(system, user, **extra)   # узкие LLM-заглушки не знают лишних параметров
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            logged_user = f"{user}\n[изображений: {len(images)}]" if images else user
            self._record(system, logged_user, response, error, started)

    def _record(self, system: str, user: str, response: str | None, error: str | None,
                started: float) -> None:
        call = LLMCallRecord(
            audience=current_audience.get(), model=self._model,
            system_prompt=system, user_prompt=user,
            status="error" if error else "success",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            response=response, error=error,
            full_name=current_person.get(), run_id=current_run_id.get(),
        )
        try:
            self._call_log.record(call)
        except Exception as exc:  # журнал не должен ронять основной поток
            logger.error("Failed to record LLM call", error=str(exc))
