"""
Endpoints для экспорта метрик.
"""

from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from observability.metrics.collectors import METRICS_REGISTRY

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics")
async def metrics() -> Response:
    """
    Endpoint для экспорта метрик в формате Prometheus.

    Используется для scraping системой мониторинга.

    Returns
    -------
    Response
        Метрики в формате Prometheus text/plain
    """
    return Response(
        content=generate_latest(METRICS_REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


@metrics_router.get("/metrics/json")
async def metrics_json() -> dict:
    """
    Endpoint для экспорта метрик в JSON формате.

    Альтернативный формат для корпоративных систем,
    которые не поддерживают Prometheus формат.

    Returns
    -------
    dict
        Метрики в JSON формате
    """
    from prometheus_client.parser import text_string_to_metric_families

    metrics_output = generate_latest(METRICS_REGISTRY).decode("utf-8")

    result: dict = {}
    for family in text_string_to_metric_families(metrics_output):
        for sample in family.samples:
            name = sample.name
            labels = sample.labels
            value = sample.value

            if name not in result:
                result[name] = []

            result[name].append(
                {
                    "labels": labels,
                    "value": value,
                }
            )

    return result
