"""
Декораторы для автоматического сбора метрик и трассировки.
"""

import asyncio
import functools
import time
from typing import Callable, TypeVar, ParamSpec

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from observability.metrics.recorders import record_external_call

P = ParamSpec("P")
T = TypeVar("T")


def track_external_service(
    service: str,
    operation: str | None = None,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """
    Декоратор для отслеживания вызовов внешних сервисов.

    Собирает метрики и создаёт OpenTelemetry spans:
    - Количество вызовов (success/error)
    - Время выполнения
    - Ошибки с типизацией
    - Доступность сервиса
    - OpenTelemetry span с атрибутами

    Parameters
    ----------
    service : str
        Имя сервиса (s3, opensearch, llm)
    operation : str | None
        Название операции (get_object, search, generate).
        Если не указано — используется имя сервиса (метрики без разбивки по операциям).

    Usage
    -----
    @track_external_service("s3", "get_object")   # с разбивкой по операции
    @track_external_service("opensearch")         # только по сервису (operation=service)
    async def get_file_from_s3(bucket: str, key: str) -> bytes:
        ...
    """
    operation = operation or service
    tracer = trace.get_tracer(__name__)
    span_name = f"{service}.{operation}"

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("service", service)
                span.set_attribute("operation", operation)

                start_time = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    duration = time.perf_counter() - start_time

                    record_external_call(service, operation, duration)

                    span.set_attribute("duration_ms", round(duration * 1000, 2))
                    span.set_status(Status(StatusCode.OK))

                    return result

                except Exception as e:
                    duration = time.perf_counter() - start_time
                    error_type = type(e).__name__

                    record_external_call(service, operation, duration, error=e)

                    span.set_attribute("error_type", error_type)
                    span.set_attribute("error_message", str(e)[:200])
                    span.set_status(Status(StatusCode.ERROR, str(e)[:100]))
                    span.record_exception(e)

                    raise

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("service", service)
                span.set_attribute("operation", operation)

                start_time = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    duration = time.perf_counter() - start_time

                    record_external_call(service, operation, duration)

                    span.set_attribute("duration_ms", round(duration * 1000, 2))
                    span.set_status(Status(StatusCode.OK))

                    return result

                except Exception as e:
                    duration = time.perf_counter() - start_time
                    error_type = type(e).__name__

                    record_external_call(service, operation, duration, error=e)

                    span.set_attribute("error_type", error_type)
                    span.set_attribute("error_message", str(e)[:200])
                    span.set_status(Status(StatusCode.ERROR, str(e)[:100]))
                    span.record_exception(e)

                    raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator
