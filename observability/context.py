"""
Управление контекстом запроса для сквозной трассировки.

Использует contextvars для async-safe хранения контекста.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional
import time
import os


@dataclass
class RequestContext:
    """
    Контекст запроса для логирования и трассировки.

    Base поля — универсальные, нужны в любом проекте:
    - trace_id: сквозной идентификатор запроса
    - service_id: идентификатор сервиса
    - host: хост обработки (POD_NAME)
    - start_time: время начала обработки

    Проектные поля (channel, model_id, file_id и т.д.) —
    передаются через extra: dict.
    """

    trace_id: str
    service_id: str = "rag-service"
    host: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        """Заполняет host из переменной окружения если не указан."""
        if self.host is None:
            self.host = os.getenv("POD_NAME", os.getenv("HOSTNAME", "unknown"))

    def to_dict(self) -> dict:
        """Преобразует контекст в словарь для логирования."""
        result = {
            "trace_id": self.trace_id,
            "service_id": self.service_id,
            "host": self.host,
        }
        result.update(self.extra)
        return result

    def to_log_dict(self) -> dict:
        """Возвращает только непустые поля для логирования."""
        return {k: v for k, v in self.to_dict().items() if v is not None}


# ContextVar для async-safe хранения
_request_context: ContextVar[Optional[RequestContext]] = ContextVar(
    "request_context", default=None
)


def get_context() -> Optional[RequestContext]:
    """Получить текущий контекст запроса."""
    return _request_context.get()


def set_context(ctx: RequestContext) -> None:
    """Установить контекст запроса."""
    _request_context.set(ctx)


def clear_context() -> None:
    """Очистить контекст запроса."""
    _request_context.set(None)


class request_context:
    """
    Context manager для автоматического управления контекстом.

    Использование:
        with request_context(trace_id="abc", extra={"file_id": "f123"}):
            logger.info("Processing request")

        async with request_context(trace_id="abc"):
            await process_something()
    """

    def __init__(self, **kwargs):
        self.ctx = RequestContext(**kwargs)
        self._token = None

    def __enter__(self) -> RequestContext:
        self._token = _request_context.set(self.ctx)
        return self.ctx

    def __exit__(self, *args):
        if self._token is not None:
            _request_context.reset(self._token)

    async def __aenter__(self) -> RequestContext:
        return self.__enter__()

    async def __aexit__(self, *args):
        self.__exit__(*args)
