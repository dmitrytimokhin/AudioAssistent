"""
Константы для системы логирования.
"""

# Поля контекста, которые добавляются автоматически в каждый лог
BASE_CONTEXT_FIELDS = {"trace_id", "channel", "redis_id"}

# Фиксированный порядок полей в JSON
FIELD_ORDER = [
    "timestamp",
    "level",
    "trace_id",
    "service_id",
    "host",
    "channel",
    "redis_id",
    "logger",
    "message",
]
