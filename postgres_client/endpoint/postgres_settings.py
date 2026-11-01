from functools import lru_cache
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresSettings(BaseSettings):
    # ── K8s / production — corporate naming (DB__*) ───────────────────────────
    # DB_CONNECTION_URL: DSN without password, e.g.
    #   postgresql://svc_user@tcp-proxy:5432,replica:5432/intent_db?application_name=batch-pipeline
    pg_connection_url: str | None = Field(default=None, alias="DB_CONNECTION_URL")
    # DB_DB_SCHEMA: search_path applied at connection time (default "public")
    pg_db_schema: str = Field(default="public", alias="DB_DB_SCHEMA")
    # DB_PASSWORD: Helm secret, never stored in configMap; substituted at runtime
    pg_password: SecretStr | None = Field(default=None, alias="DB_PASSWORD")
    # DB_PREPING_CONNECTION: run SELECT 1 before yielding connection from pool
    pg_preping_connection: bool = Field(default=True, alias="DB_PREPING_CONNECTION")

    # ── Local / dev fallback (POSTGRES_*) ─────────────────────────────────────
    # Used only when DB_CONNECTION_URL is not set.
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(default="postgres", alias="POSTGRES_USER")
    postgres_password: str = Field(default="postgres", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="intent_db", alias="POSTGRES_DB")

    # ── SQLAlchemy connection pool ─────────────────────────────────────────────
    postgres_pool_size: int = Field(default=5, alias="POSTGRES_POOL_SIZE")
    postgres_max_overflow: int = Field(default=10, alias="POSTGRES_MAX_OVERFLOW")
    postgres_pool_recycle: int = Field(default=3600, alias="POSTGRES_POOL_RECYCLE")
    postgres_pool_timeout: int = Field(default=30, alias="POSTGRES_POOL_TIMEOUT")

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache()
def get_postgres_settings() -> PostgresSettings:
    return PostgresSettings()
