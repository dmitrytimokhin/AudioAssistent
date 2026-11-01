"""Middleware для FastAPI."""

from observability.middleware.fastapi_middleware import (
    ObservabilityMiddleware,
    setup_observability_middleware,
)

__all__ = ["ObservabilityMiddleware", "setup_observability_middleware"]
