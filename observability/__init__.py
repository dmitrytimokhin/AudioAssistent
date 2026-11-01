"""
Единая точка входа для системы observability.

Модуль предоставляет:
- Структурированное логирование (structlog)
- Метрики (prometheus_client)
- Контекст запроса для сквозной трассировки
- Middleware для FastAPI

Использование:
    from observability import init_observability, get_logger, request_context

    # При старте приложения
    init_observability()

    # В коде
    logger = get_logger(__name__)
    with request_context(trace_id="abc"):
        logger.info("Processing", key="value")
"""

from observability.config import (
    get_logging_settings,
    get_tracing_settings,
    get_monitoring_settings,
)
from observability.logging.setup import (
    setup_logging,
    get_logger,
)
from observability.metrics.collectors import METRICS_REGISTRY, set_service_info
from observability.metrics.endpoint import metrics_router
from observability.metrics.decorators import track_external_service
from observability.context import (
    RequestContext,
    get_context,
    set_context,
    clear_context,
    request_context,
)
from observability.middleware.fastapi_middleware import setup_observability_middleware
from observability.tracing import setup_tracing, get_tracer

__all__ = [
    # Config
    "get_logging_settings",
    "get_tracing_settings",
    "get_monitoring_settings",
    # Logging
    "setup_logging",
    "get_logger",
    # Metrics
    "METRICS_REGISTRY",
    "set_service_info",
    "metrics_router",
    "track_external_service",
    # Context
    "RequestContext",
    "get_context",
    "set_context",
    "clear_context",
    "request_context",
    # Tracing
    "setup_tracing",
    "get_tracer",
    # Middleware
    "setup_observability_middleware",
    # Init
    "init_observability",
]


def init_observability() -> None:
    """
    Инициализация всей системы observability.

    Вызывать один раз при старте приложения, ДО создания логгеров.
    """
    logging_settings = get_logging_settings()
    tracing_settings = get_tracing_settings()
    monitoring_settings = get_monitoring_settings()

    # 1. Настройка логирования
    setup_logging(logging_settings)

    logger = get_logger(__name__)
    logger.info("[RUN_APP] [START] Application initialization")

    # 2. Установка информации о сервисе для метрик
    if monitoring_settings.enabled:
        set_service_info(
            version="1.0.0",
            environment=logging_settings.workspace,
            service_name=monitoring_settings.service_name,
        )

    # 3. Настройка трассировки (OpenTelemetry)
    if tracing_settings.enabled:
        setup_tracing(
            service_name=tracing_settings.service_name,
            service_version="1.0.0",
            environment=logging_settings.workspace,
            otlp_endpoint=tracing_settings.otlp_endpoint,
            console_export=tracing_settings.console_export,
        )

    logger.info(
        "[STEP] Observability initialized",
        service_name=monitoring_settings.service_name,
        workspace=logging_settings.workspace,
        log_level=logging_settings.log_level,
        log_format=logging_settings.log_format,
        monitoring_enabled=monitoring_settings.enabled,
        tracing_enabled=tracing_settings.enabled,
    )
