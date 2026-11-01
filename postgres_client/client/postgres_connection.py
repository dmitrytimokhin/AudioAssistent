import re
import time

import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from observability.logging.setup import get_logger

logger = get_logger(__name__)


def _corporate_url_to_kwargs(url: str, password: str | None = None) -> dict:
    """
    Parse a corporate PostgreSQL URL (possibly multi-host) into psycopg2.connect() kwargs.

    Input:  postgresql://user@h1:5432,h2:5432/db?application_name=svc
    Output: {"host": "h1,h2", "port": "5432,5432", "user": "user",
             "dbname": "db", "application_name": "svc", "password": "..."}

    psycopg2 natively accepts comma-separated host/port values for failover —
    the same behaviour the corporate psycopg_pool uses.
    """
    rest = re.sub(r"^postgresql(\+\w+)?://", "", url)

    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)

    dbname = ""
    if "/" in rest:
        rest, dbname = rest.rsplit("/", 1)

    user = ""
    if "@" in rest:
        userinfo, rest = rest.rsplit("@", 1)
        user = userinfo.split(":")[0]  # ignore any embedded password in URL

    hosts, ports = [], []
    for part in rest.split(","):
        part = part.strip()
        if ":" in part:
            h, p = part.rsplit(":", 1)
            hosts.append(h)
            ports.append(p)
        else:
            hosts.append(part)
            ports.append("5432")

    kwargs: dict = {
        "host": ",".join(hosts),
        "port": ",".join(ports),
        "user": user,
        "dbname": dbname,
    }
    if password:
        kwargs["password"] = password

    # Forward extra query params (application_name, connect_timeout, sslmode, …)
    if query:
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k != "password":
                    kwargs[k] = v

    return kwargs


def _is_multihost_url(url: str) -> bool:
    """Return True when the URL contains comma-separated hosts."""
    try:
        rest = re.sub(r"^postgresql(\+\w+)?://", "", url)
        if "?" in rest:
            rest = rest.split("?")[0]
        if "/" in rest:
            rest = rest.rsplit("/", 1)[0]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        return "," in rest
    except Exception:
        return False


def _psycopg2_url(url: str) -> str:
    """
    Явно фиксирует драйвер psycopg2.

    Для "postgresql://" SQLAlchemy 2.1+ выбирает psycopg (v3), которого в образе
    нет: везде в этом модуле используется psycopg2 (multi-host creator — тоже).
    """
    return re.sub(r"^postgresql://", "postgresql+psycopg2://", url)


def build_postgres_url(settings) -> str:
    """
    Return a displayable connection string (no embedded password).

    Used for logging / health endpoint. Pass settings directly to
    create_postgres_engine() — it handles password injection and multi-host.
    """
    if settings.pg_connection_url:
        return settings.pg_connection_url
    return (
        f"postgresql://{settings.postgres_user}:***"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def create_postgres_engine(
    settings,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_recycle: int = 3600,
    pool_timeout: int = 30,
    db_schema: str | None = None,
) -> Engine:
    """
    Create a SQLAlchemy Engine from PostgresSettings.

    Corporate / K8s (DB_CONNECTION_URL set):
      • Single-host URL  → standard create_engine() + password as query-param.
      • Multi-host URL   → psycopg2.connect(**kwargs) via creator= so SQLAlchemy
                           never tries to parse the comma-separated host list.
                           Behaviour is identical to the corporate psycopg_pool.

    Local dev (POSTGRES_* fallback):
      • Standard create_engine() with individual POSTGRES_* fields.
    """
    # Resolve schema connect_arg once
    schema_opts: dict = {}
    if db_schema and db_schema != "public":
        schema_opts["options"] = f"-csearch_path={db_schema},public"

    pool_kwargs = dict(
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle,
        pool_timeout=pool_timeout,
    )

    if settings.pg_connection_url:
        password = (
            settings.pg_password.get_secret_value()
            if settings.pg_password
            else None
        )
        url = settings.pg_connection_url

        if _is_multihost_url(url):
            # ── Corporate multi-host path ─────────────────────────────────────
            # SQLAlchemy cannot parse "host1:p,host2:p" — bypass URL parsing
            # entirely and hand psycopg2 the raw kwargs (same as pim_framework).
            connect_kwargs = _corporate_url_to_kwargs(url, password)
            connect_kwargs.update(schema_opts)
            # Per-host TCP connect timeout: psycopg2 tries hosts in order and
            # without this waits for the OS TCP timeout (~120s) on a dead host
            # before trying the next one. 5s means failover in ≤5s per host.
            connect_kwargs.setdefault("connect_timeout", 5)

            def creator():
                """
                Открывает соединение, перебирая хосты ПО ОЧЕРЕДИ и логируя
                каждую попытку.

                psycopg2 умеет перебирать comma-separated host сам, но делает
                это молча: в журнале видно только итог, и после инцидента
                нельзя сказать, какой хост отказал и когда произошло
                переключение. Поэтому перебор делаем здесь — поведение то же,
                а каждый шаг попадает в журнал и в канал внешних вызовов.
                """
                hosts = str(connect_kwargs.get("host", "")).split(",")
                ports = str(connect_kwargs.get("port", "")).split(",")
                last_error: Exception | None = None

                for index, host in enumerate(hosts):
                    host = host.strip()
                    port = (ports[index] if index < len(ports) else ports[-1]).strip()
                    single = {**connect_kwargs, "host": host, "port": port}
                    started = time.perf_counter()
                    try:
                        conn = psycopg2.connect(**single)
                    except psycopg2.OperationalError as exc:
                        last_error = exc
                        logger.error(
                            "[postgres][ERROR] Host unavailable",
                            host=host,
                            port=port,
                            attempt=index + 1,
                            of_hosts=len(hosts),
                            error=str(exc).strip(),
                            _external_service="postgres",
                            _external_operation="connect",
                        )
                        if index + 1 < len(hosts):
                            logger.warning(
                                "[postgres][FAILOVER] Switching PostgreSQL host",
                                failed_host=f"{host}:{port}",
                                new_host=hosts[index + 1].strip(),
                                _external_service="postgres",
                                _external_operation="failover",
                            )
                        continue

                    logger.info(
                        "[postgres][STEP] Connection established",
                        host=host,
                        port=port,
                        # Не первый по списку — значит основной хост не ответил
                        # и мы работаем на резервном. Это главный признак того,
                        # что failover сработал.
                        primary=index == 0,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        _external_service="postgres",
                        _external_operation="connect",
                    )
                    return conn

                logger.error(
                    "[postgres][ERROR] All PostgreSQL hosts unavailable",
                    hosts=connect_kwargs.get("host"),
                    _external_service="postgres",
                    _external_operation="connect",
                )
                raise last_error  # type: ignore[misc]

            logger.info(
                "PostgreSQL engine: multi-host creator mode",
                hosts=connect_kwargs.get("host"),
                dbname=connect_kwargs.get("dbname"),
            )
            return create_engine(
                "postgresql+psycopg2://",
                creator=creator,
                **pool_kwargs,
            )
        else:
            # ── Corporate single-host path ────────────────────────────────────
            if password:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}password={password}"
            return create_engine(_psycopg2_url(url), connect_args=schema_opts, **pool_kwargs)

    # ── Local dev fallback ────────────────────────────────────────────────────
    url = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )
    return create_engine(_psycopg2_url(url), connect_args=schema_opts, **pool_kwargs)


def verify_postgres_connection(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("PostgreSQL connection successful.")
        return True
    except Exception as exc:
        logger.error("Failed to connect to PostgreSQL: %s", exc)
        return False
