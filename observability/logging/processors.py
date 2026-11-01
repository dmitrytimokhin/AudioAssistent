"""
Structlog процессоры для обогащения лог-записей.
"""

import logging
import os
import socket
from typing import Any, MutableMapping

from observability.context import get_context
from observability.logging.constants import BASE_CONTEXT_FIELDS, FIELD_ORDER


def add_request_context(
    logger: logging.Logger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """
    Добавляет базовый контекст запроса из RequestContext.

    Добавляет только поля из BASE_CONTEXT_FIELDS и только если их нет.
    """
    ctx = get_context()
    if ctx:
        for key, value in ctx.to_log_dict().items():
            if key in BASE_CONTEXT_FIELDS and key not in event_dict:
                event_dict[key] = value
    return event_dict


def add_service_info(
    logger: logging.Logger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Добавляет информацию о сервисе и хосте."""
    if "service_id" not in event_dict:
        event_dict["service_id"] = os.getenv("SERVICE_NAME", "rag-service")
    if "host" not in event_dict:
        event_dict["host"] = os.getenv("POD_NAME", socket.gethostname())
    return event_dict


def rename_event_to_message(
    logger: logging.Logger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Переименовывает 'event' в 'message' для совместимости с корпоративными системами."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def ensure_field_order(
    logger: logging.Logger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """
    Гарантирует фиксированный порядок полей в JSON.

    Порядок определён в FIELD_ORDER. Остальные поля по алфавиту.
    """
    ordered: dict[str, Any] = {}
    for key in FIELD_ORDER:
        if key in event_dict:
            ordered[key] = event_dict.pop(key)
    for key in sorted(event_dict.keys()):
        ordered[key] = event_dict[key]
    return ordered
