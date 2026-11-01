"""
Метрики приложения на prometheus_client.

Все метрики собираются в отдельный registry для возможности
экспорта в разные системы мониторинга.
"""

from prometheus_client import Counter, Histogram, Gauge, Info
from prometheus_client import CollectorRegistry


# Создаем отдельный registry для возможности экспорта в разные системы
METRICS_REGISTRY = CollectorRegistry()


# ============================================================================
# HTTP Метрики (FastAPI)
# ============================================================================

http_requests_total = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    labelnames=["method", "endpoint", "status_code"],
    registry=METRICS_REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=METRICS_REGISTRY,
)

http_requests_in_progress = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed",
    labelnames=["method", "endpoint"],
    registry=METRICS_REGISTRY,
)

http_errors_total = Counter(
    "http_errors_total",
    "Total number of HTTP errors by type (4xx, 5xx)",
    labelnames=["method", "endpoint", "error_type"],
    registry=METRICS_REGISTRY,
)


# ============================================================================
# Redis Streams Метрики
# ============================================================================

stream_messages_received_total = Counter(
    "stream_messages_received_total",
    "Unique messages received from Redis Streams (first attempt only, not retries)",
    labelnames=["stream", "channel"],
    registry=METRICS_REGISTRY,
)

stream_message_attempts_total = Counter(
    "stream_message_attempts_total",
    "Processing attempts: success (processed OK), error (processing failed)",
    labelnames=["stream", "channel", "status"],
    registry=METRICS_REGISTRY,
)

stream_messages_completed_total = Counter(
    "stream_messages_completed_total",
    "Final outcome per unique message: success, timeout (SLA exceeded), max_retries (all attempts failed)",
    labelnames=["stream", "channel", "status"],
    registry=METRICS_REGISTRY,
)

stream_message_processing_duration_seconds = Histogram(
    "stream_message_processing_duration_seconds",
    "Message processing duration in seconds (calc_time)",
    labelnames=["stream", "channel"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
    registry=METRICS_REGISTRY,
)

stream_message_queue_time_seconds = Histogram(
    "stream_message_queue_time_seconds",
    "Time message spent in queue before processing",
    labelnames=["stream", "channel"],
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=METRICS_REGISTRY,
)

stream_queue_size = Gauge(
    "stream_queue_size",
    "Current queue size by type (new, pending, total)",
    labelnames=["stream", "queue_type"],
    registry=METRICS_REGISTRY,
)

stream_retries_total = Counter(
    "stream_retries_total",
    "Total number of message retries",
    labelnames=["stream", "channel"],
    registry=METRICS_REGISTRY,
)

stream_max_retries_exceeded_total = Counter(
    "stream_max_retries_exceeded_total",
    "Messages that exceeded max retries",
    labelnames=["stream", "channel"],
    registry=METRICS_REGISTRY,
)


# ============================================================================
# Бизнес-метрики пайплайна
# ============================================================================

translator_responses_sent_total = Counter(
    "translator_responses_sent_total",
    "Responses delivered to translator, by channel and outcome",
    labelnames=["channel", "status"],  # status: success | error
    registry=METRICS_REGISTRY,
)


# ============================================================================
# Логи (счётчик по уровню)
# ============================================================================

log_messages_total = Counter(
    "log_messages_total",
    "Log records emitted to stdout, by level",
    labelnames=["level"],  # info | warning | error | debug | critical
    registry=METRICS_REGISTRY,
)


# ============================================================================
# Внешние Сервисы (S3, OpenSearch, LLM)
# ============================================================================

external_service_requests_total = Counter(
    "external_service_requests_total",
    "Total requests to external services",
    labelnames=["service", "operation", "status"],
    registry=METRICS_REGISTRY,
)

external_service_duration_seconds = Histogram(
    "external_service_duration_seconds",
    "External service request duration in seconds",
    labelnames=["service", "operation"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=METRICS_REGISTRY,
)

external_service_errors_total = Counter(
    "external_service_errors_total",
    "Total errors from external services by error type",
    labelnames=["service", "operation", "error_type"],
    registry=METRICS_REGISTRY,
)

external_service_availability = Gauge(
    "external_service_availability",
    "External service availability (1 = available, 0 = unavailable)",
    labelnames=["service"],
    registry=METRICS_REGISTRY,
)


external_service_error_class_total = Counter(
    "external_service_error_class_total",
    "External service errors by class (http_429, http_5xx, timeout, connect, db_connection, ...)",
    labelnames=["service", "operation", "error_class"],
    registry=METRICS_REGISTRY,
)


# ============================================================================
# Info метрика (для metadata)
# ============================================================================

service_info = Info(
    "service",
    "Service information",
    registry=METRICS_REGISTRY,
)


def set_service_info(
    version: str,
    environment: str,
    service_name: str = "rag-service",
) -> None:
    """Устанавливает информацию о сервисе."""
    service_info.info({
        "version": version,
        "environment": environment,
        "service_name": service_name,
    })
