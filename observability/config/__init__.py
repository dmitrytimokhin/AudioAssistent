"""
Конфигурация observability — отдельные настройки для каждого домена.
"""

from observability.config.logging_config import LoggingSettings, get_logging_settings
from observability.config.tracing_config import TracingSettings, get_tracing_settings
from observability.config.monitoring_config import (
    MonitoringSettings,
    get_monitoring_settings,
)

__all__ = [
    "LoggingSettings",
    "get_logging_settings",
    "TracingSettings",
    "get_tracing_settings",
    "MonitoringSettings",
    "get_monitoring_settings",
]
