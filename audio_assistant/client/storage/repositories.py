"""PostgreSQL-реализации портов: история статусов, хранилище запусков по стадиям, журнал вызовов LLM."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select

from observability.logging.setup import get_logger
from postgres_client.client.postgres_client import PostgresClient

from ..domain import DailySummary, LLMCallRecord, PersonStatus, Segment, Transcript
from ..run_context import current_run_id
from .models import (Base, LLMCall, MeetingRun, PersonStatusRecord, Report, SpeakerUtterance,
                     TranscriptRecord)

logger = get_logger(__name__)


def init_storage(client: PostgresClient) -> None:
    client.init_schema(Base)


class PostgresHistoryRepository:
    """HistoryRepository: статусы сотрудников по датам и аудиториям (person_statuses)."""

    def __init__(self, client: PostgresClient):
        self._client = client

    def save(self, summary: DailySummary) -> None:
        """Статусы запуска; повторная суммаризация (run_id, audience) заменяет прежние."""
        run_id = current_run_id.get()
        with self._client.session() as session:
            if run_id is not None:
                session.execute(delete(PersonStatusRecord).where(
                    PersonStatusRecord.run_id == run_id, PersonStatusRecord.audience == summary.audience))
            session.add_all(
                PersonStatusRecord(
                    run_id=run_id, meeting_date=summary.day, audience=summary.audience,
                    full_name=st.full_name, project_id=st.project_id, text=st.text,
                    has_data=st.has_data,
                )
                for st in summary.statuses
            )
            session.commit()

    def load_summary(self, run_id: int, audience: str) -> DailySummary | None:
        with self._client.session() as session:
            rows = session.scalars(
                select(PersonStatusRecord)
                .where(PersonStatusRecord.run_id == run_id, PersonStatusRecord.audience == audience)
                .order_by(PersonStatusRecord.id)).all()
        if not rows:
            return None
        return DailySummary(rows[0].meeting_date, audience,
                            [PersonStatus(r.full_name, r.project_id, r.text, r.has_data) for r in rows])

    def previous_status(self, full_name: str, before: date, audience: str) -> str | None:
        """Последний статус с данными до даты; ту же аудиторию предпочитаем, иначе любая."""
        with self._client.session() as session:
            rows = session.execute(
                select(PersonStatusRecord.audience, PersonStatusRecord.meeting_date,
                       PersonStatusRecord.id, PersonStatusRecord.text)
                .where(PersonStatusRecord.full_name == full_name,
                       PersonStatusRecord.meeting_date < before,
                       PersonStatusRecord.has_data.is_(True))
                .order_by(PersonStatusRecord.meeting_date.desc(), PersonStatusRecord.id.desc())
            ).all()
        same = [r for r in rows if r.audience == audience]
        chosen = (same or rows or [None])[0]
        return chosen.text if chosen else None


class PostgresRunStore:
    """RunStore: запуск, транскрипты, реплики по говорящим, отчёты и стадия обработки."""

    supports_stages = True

    def __init__(self, client: PostgresClient):
        self._client = client

    # ── жизненный цикл ───────────────────────────────────────────────────
    def start(self, day: date, source: str, recorded_at: datetime | None = None) -> int:
        with self._client.session() as session:
            run = MeetingRun(meeting_date=day, source=source, recorded_at=recorded_at)
            session.add(run)
            session.commit()
            return run.id

    def run_day(self, run_id: int) -> date:
        return self._get_run(run_id).meeting_date

    def set_stage(self, run_id: int | None, stage: str) -> None:
        self._update_run(run_id, status=stage, error=None)

    def fail(self, run_id: int | None, error: str) -> None:
        self._update_run(run_id, status="error", error=error)

    def _update_run(self, run_id: int | None, **fields) -> None:
        if run_id is None:
            return
        with self._client.session() as session:
            run = session.get(MeetingRun, run_id)
            if run is None:
                return
            for k, v in fields.items():
                setattr(run, k, v)
            session.commit()

    def _get_run(self, run_id: int) -> MeetingRun:
        with self._client.session() as session:
            run = session.get(MeetingRun, run_id)
        if run is None:
            raise LookupError(f"Запуск {run_id} не найден")
        return run

    # ── транскрипты и реплики ─────────────────────────────────────────────
    def save_raw(self, run_id: int | None, transcript: Transcript) -> None:
        self._save_transcript(run_id, "raw", transcript)

    def save_attributed(self, run_id: int | None, transcript: Transcript) -> None:
        if run_id is None:
            return
        self._save_transcript(run_id, "attributed", transcript)
        turns = transcript.merge_turns().segments
        with self._client.session() as session:
            session.execute(delete(SpeakerUtterance).where(SpeakerUtterance.run_id == run_id))
            session.add_all(
                SpeakerUtterance(run_id=run_id, position=i, full_name=t.speaker,
                                 start_sec=t.start, end_sec=t.end, text=t.text)
                for i, t in enumerate(turns))
            session.commit()

    def _save_transcript(self, run_id: int | None, kind: str, transcript: Transcript) -> None:
        if run_id is None:
            return
        with self._client.session() as session:
            session.execute(delete(TranscriptRecord).where(
                TranscriptRecord.run_id == run_id, TranscriptRecord.kind == kind))
            session.add(TranscriptRecord(run_id=run_id, kind=kind,
                                         segments=transcript.to_dict()["segments"]))
            session.commit()

    def load_raw(self, run_id: int) -> Transcript:
        return self._load_transcript(run_id, "raw", "сначала выполните транскрибацию")

    def load_attributed(self, run_id: int) -> Transcript:
        return self._load_transcript(run_id, "attributed", "сначала выполните стадию attribute")

    def _load_transcript(self, run_id: int, kind: str, hint: str) -> Transcript:
        self._get_run(run_id)
        with self._client.session() as session:
            rec = session.scalar(select(TranscriptRecord).where(
                TranscriptRecord.run_id == run_id, TranscriptRecord.kind == kind))
        if rec is None:
            raise LookupError(f"У запуска {run_id} нет транскрипта '{kind}': {hint}")
        return Transcript([Segment(**s) for s in rec.segments], source=self._get_run(run_id).source)

    # ── отчёты ───────────────────────────────────────────────────────────
    def save_report(self, run_id: int | None, audience: str, text: str) -> None:
        if run_id is None:
            return
        with self._client.session() as session:
            session.execute(delete(Report).where(Report.run_id == run_id, Report.audience == audience))
            session.add(Report(run_id=run_id, audience=audience, text=text))
            session.commit()

    # ── удаление ─────────────────────────────────────────────────────────
    def delete(self, *, run_id: int | None = None, file_name: str | None = None,
               meeting_date: date | None = None, everything: bool = False,
               dry_run: bool = False) -> dict:
        """Удаляет запуски и ВСЕ связанные строки (транскрипты, реплики, статусы, отчёты, журнал LLM).

        Фильтры run_id / file_name / meeting_date комбинируются через AND; everything=True
        очищает все таблицы целиком, включая строки без run_id (вызовы LLM при save=false).
        Дочерние строки удаляются явно, а не каскадом: у person_statuses и llm_calls
        внешний ключ SET NULL, и без этого статусы удалённой встречи остались бы в «истории»
        и попали бы в промпт следующего дня. dry_run=True только считает.
        """
        children = [(LLMCall, "llm_calls"), (PersonStatusRecord, "person_statuses"),
                    (Report, "reports"), (SpeakerUtterance, "speaker_utterances"),
                    (TranscriptRecord, "transcripts")]
        with self._client.session() as session:
            ids: list[int] | None = None
            if not everything:
                query = select(MeetingRun.id)
                if run_id is not None:
                    query = query.where(MeetingRun.id == run_id)
                if file_name is not None:
                    query = query.where(MeetingRun.source == file_name)
                if meeting_date is not None:
                    query = query.where(MeetingRun.meeting_date == meeting_date)
                ids = list(session.scalars(query))
                if not ids:
                    return {"run_ids": [], "deleted": {"meeting_runs": 0}, "dry_run": dry_run}

            def scoped(model, column):
                return (model, column.in_(ids)) if ids is not None else (model, None)

            counts: dict[str, int] = {}
            for model, name in children:
                _, cond = scoped(model, model.run_id)
                count_q = select(func.count()).select_from(model)
                counts[name] = session.scalar(count_q.where(cond) if cond is not None else count_q)
                if not dry_run:
                    session.execute(delete(model).where(cond) if cond is not None else delete(model))
            _, cond = scoped(MeetingRun, MeetingRun.id)
            count_q = select(func.count()).select_from(MeetingRun)
            counts["meeting_runs"] = session.scalar(count_q.where(cond) if cond is not None else count_q)
            if not dry_run:
                session.execute(delete(MeetingRun).where(cond) if cond is not None else delete(MeetingRun))
                session.commit()
            logger.info("Runs deleted" if not dry_run else "Runs delete dry-run",
                        run_ids=ids if ids is not None else "all", **counts)
            return {"run_ids": ids if ids is not None else [], "deleted": counts, "dry_run": dry_run}

    # ── чтение для API ───────────────────────────────────────────────────
    @staticmethod
    def _run_dict(r: MeetingRun) -> dict:
        return {"id": r.id, "meeting_date": r.meeting_date.isoformat(),
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
                "source": r.source, "status": r.status, "error": r.error,
                "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()}

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._client.session() as session:
            runs = session.scalars(select(MeetingRun).order_by(MeetingRun.id.desc()).limit(limit)).all()
            return [self._run_dict(r) for r in runs]

    def detail(self, run_id: int) -> dict:
        """Всё по запуску: сырой транскрипт, диалог по говорящим, статусы и отчёты по аудиториям."""
        with self._client.session() as session:
            run = session.get(MeetingRun, run_id)
            if run is None:
                raise LookupError(f"Запуск {run_id} не найден")
            raw = session.scalar(select(TranscriptRecord).where(
                TranscriptRecord.run_id == run_id, TranscriptRecord.kind == "raw"))
            utterances = session.scalars(select(SpeakerUtterance).where(
                SpeakerUtterance.run_id == run_id).order_by(SpeakerUtterance.position)).all()
            statuses = session.scalars(select(PersonStatusRecord).where(
                PersonStatusRecord.run_id == run_id).order_by(PersonStatusRecord.id)).all()
            reports = session.scalars(select(Report).where(Report.run_id == run_id)).all()
            by_audience: dict[str, list[dict]] = {}
            for st in statuses:
                by_audience.setdefault(st.audience, []).append(
                    {"full_name": st.full_name, "project_id": st.project_id,
                     "text": st.text, "has_data": st.has_data})
            return {
                "run": self._run_dict(run),
                "raw_transcript": raw.segments if raw else [],
                "dialogue": [{"position": u.position, "speaker": u.full_name,
                              "start": u.start_sec, "end": u.end_sec, "text": u.text}
                             for u in utterances],
                "statuses": by_audience,
                "reports": {r.audience: r.text for r in reports},
            }


class PostgresCallLog:
    """CallLog: промпты, ответы и ошибки LLM."""

    def __init__(self, client: PostgresClient):
        self._client = client

    def record(self, call: LLMCallRecord) -> None:
        with self._client.session() as session:
            session.add(LLMCall(
                run_id=call.run_id, full_name=call.full_name, audience=call.audience,
                model=call.model, system_prompt=call.system_prompt, user_prompt=call.user_prompt,
                response=call.response, status=call.status, error=call.error,
                duration_ms=call.duration_ms,
            ))
            session.commit()
