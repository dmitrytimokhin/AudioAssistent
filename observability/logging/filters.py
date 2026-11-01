"""
Фильтры для логирования.

Содержит фильтры для исключения определённых запросов из логов,
например healthcheck запросов от Kubernetes.
"""

from __future__ import annotations

import logging
from typing import Sequence

from observability.metrics.collectors import log_messages_total


class PathExclusionFilter(logging.Filter):
    """
    Базовый фильтр: исключает лог-записи, содержащие указанные пути.
    """

    def __init__(self, paths: set[str], name: str = "") -> None:
        super().__init__(name)
        self._paths = paths

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._paths:
            return True
        message = record.getMessage()
        return not any(path in message for path in self._paths)


DEFAULT_HEALTH_PATHS = {
    "/health",
    "/healthz",
    "/readiness",
    "/ready",
    "/liveness",
    "/livez",
    "/metrics",
}


class HealthCheckFilter(PathExclusionFilter):
    """
    Фильтр для исключения healthcheck запросов из логов.

    Предотвращает засорение логов запросами от Kubernetes probes
    и Prometheus scraping.

    Использование:
        handler.addFilter(HealthCheckFilter())
    """

    def __init__(
        self,
        name: str = "",
        health_paths: Sequence[str] | None = None,
    ) -> None:
        paths = set(DEFAULT_HEALTH_PATHS)
        if health_paths:
            paths.update(health_paths)
        super().__init__(paths, name)


class EndpointFilter(PathExclusionFilter):
    """
    Фильтр для исключения определённых эндпоинтов из логов.

    Использование:
        handler.addFilter(EndpointFilter(excluded_paths=["/internal", "/debug"]))
    """

    def __init__(
        self,
        name: str = "",
        excluded_paths: Sequence[str] | None = None,
    ) -> None:
        paths = set(excluded_paths) if excluded_paths else set()
        super().__init__(paths, name)


class LogLevelCounterFilter(logging.Filter):
    """
    Считает каждую лог-запись по уровню (метрика log_messages_total).

    Не фильтрует — всегда пропускает запись дальше (возвращает True).
    Вешается на stdout-handler, чтобы считать ровно те логи, что
    реально уходят в stdout.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        log_messages_total.labels(level=record.levelname.lower()).inc()
        return True