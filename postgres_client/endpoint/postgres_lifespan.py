"""PostgreSQL client lifespan"""

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from postgres_client.client.postgres_client import PostgresClient
from postgres_client.client.postgres_connection import (
    create_postgres_engine,
    verify_postgres_connection,
)
from postgres_client.endpoint.postgres_settings import get_postgres_settings

try:
    from observability.logging.setup import get_logger

    logger = get_logger(__name__)
except ImportError:
    logger = logging.getLogger(__name__)


@asynccontextmanager
async def postgres_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Lifespan для подключения роутера PostgreSQL к FastAPI-приложению.

    Создаёт Engine и PostgresClient при старте, сохраняет их в app.state.
    Спаны логирования те же, что у redis_lifespan ([START] / [STEP] / [END]
    с duration_ms): в общем логе запуска обе зависимости должны выглядеть
    одинаково, иначе по логу непонятно, дошёл ли старт до PostgreSQL вообще.
    """
    logger.info("[START] PostgreSQL client")
    pg_start = time.perf_counter()
    settings = get_postgres_settings()

    if settings.pg_connection_url:
        # Корпоративный контур: DSN без пароля, возможно с двумя хостами.
        # Пароль в лог не попадает — печатаем только сам DSN из настроек.
        logger.info(
            "[STEP] PostgreSQL: corporate DSN mode. Schema: %s, URL: %s",
            settings.pg_db_schema,
            settings.pg_connection_url,
        )
    else:
        logger.info(
            "[STEP] PostgreSQL: direct connection mode. Host: %s, Port: %s, DB: %s",
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
        )

    engine = create_postgres_engine(
        settings,
        pool_size=settings.postgres_pool_size,
        max_overflow=settings.postgres_max_overflow,
        pool_recycle=settings.postgres_pool_recycle,
        pool_timeout=settings.postgres_pool_timeout,
        db_schema=settings.pg_db_schema,
    )
    logger.info(
        "[STEP] PostgreSQL: connection pool created. Size: %s, Overflow: %s",
        settings.postgres_pool_size,
        settings.postgres_max_overflow,
    )

    if not verify_postgres_connection(engine):
        logger.error(
            "[END] PostgreSQL client: connection check failed",
            duration_ms=round((time.perf_counter() - pg_start) * 1000, 2),
            _external_service="postgres",
            _external_operation="init",
        )
        raise RuntimeError(
            "Failed to connect to PostgreSQL. "
            "Check DB_CONNECTION_URL / POSTGRES_* settings."
        )

    client = PostgresClient(engine)
    app.state.postgres_client = client

    logger.info(
        "[END] PostgreSQL client",
        duration_ms=round((time.perf_counter() - pg_start) * 1000, 2),
        _external_service="postgres",
        _external_operation="init",
    )

    yield

    logger.info("PostgreSQL lifespan: Shutting down.")
    engine.dispose()
    logger.info("PostgreSQL lifespan: Connection pool closed.")
