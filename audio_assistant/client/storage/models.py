"""SQLAlchemy-модели. Доменные таблицы живут здесь, а не в postgres_client,
который остаётся универсальной обёрткой над Engine."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB в PostgreSQL, обычный JSON в остальных диалектах (SQLite в тестах)
JsonType = JSON().with_variant(JSONB(), "postgresql")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MeetingRun(Base):
    """Встреча и ход её обработки. Аудитория относится не к запуску, а к статусам и отчётам:
    одну встречу можно суммаризировать для нескольких аудиторий."""

    __tablename__ = "meeting_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_date: Mapped[date] = mapped_column(Date, nullable=False)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime)  # из имени файла, локальное время записи
    source: Mapped[str] = mapped_column(Text, default="")
    # created -> transcribed -> attributed -> summarized -> reported | error
    status: Mapped[str] = mapped_column(String(16), default="created")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (Index("ix_meeting_runs_date", "meeting_date"),)


class TranscriptRecord(Base):
    """Транскрипт запуска: raw — как отдал Whisper, attributed — с разметкой говорящих."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("meeting_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # raw | attributed
    segments: Mapped[list] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("run_id", "kind", name="uq_transcripts_run_kind"),)


class SpeakerUtterance(Base):
    """Диалог по репликам: кто что сказал (подряд идущие фразы одного говорящего склеены).
    full_name NULL — реплики до первой именной метки (вступление ведущего)."""

    __tablename__ = "speaker_utterances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("meeting_runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128))
    start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class Report(Base):
    """Итоговый текст статуса для аудитории."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("meeting_runs.id", ondelete="CASCADE"))
    audience: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("run_id", "audience", name="uq_reports_run_audience"),)


class PersonStatusRecord(Base):
    """Итоговый статус сотрудника. Из этой таблицы берётся «прошлый статус» для контекста LLM."""

    __tablename__ = "person_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("meeting_runs.id", ondelete="SET NULL"))
    meeting_date: Mapped[date] = mapped_column(Date, nullable=False)
    audience: Mapped[str] = mapped_column(String(64), nullable=False)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    has_data: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_person_statuses_name_date", "full_name", "meeting_date"),)


class LLMCall(Base):
    """Журнал вызовов LLM: что отправили, что получили, чем закончилось."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("meeting_runs.id", ondelete="SET NULL"), index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    audience: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    user_prompt: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success | error
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
