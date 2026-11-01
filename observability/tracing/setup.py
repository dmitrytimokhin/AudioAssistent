"""
Настройка OpenTelemetry tracing.

Tracing работает параллельно с существующими логами и метриками.
Spans экспортируются только если настроен otlp_endpoint.
"""

import os
from functools import lru_cache

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION

from observability.logging.setup import get_logger

logger = get_logger(__name__)


def setup_tracing(
    service_name: str = "rag-service",
    service_version: str = "1.0.0",
    environment: str = "development",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> None:
    """
    Инициализация OpenTelemetry tracing.

    Parameters
    ----------
    service_name : str
        Имя сервиса для трейсов
    service_version : str
        Версия сервиса
    environment : str
        Окружение (development, production)
    otlp_endpoint : str | None
        OTLP endpoint для экспорта spans (например, http://jaeger:4317)
    console_export : bool
        Выводить spans в консоль (для отладки)
    """
    # Resource с метаданными сервиса
    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
            "deployment.environment": environment,
            "host.name": os.getenv("POD_NAME", os.getenv("HOSTNAME", "unknown")),
        }
    )

    # Создаём provider
    provider = TracerProvider(resource=resource)

    # Console exporter (для отладки)
    if console_export:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("[STEP] Tracing console export enabled")

    # OTLP exporter
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))

            logger.info(
                "[STEP] Tracing enabled",
                otlp_endpoint=otlp_endpoint,
                service_name=service_name,
            )
        except Exception as e:
            logger.warning(
                "Failed to setup OTLP exporter, tracing disabled",
                error=str(e),
            )
    else:
        logger.info(
            "[STEP] Tracing initialized (no exporter)",
            service_name=service_name,
        )

    # Устанавливаем глобальный provider
    trace.set_tracer_provider(provider)


@lru_cache()
def get_tracer(name: str = "rag-service") -> trace.Tracer:
    """
    Получить tracer для создания spans.

    Parameters
    ----------
    name : str
        Имя tracer (обычно имя модуля или сервиса)

    Returns
    -------
    trace.Tracer
        OpenTelemetry tracer

    Пример
    ------
    ```python
    from observability.tracing import get_tracer

    tracer = get_tracer(__name__)

    with tracer.start_as_current_span("my-operation") as span:
        span.set_attribute("key", "value")
        do_something()
    ```
    """
    return trace.get_tracer(name)
