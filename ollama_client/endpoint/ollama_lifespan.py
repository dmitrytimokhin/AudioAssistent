import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from ollama_client.client.ollama_client import OllamaClient
from ollama_client.endpoint.ollama_settings import get_ollama_settings

try:
    from observability.logging.setup import get_logger
    logger = get_logger(__name__)
except ImportError:
    logger = logging.getLogger(__name__)


@asynccontextmanager
async def ollama_lifespan(app: FastAPI):
    """
    Lifespan для Ollama-роутера.
    Создаёт клиент и сохраняет его в app.state.ollama_client.

    Подключение НЕ проверяется при старте: Ollama может быть недоступна вне
    work-window (см. batch_coordinator.py). Фактический connect() выполняется
    лениво в phase_logic._get_ollama_client() перед первым вызовом LLM —
    к тому моменту PipelineSupervisor уже гарантирует открытое окно.
    """
    settings = get_ollama_settings()
    client = OllamaClient(settings)
    app.state.ollama_client = client

    # Недоступность Ollama на старте НЕ роняет приложение: сервис должен
    # подняться и стучаться к модели, пока она не ответит (то же поведение,
    # что у корпоративного yallm_lifespan). Повторный connect() делается лениво
    # перед каждым вызовом — см. phase_logic_local._get_ollama_client.
    if client.connect():
        logger.info(
            "[STEP] Ollama client connected (url=%s, model=%s)",
            settings.ollama_url,
            settings.model_name,
        )
    else:
        logger.warning(
            "[STEP] Ollama unavailable at startup (url=%s) -- will retry on first call",
            settings.ollama_url,
        )
    yield