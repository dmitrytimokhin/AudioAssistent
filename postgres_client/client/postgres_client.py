from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy import text

from observability.logging.setup import get_logger
from observability.metrics.decorators import track_external_service

logger = get_logger(__name__)


class PostgresClient:
    """Тонкая обёртка вокруг SQLAlchemy Engine — не знает ни о каких доменных моделях."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def init_schema(self, base: type[DeclarativeBase]) -> None:
        """Создаёт таблицы указанной декларативной базы, если их ещё нет."""
        base.metadata.create_all(self.engine)
        logger.info("DB schema initialized.")

    def session(self) -> Session:
        return Session(self.engine)

    @track_external_service("postgres", "ping")
    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
