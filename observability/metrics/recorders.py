"""
Запись результата вызова внешнего сервиса в метрики.

Общая точка для декоратора track_external_service. Здесь нет OpenTelemetry
и logging: модуль импортируется из самого логирования, и любая запись в лог
отсюда дала бы рекурсию.
"""

from observability.metrics.collectors import (
    external_service_availability,
    external_service_duration_seconds,
    external_service_error_class_total,
    external_service_errors_total,
    external_service_requests_total,
)

# Серии предсоздаются нулями: иначе increase()/rate() не заметит самую первую
# ошибку (серии ещё нет). Ключ — сервис, значение — основная операция.
PREINIT_SERVICES = {
    "ollama": "chat",
    "whisper": "transcribe",
    "postgres": "ping",
}


def classify_error(exc: BaseException) -> str:
    """
    Грубый класс ошибки с малой кардинальностью — для алертов и дашбордов.

    Определяется по имени класса и атрибутам, без импорта httpx/sqlalchemy/redis:
    observability не должен зависеть от клиентов. Идёт по цепочке __cause__,
    поэтому обёрнутое исключение (TransientLLMError из HTTPStatusError)
    классифицируется по первопричине.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 5:
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            if status in (401, 403):
                return "auth"
            if status == 429:
                return "http_429"
            if status >= 500:
                return "http_5xx"
            if status >= 400:
                return "http_4xx"

        names = {cls.__name__ for cls in type(current).__mro__}
        if "PermanentContentError" in names:
            return "content"
        if "QwenAuthError" in names:
            return "auth"
        if isinstance(current, TimeoutError) or any("Timeout" in n for n in names):
            # sqlalchemy.exc.TimeoutError — исчерпан пул, а не сеть
            return "db_pool" if "QueuePool" in str(current) else "timeout"
        if "OperationalError" in names:
            return "db_connection"
        if names & {"ProgrammingError", "IntegrityError", "DataError", "DBAPIError", "InternalError"}:
            return "db_query"
        if isinstance(current, OSError) or any("Connect" in n for n in names):
            return "connect"
        current = current.__cause__
        seen += 1
    return "other"


def record_external_call(
    service: str,
    operation: str,
    duration: float,
    error: BaseException | None = None,
) -> None:
    """Учитывает один вызов внешнего сервиса: счётчики, длительность, доступность."""
    if error is None:
        external_service_requests_total.labels(
            service=service, operation=operation, status="success"
        ).inc()
        external_service_duration_seconds.labels(
            service=service, operation=operation
        ).observe(duration)
        external_service_availability.labels(service=service).set(1)
        return

    external_service_requests_total.labels(
        service=service, operation=operation, status="error"
    ).inc()
    external_service_errors_total.labels(
        service=service, operation=operation, error_type=type(error).__name__
    ).inc()
    external_service_error_class_total.labels(
        service=service, operation=operation, error_class=classify_error(error)
    ).inc()
    external_service_availability.labels(service=service).set(0)


def _preinit() -> None:
    """Заводит нулевые серии, чтобы панели не пустовали до первого вызова."""
    for service, operation in PREINIT_SERVICES.items():
        for status in ("success", "error"):
            external_service_requests_total.labels(
                service=service, operation=operation, status=status
            )


_preinit()
