"""
Конфигурация structlog для структурированного логирования.

Этот модуль — только склейка: подключает процессоры, handlers, каналы.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import structlog

from observability.logging.processors import (
    add_request_context,
    add_service_info,
    rename_event_to_message,
    ensure_field_order,
)
from observability.logging.filters import (
    HealthCheckFilter,
    LogLevelCounterFilter,
)

if TYPE_CHECKING:
    from observability.config.logging_config import LoggingSettings

def setup_logging(settings: LoggingSettings) -> None:
    """
    Настройка structlog для приложения.

    Parameters
    ----------
    settings : LoggingSettings
        Настройки логирования (уровень, формат, каналы)
    """
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_request_context,
        add_service_info,
        rename_event_to_message,
        ensure_field_order,
    ]

    renderer: Any
    if settings.log_development:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    elif settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.processors.KeyValueRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    # Счётчик логов по уровню (метрика log_messages_total) — считаем ровно то,
    # что уходит в stdout (этот handler шарится и с uvicorn-логгерами).
    handler.addFilter(LogLevelCounterFilter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, settings.log_level.upper()))

    # Uvicorn: единый формат для всех логов
    for uvicorn_logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = False

    # Фильтруем healthcheck запросы
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    # Уменьшаем шум от HTTP библиотек
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("niquests").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Получить структурированный логгер.

    Parameters
    ----------
    name : str
        Имя логгера (обычно __name__)
    """
    return structlog.get_logger(name)
