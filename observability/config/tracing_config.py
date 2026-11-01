"""
Настройки трассировки (OpenTelemetry).

Env-переменные с префиксом TRACING_.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TracingSettings(BaseSettings):
    """Настройки трассировки."""

    enabled: bool = Field(
        default=True,
        description="Включить трассировку",
    )
    service_name: str = Field(
        default="rag-service",
        description="Имя сервиса (для OpenTelemetry Resource)",
    )
    console_export: bool = Field(
        default=False,
        description="Выводить spans в консоль (для отладки)",
    )
    otlp_endpoint: Optional[str] = Field(
        default=None,
        description="OTLP endpoint для экспорта spans",
    )

    @field_validator("otlp_endpoint", mode="before")
    @classmethod
    def _check_otel_env(cls, v: Optional[str]) -> Optional[str]:
        """Поддержка стандартной переменной OTEL_EXPORTER_OTLP_ENDPOINT."""
        if v is not None:
            return v
        return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    model_config = SettingsConfigDict(
        env_prefix="TRACING_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_tracing_settings() -> TracingSettings:
    return TracingSettings()
