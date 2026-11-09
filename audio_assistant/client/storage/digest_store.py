"""Хранилище сводок (digests): ход построения, строки таблицы, имя xlsx-файла."""
from __future__ import annotations

from datetime import date

from sqlalchemy import delete, func, select

from postgres_client.client.postgres_client import PostgresClient

from .models import Digest


class PostgresDigestStore:
    def __init__(self, client: PostgresClient):
        self._client = client

    @staticmethod
    def _dict(d: Digest, with_rows: bool = True) -> dict:
        data = {"id": d.id, "name": d.name, "title": d.title,
                "date_from": d.date_from.isoformat(), "date_to": d.date_to.isoformat(),
                "kinds": list(d.kinds or []), "tags": list(d.tags or []), "source": d.source,
                "status": d.status, "error": d.error, "done": d.done, "total": d.total,
                "file_name": d.file_name, "rows_count": len(d.rows or []),
                "created_at": d.created_at.isoformat(), "updated_at": d.updated_at.isoformat()}
        if with_rows:
            data.update(headers=list(d.headers or []), meetings=list(d.meetings or []), rows=list(d.rows or []))
        return data

    def _get(self, session, digest_id: int) -> Digest:
        digest = session.get(Digest, digest_id)
        if digest is None:
            raise LookupError(f"Сводка {digest_id} не найдена")
        return digest

    def create(self, name: str, title: str, date_from: date, date_to: date, kinds: list[str],
               tags: list[str], source: str, total: int, headers: list[str]) -> int:
        with self._client.session() as session:
            digest = Digest(name=name, title=title, date_from=date_from, date_to=date_to,
                            kinds=kinds, tags=tags, source=source, total=total, headers=headers)
            session.add(digest)
            session.commit()
            return digest.id

    def add_meeting(self, digest_id: int, meeting: dict, rows: list[dict]) -> None:
        """Результат по одной встрече: пересобираем списки целиком (JSON-колонки не отслеживают append)."""
        with self._client.session() as session:
            digest = self._get(session, digest_id)
            digest.meetings = [*(digest.meetings or []), meeting]
            digest.rows = [*(digest.rows or []), *rows]
            digest.done += 1
            session.commit()

    def finish(self, digest_id: int, file_name: str | None) -> None:
        with self._client.session() as session:
            digest = self._get(session, digest_id)
            digest.status, digest.file_name = "done", file_name
            session.commit()

    def fail(self, digest_id: int, error: str) -> None:
        with self._client.session() as session:
            digest = self._get(session, digest_id)
            digest.status, digest.error = "error", error
            session.commit()

    def fail_interrupted(self) -> int:
        with self._client.session() as session:
            running = session.scalars(select(Digest).where(Digest.status == "running")).all()
            for digest in running:
                digest.status, digest.error = "error", "Прервано перезапуском сервиса, постройте сводку заново"
            session.commit()
            return len(running)

    def get(self, digest_id: int) -> dict:
        with self._client.session() as session:
            return self._dict(self._get(session, digest_id))

    def list(self, limit: int = 20) -> list[dict]:
        with self._client.session() as session:
            rows = session.scalars(select(Digest).order_by(Digest.id.desc()).limit(limit)).all()
            return [self._dict(d, with_rows=False) for d in rows]

    def delete(self, *, digest_id: int | None = None, everything: bool = False,
               dry_run: bool = False) -> dict:
        with self._client.session() as session:
            query = select(Digest.id)
            if not everything:
                query = query.where(Digest.id == digest_id)
            ids = list(session.scalars(query))
            files = [f for f in session.scalars(select(Digest.file_name).where(Digest.id.in_(ids))) if f]
            if ids and not dry_run:
                session.execute(delete(Digest).where(Digest.id.in_(ids)))
                session.commit()
            return {"digest_ids": ids, "files": files, "dry_run": dry_run}

    def count(self) -> int:
        with self._client.session() as session:
            return session.scalar(select(func.count()).select_from(Digest))
