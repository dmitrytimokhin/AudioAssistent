"""Хранилище разборов картинок (image_analyses)."""
from __future__ import annotations

from sqlalchemy import delete, select

from postgres_client.client.postgres_client import PostgresClient

from .models import ImageAnalysis


class PostgresImageStore:
    def __init__(self, client: PostgresClient):
        self._client = client

    @staticmethod
    def _dict(a: ImageAnalysis) -> dict:
        return {"id": a.id, "images": list(a.images or []), "prompt_name": a.prompt_name,
                "system_prompt": a.system_prompt, "prompt": a.prompt, "model": a.model,
                "tags": list(a.tags or []), "text": a.text, "duration_ms": a.duration_ms,
                "created_at": a.created_at.isoformat()}

    def save(self, *, images: list[dict], prompt_name: str | None, system_prompt: str, prompt: str,
             model: str, tags: list[str], text: str, duration_ms: float) -> int:
        with self._client.session() as session:
            row = ImageAnalysis(images=images, prompt_name=prompt_name, system_prompt=system_prompt,
                                prompt=prompt, model=model, tags=tags, text=text, duration_ms=duration_ms)
            session.add(row)
            session.commit()
            return row.id

    def get(self, analysis_id: int) -> dict:
        with self._client.session() as session:
            row = session.get(ImageAnalysis, analysis_id)
            if row is None:
                raise LookupError(f"Разбор картинок {analysis_id} не найден")
            return self._dict(row)

    def list(self, limit: int = 20, tag: str | None = None, file_name: str | None = None) -> list[dict]:
        """Свежие первыми. Теги и файл фильтруются в Python: JSON-операторы различаются между СУБД."""
        with self._client.session() as session:
            rows = [self._dict(r) for r in session.scalars(select(ImageAnalysis).order_by(ImageAnalysis.id.desc()))]
        if tag:
            rows = [r for r in rows if tag in r["tags"]]
        if file_name:
            rows = [r for r in rows if any(i["file"] == file_name for i in r["images"])]
        return rows[:limit]

    def delete(self, *, analysis_id: int | None = None, everything: bool = False, dry_run: bool = False) -> dict:
        with self._client.session() as session:
            query = select(ImageAnalysis.id)
            if not everything:
                query = query.where(ImageAnalysis.id == analysis_id)
            ids = list(session.scalars(query))
            if ids and not dry_run:
                session.execute(delete(ImageAnalysis).where(ImageAnalysis.id.in_(ids)))
                session.commit()
            return {"analysis_ids": ids, "dry_run": dry_run}
