import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from audio_assistant.client.bootstrap import build_history, build_pipeline
from audio_assistant.client.llm_adapter import LoggedLLMClient, OllamaLLMAdapter
from audio_assistant.client.ports import NullCallLog, NullRunStore
from audio_assistant.client.storage.repositories import (
    PostgresCallLog,
    PostgresHistoryRepository,
    PostgresRunStore,
    init_storage,
)
from audio_assistant.endpoint.assistant_settings import get_assistant_settings
from observability.logging.setup import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def assistant_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Собирает пайплайн и кладёт его в app.state.assistant_pipeline.

    Требует app.state.ollama_client (и app.state.postgres_client при
    ASSISTANT_HISTORY_BACKEND=postgres): роутеры Ollama и PostgreSQL должны быть
    подключены ДО assistant_router. Whisper-модель загружается лениво, при
    первой транскрибации, поэтому старт сервиса быстрый.
    """
    logger.info("[START] Audio assistant")
    started = time.perf_counter()
    settings = get_assistant_settings()

    ollama = getattr(app.state, "ollama_client", None)
    if ollama is None:
        raise RuntimeError("assistant_router requires ollama_router to be included first")

    if settings.history_backend == "postgres":
        pg = getattr(app.state, "postgres_client", None)
        if pg is None:
            raise RuntimeError("assistant_router requires postgres_router to be included first")
        init_storage(pg)
        history, store, call_log = (
            PostgresHistoryRepository(pg), PostgresRunStore(pg), PostgresCallLog(pg))
    else:
        history, store, call_log = build_history(settings), NullRunStore(), NullCallLog()
        logger.warning("[STEP] History backend = file: журнал запусков и вызовов LLM не пишется")

    llm = LoggedLLMClient(
        OllamaLLMAdapter(ollama, ollama.settings.temperature, ollama.settings.max_tokens),
        call_log,
    )
    app.state.assistant_pipeline = build_pipeline(settings, llm, history, store)

    logger.info(
        "[END] Audio assistant",
        history_backend=settings.history_backend,
        transcriber=settings.transcriber_backend,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    yield
    logger.info("Audio assistant lifespan: shutting down.")
