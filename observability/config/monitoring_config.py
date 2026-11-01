"""
Настройки мониторинга (Prometheus метрики).

Env-переменные с префиксом MONITORING_.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MonitoringSettings(BaseSettings):
    """Настройки мониторинга."""

    enabled: bool = Field(
        default=True,
        description="Включить сбор метрик",
    )
    prefix: str = Field(
        default="rag_service",
        description="Префикс для метрик",
    )
    service_name: str = Field(
        default="rag-service",
        description="Имя сервиса (для метрики service_info)",
    )

    model_config = SettingsConfigDict(
        env_prefix="MONITORING_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_monitoring_settings() -> MonitoringSettings:
    return MonitoringSettings()
