import re
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from postgres_client.endpoint.postgres_entities import (
    ColumnDefinition,
    CreateTableRequest,
    CreateTableResponse,
    DropTableResponse,
    PostgresHealthResponse,
)
from postgres_client.endpoint.postgres_lifespan import postgres_lifespan
from postgres_client.endpoint.postgres_settings import get_postgres_settings

postgres_router = APIRouter(
    prefix="/postgres",
    tags=["PostgreSQL"],
    lifespan=postgres_lifespan,
)


def _get_client(req: Request):
    client = getattr(req.app.state, "postgres_client", None)
    if client is None:
        raise HTTPException(status_code=500, detail="PostgreSQL client not initialized")
    return client


def _get_engine(req: Request):
    return _get_client(req).engine


@postgres_router.get("/health", response_model=PostgresHealthResponse)
async def postgres_health(req: Request):
    client = _get_client(req)
    settings = get_postgres_settings()
    if not client.ping():
        raise HTTPException(status_code=503, detail="PostgreSQL unavailable")

    # Derive host/database from corporate URL when available, else from fallback fields.
    if settings.pg_connection_url:
        # Strip scheme to safely split off userinfo and query string.
        # URL may be multi-host: postgresql://user@h1:5432,h2:5432/db?app=...
        try:
            from urllib.parse import urlparse
            parsed = urlparse(settings.pg_connection_url)
            # netloc may be "user@h1:5432,h2:5432" — take first host only
            hostpart = parsed.hostname or (parsed.netloc.split("@")[-1].split(",")[0].split(":")[0])
            port = parsed.port or 5432
            database = parsed.path.lstrip("/").split("?")[0] or settings.postgres_db
        except Exception:
            hostpart, port, database = settings.postgres_host, settings.postgres_port, settings.postgres_db
    else:
        hostpart, port, database = settings.postgres_host, settings.postgres_port, settings.postgres_db

    return PostgresHealthResponse(
        status="ok",
        host=hostpart,
        port=port,
        database=database,
    )


_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")

# Safe SQL type whitelist — prevents injection via the type field.
_ALLOWED_TYPES = {
    "text", "integer", "int", "bigint", "smallint",
    "boolean", "bool", "float", "real", "double precision",
    "numeric", "date", "timestamp", "timestamptz",
    "jsonb", "json", "uuid",
    "serial", "bigserial", "smallserial",
}
_VARCHAR_RE = re.compile(r"^varchar\(\d{1,6}\)$")


def _resolve_sql_type(type_str: str) -> str:
    """Validate and normalise a column type string."""
    t = type_str.strip().lower()
    if t in _ALLOWED_TYPES:
        return t.upper() if t not in ("text", "jsonb", "json", "uuid") else t
    if _VARCHAR_RE.match(t):
        return t.upper()
    raise ValueError(
        f"Unsupported column type '{type_str}'. "
        f"Allowed: {sorted(_ALLOWED_TYPES)} or varchar(N)."
    )


def _validate_table_name(name: str) -> None:
    if not _TABLE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid table name '{name}'. Use only letters, digits and underscores.",
        )


def _validate_column_name(name: str) -> None:
    if not _TABLE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid column name '{name}'. Use only letters, digits and underscores.",
        )


@postgres_router.delete("/table/{table_name}", response_model=DropTableResponse)
async def postgres_drop_table(table_name: str, req: Request):
    """
    DROP TABLE IF EXISTS <table_name> CASCADE.
    Имя таблицы валидируется regex-ом — только буквы, цифры и подчёркивание.
    """
    _validate_table_name(table_name)
    engine = _get_engine(req)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to drop table '{table_name}': {exc}",
        )
    return DropTableResponse(
        table_name=table_name,
        message=f"Table '{table_name}' dropped (CASCADE).",
    )


@postgres_router.post("/table", response_model=CreateTableResponse)
async def postgres_create_table(body: CreateTableRequest, req: Request):
    """
    CREATE TABLE с произвольной структурой колонок.

    Используется для создания таблиц из ноутбука без изменения models.py.
    Использует чистый SQL DDL — не затрагивает SQLAlchemy ORM MetaData,
    поэтому не конфликтует со структурами, определёнными в models.py.

    Пример тела запроса:
    {
      "table_name": "my_results",
      "columns": [
        {"name": "id",          "type": "bigserial", "primary_key": true},
        {"name": "external_id", "type": "integer",   "nullable": false},
        {"name": "result",      "type": "text"},
        {"name": "score",       "type": "float",     "default": "0.0"},
        {"name": "created_at",  "type": "timestamp", "default": "now()"}
      ],
      "if_not_exists": true
    }
    """
    _validate_table_name(body.table_name)
    if not body.columns:
        raise HTTPException(status_code=422, detail="columns list must not be empty.")

    col_parts: list[str] = []
    for col in body.columns:
        _validate_column_name(col.name)
        try:
            sql_type = _resolve_sql_type(col.type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        parts = [f'"{col.name}"', sql_type]
        if col.primary_key:
            parts.append("PRIMARY KEY")
        else:
            if not col.nullable:
                parts.append("NOT NULL")
            if col.unique:
                parts.append("UNIQUE")
        if col.default is not None:
            # Whitelist: allow only safe default expressions
            if not re.match(r"^[\w\s\(\)\.\'\"\-\+\*\/,]+$", col.default):
                raise HTTPException(
                    status_code=422,
                    detail=f"Unsafe default expression for column '{col.name}': {col.default!r}",
                )
            parts.append(f"DEFAULT {col.default}")
        col_parts.append(" ".join(parts))

    exists_clause = "IF NOT EXISTS " if body.if_not_exists else ""
    ddl = (
        f'CREATE TABLE {exists_clause}"{body.table_name}" (\n'
        + ",\n".join(f"  {p}" for p in col_parts)
        + "\n)"
    )

    engine = _get_engine(req)
    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create table '{body.table_name}': {exc}",
        )

    return CreateTableResponse(
        table_name=body.table_name,
        columns=[col.name for col in body.columns],
        ddl=ddl,
        message=f"Table '{body.table_name}' created with {len(body.columns)} columns.",
    )
