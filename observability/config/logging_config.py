"""
Настройки логирования (stdout, structlog).

Env-переменные:
- LOG_LEVEL, LOG_FORMAT — без префикса
- LOGGING_* — форматирование
- NAMESPACE, POD_NAME — инфраструктурные, без префикса
"""

import os
import socket
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LoggingSettings(BaseSettings):
    """Настройки логирования."""

    # Общие (без префикса LOGGING_)
    log_level: str = Field(
        default="INFO",
        description="Уровень логирования (DEBUG, INFO, WARNING, ERROR)",
    )
    log_format: Literal["json", "console", "keyvalue"] = Field(
        default="json",
        description="Формат логов (json для prod, console для dev)",
    )
    log_development: bool = Field(
        default=False,
        description="Режим разработки (цветной вывод в консоль)",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _read_log_level(cls, v: str) -> str:
        """Поддержка LOG_LEVEL без префикса."""
        return os.getenv("LOG_LEVEL", v)

    @field_validator("log_format", mode="before")
    @classmethod
    def _read_log_format(cls, v: str) -> str:
        """Поддержка LOG_FORMAT без префикса."""
        return os.getenv("LOG_FORMAT", v)

    # Контекст сервиса, добавляется в каждую запись
    app_name: str = Field(
        default_factory=lambda: os.getenv("APP_NAME", "rag-service"),
        description="Имя приложения. Читается из APP_NAME.",
    )
    namespace: str = Field(
        default_factory=lambda: os.getenv("NAMESPACE", ""),
        description="Namespace Kubernetes (читается из NAMESPACE)",
    )
    hostname: str = Field(
        default_factory=lambda: os.getenv("POD_NAME", socket.gethostname()),
        description="Имя хоста/пода",
    )
    workspace: str = Field(
        default_factory=lambda: os.getenv("WORKSPACE", "dev"),
        description="Окружение (dev, ift, preprod, prod, ss). Читается из WORKSPACE.",
    )

    model_config = SettingsConfigDict(
        env_prefix="LOGGING_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_logging_settings() -> LoggingSettings:
    return LoggingSettings()
