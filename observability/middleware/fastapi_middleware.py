"""
Middleware для автоматического сбора метрик и трассировки HTTP запросов.
"""

import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from observability.context import RequestContext, set_context, clear_context
from observability.metrics.collectors import (
    http_requests_total,
    http_request_duration_seconds,
    http_requests_in_progress,
    http_errors_total,
)
from observability.logging.setup import get_logger

logger = get_logger(__name__)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Middleware для сбора метрик и трассировки HTTP запросов.

    Функции:
    1. Создает RequestContext с trace_id
    2. Извлекает trace_id из заголовков (если есть)
    3. Собирает метрики (RPS, latency, errors)
    4. Логирует запрос/ответ
    5. Добавляет X-Trace-ID в ответ
    """

    # Заголовки для трассировки (совместимость с разными системами)
    TRACE_HEADERS = [
        "X-Trace-ID",
        "X-Request-ID",
        "X-Correlation-ID",
        "traceparent",  # W3C Trace Context
    ]

    def __init__(
        self,
        app: ASGIApp,
        service_id: str = "rag-service",
        exclude_paths: list[str] | None = None,
    ):
        super().__init__(app)
        self.service_id = service_id
        self.exclude_paths = exclude_paths or [
            "/health",
            "/healthz",
            "/readiness",
            "/ready",
            "/liveness",
            "/metrics",
            "/metrics/json",
        ]

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        # Пропускаем служебные endpoints
        if request.url.path in self.exclude_paths:
            return await call_next(request)

        # Извлекаем или генерируем trace_id
        trace_id = None
        for header in self.TRACE_HEADERS:
            trace_id = request.headers.get(header)
            if trace_id:
                break

        if not trace_id:
            trace_id = str(uuid.uuid4())

        # Извлекаем другие метаданные из запроса
        client_id = request.headers.get("X-Client-ID")
        session_id = request.headers.get("X-Session-ID")
        caller_info = request.headers.get(
            "X-Caller-Service", request.headers.get("User-Agent", "unknown")
        )

        # Создаем контекст запроса
        ctx = RequestContext(
            trace_id=trace_id,
            service_id=self.service_id,
            extra={
                "client_id": client_id,
                "session_id": session_id,
                "caller_info": caller_info,
            },
        )
        set_context(ctx)

        # Метрики
        method = request.method
        endpoint = request.url.path

        http_requests_in_progress.labels(method=method, endpoint=endpoint).inc()
        start_time = time.perf_counter()

        try:
            # Логируем входящий запрос
            logger.info(
                "HTTP request started",
                method=method,
                endpoint=endpoint,
            )

            # Выполняем запрос
            response = await call_next(request)

            # Вычисляем время
            duration = time.perf_counter() - start_time
            status_code = response.status_code

            # Записываем метрики
            http_requests_total.labels(
                method=method,
                endpoint=endpoint,
                status_code=status_code,
            ).inc()

            http_request_duration_seconds.labels(
                method=method,
                endpoint=endpoint,
            ).observe(duration)

            # Записываем ошибки по типам
            if 400 <= status_code < 500:
                http_errors_total.labels(
                    method=method,
                    endpoint=endpoint,
                    error_type="4xx",
                ).inc()
            elif status_code >= 500:
                http_errors_total.labels(
                    method=method,
                    endpoint=endpoint,
                    error_type="5xx",
                ).inc()

            # Логируем завершение запроса
            logger.info(
                "HTTP request completed",
                method=method,
                endpoint=endpoint,
                status_code=status_code,
                duration_ms=round(duration * 1000, 2),
            )

            # Добавляем trace_id в заголовки ответа
            response.headers["X-Trace-ID"] = trace_id

            return response

        except Exception as e:
            duration = time.perf_counter() - start_time

            http_errors_total.labels(
                method=method,
                endpoint=endpoint,
                error_type="5xx",
            ).inc()

            logger.exception(
                "HTTP request failed",
                method=method,
                endpoint=endpoint,
                error_type=type(e).__name__,
                error_message=str(e),
                duration_ms=round(duration * 1000, 2),
            )

            raise

        finally:
            http_requests_in_progress.labels(method=method, endpoint=endpoint).dec()
            clear_context()


def setup_observability_middleware(
    app: FastAPI,
    service_id: str = "rag-service",
    exclude_paths: list[str] | None = None,
) -> None:
    """
    Добавляет middleware для observability к FastAPI приложению.

    Parameters
    ----------
    app : FastAPI
        FastAPI приложение
    service_id : str
        Идентификатор сервиса
    exclude_paths : list[str] | None
        Пути для исключения из трассировки
    """
    app.add_middleware(
        ObservabilityMiddleware,
        service_id=service_id,
        exclude_paths=exclude_paths,
    )
