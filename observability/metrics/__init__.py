"""Модуль метрик на prometheus_client."""

from observability.metrics.collectors import (
    METRICS_REGISTRY,
    http_requests_total,
    http_request_duration_seconds,
    http_requests_in_progress,
    http_errors_total,
    stream_messages_received_total,
    stream_message_attempts_total,
    stream_messages_completed_total,
    stream_message_processing_duration_seconds,
    stream_message_queue_time_seconds,
    stream_queue_size,
    stream_retries_total,
    stream_max_retries_exceeded_total,
    external_service_requests_total,
    external_service_duration_seconds,
    external_service_errors_total,
    external_service_availability,
    external_service_error_class_total,
    set_service_info,
)
from observability.metrics.recorders import record_external_call
from observability.metrics.decorators import track_external_service

__all__ = [
    "METRICS_REGISTRY",
    "track_external_service",
    "http_requests_total",
    "http_request_duration_seconds",
    "http_requests_in_progress",
    "http_errors_total",
    "stream_messages_received_total",
    "stream_message_attempts_total",
    "stream_messages_completed_total",
    "stream_message_processing_duration_seconds",
    "stream_message_queue_time_seconds",
    "stream_queue_size",
    "stream_retries_total",
    "stream_max_retries_exceeded_total",
    "external_service_requests_total",
    "external_service_duration_seconds",
    "external_service_errors_total",
    "external_service_availability",
    "external_service_error_class_total",
    "record_external_call",
    "set_service_info",
]
