"""
Точка входа сервиса AudioAssistent: аудио/текст встречи -> статус для руководства.

init_observability() вызывается ПЕРВЫМ, до импорта роутеров: он настраивает
structlog, метрики и трассировку. Любой модуль, взявший логгер раньше этого
вызова, получил бы логгер с настройками по умолчанию.

Порядок подключения роутеров ЗНАЧИМ: каждый следующий читает app.state,
выставленный предыдущим (lifespan роутера выполняется в порядке include_router).

    postgres_router  → app.state.postgres_client
    ollama_router    → app.state.ollama_client
    assistant_router → читает оба, поднимает пайплайн (app.state.assistant_pipeline)

Хранилище выбирается ASSISTANT_HISTORY_BACKEND: postgres (по умолчанию) | file.
При file postgres_router не подключается (PostgreSQL не нужен), но и журнала
запусков/вызовов LLM не будет.
"""

import os

from observability import get_logger, init_observability, metrics_router

init_observability()

from fastapi import FastAPI  # noqa: E402  (только после init_observability)

from audio_assistant.endpoint.assistant_router import assistant_router  # noqa: E402
from audio_assistant.endpoint.assistant_settings import get_assistant_settings  # noqa: E402
from observability import setup_observability_middleware  # noqa: E402
from ollama_client.endpoint.ollama_router import ollama_router  # noqa: E402
from postgres_client.endpoint.postgres_router import postgres_router  # noqa: E402

logger = get_logger(__name__)

# Имя сервиса в middleware и метриках. Берётся из той же переменной, что и
# service_id в логах, — иначе в трёх местах оказались бы три разных имени.
SERVICE_NAME = os.getenv("SERVICE_NAME", "audio-assistant")
SERVICE_VERSION = "1.0.0"

app = FastAPI(
    title="Audio Assistant",
    description="Транскрибация встреч (Whisper), атрибуция говорящих по именам и статусы дейли (Ollama) с историей в PostgreSQL",
    version=SERVICE_VERSION,
)

# Пути, которые не должны попадать ни в логи, ни в HTTP-метрики: пробы k8s,
# скрейп Prometheus и дешёвые статусные ручки. Список передаётся ЯВНО: у
# middleware свой набор по умолчанию, и в нём нет вложенных путей вроде
# /healthz/liveness — probes писали бы по две строки на каждый запрос.
EXCLUDED_PATHS = [
    "/health", "/healthz", "/healthz/liveness", "/healthz/readiness",
    "/readiness", "/ready", "/liveness", "/livez",
    "/metrics", "/metrics/json",
    "/postgres/health", "/ollama/health", "/assistant/health",
]

# Без middleware не будет ни X-Trace-ID в ответах, ни HTTP-метрик.
setup_observability_middleware(app, service_id=SERVICE_NAME, exclude_paths=EXCLUDED_PATHS)

if get_assistant_settings().history_backend == "postgres":
    app.include_router(postgres_router)
app.include_router(ollama_router)
app.include_router(assistant_router)
app.include_router(metrics_router)


# ── Пробы k8s ───────────────────────────────────────────────────────────────
# Маршруты объявляются здесь: observability их не отдаёт, в нём только списки
# путей, которые исключаются из логов (logging/filters.py) и из middleware.


@app.get("/healthz/liveness", tags=["Service"])
async def route_liveness_probe() -> dict[str, str]:
    """Liveness: отвечает, пока жив процесс."""
    return {"details": "OK"}


@app.get("/healthz/readiness", tags=["Service"])
async def route_readiness_probe() -> dict[str, str]:
    """Readiness: готовность принимать трафик. Пайплайн — в /assistant/health."""
    return {"details": "OK"}


@app.get("/health", tags=["Service"])
async def health() -> dict[str, str]:
    """Короткий алиас liveness-пробы."""
    return {"status": "ok"}


logger.info(
    "[STEP] FastAPI application assembled",
    service=SERVICE_NAME,
    version=SERVICE_VERSION,
)
