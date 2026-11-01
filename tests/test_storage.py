"""Репозитории хранилища на SQLite in-memory (диалект-независимые модели)."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from audio_assistant.client.domain import DailySummary, LLMCallRecord, PersonStatus, Segment, Transcript
from audio_assistant.client.llm_adapter import LoggedLLMClient
from audio_assistant.client.run_context import current_person, current_run_id
from audio_assistant.client.storage.models import LLMCall
from audio_assistant.client.storage.repositories import (
    PostgresCallLog, PostgresHistoryRepository, PostgresRunStore, init_storage)
from postgres_client.client.postgres_client import PostgresClient


@pytest.fixture
def pg():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    client = PostgresClient(engine)
    init_storage(client)
    return client


def _summary(day, audience, *statuses):
    return DailySummary(day, audience, [PersonStatus(*s) for s in statuses])


def test_history_prefers_same_audience_and_skips_no_data(pg):
    repo = PostgresHistoryRepository(pg)
    repo.save(_summary(date(2026, 11, 12), "team", ("Иванов Иван", "1", "тимовый", True)))
    repo.save(_summary(date(2026, 11, 13), "management", ("Иванов Иван", "1", "руководству", True)))
    repo.save(_summary(date(2026, 11, 14), "management", ("Иванов Иван", "1", "нет обновлений", False)))

    assert repo.previous_status("Иванов Иван", date(2026, 11, 15), "team") == "тимовый"
    assert repo.previous_status("Иванов Иван", date(2026, 11, 15), "management") == "руководству"
    assert repo.previous_status("Иванов Иван", date(2026, 11, 15), "other") == "руководству"  # самый свежий
    assert repo.previous_status("Иванов Иван", date(2026, 11, 12), "team") is None  # строго до даты
    assert repo.previous_status("Петров Пётр", date(2026, 11, 15), "team") is None


def test_run_lifecycle_transcripts_and_dialogue(pg):
    store = PostgresRunStore(pg)
    rid = store.start(date(2026, 11, 14), "a.wav", datetime(2026, 11, 14, 10, 0, 0))
    raw = Transcript([Segment(0, 1, "Привет."), Segment(1, 2, "Катя. Делала анализ."),
                      Segment(2, 3, "Ещё немного.")])
    store.save_raw(rid, raw)
    attributed = Transcript([Segment(0, 1, "Привет."),
                             Segment(1, 2, "Делала анализ.", "Вертоградова Екатерина"),
                             Segment(2, 3, "Ещё немного.", "Вертоградова Екатерина")])
    store.save_attributed(rid, attributed)
    store.save_attributed(rid, attributed)  # повтор не дублирует
    store.save_report(rid, "management", "Дейли ...")
    store.save_report(rid, "management", "Дейли v2")
    store.set_stage(rid, "reported")

    assert store.load_raw(rid).segments[1].text == "Катя. Делала анализ."
    assert store.load_attributed(rid).segments[1].speaker == "Вертоградова Екатерина"
    assert store.run_day(rid) == date(2026, 11, 14)

    d = store.detail(rid)
    assert d["run"]["status"] == "reported" and d["run"]["recorded_at"].startswith("2026-11-14T10:00")
    assert [(u["speaker"], u["text"]) for u in d["dialogue"]] == [
        (None, "Привет."), ("Вертоградова Екатерина", "Делала анализ. Ещё немного.")]
    assert d["reports"] == {"management": "Дейли v2"}


def test_missing_stage_input_and_failure(pg):
    store = PostgresRunStore(pg)
    rid = store.start(date(2026, 11, 14), "x", None)
    with pytest.raises(LookupError, match="транскрибац"):
        store.load_raw(rid)
    with pytest.raises(LookupError):
        store.load_raw(999)
    store.fail(rid, "RuntimeError: boom")
    assert store.list_runs()[0]["status"] == "error" and "boom" in store.list_runs()[0]["error"]
    store.set_stage(rid, "transcribed")           # успешная повторная стадия снимает ошибку
    assert store.list_runs()[0]["error"] is None


def test_statuses_replace_per_run_audience_and_load_summary(pg):
    store, repo = PostgresRunStore(pg), PostgresHistoryRepository(pg)
    rid = store.start(date(2026, 11, 14), "x", None)
    tok = current_run_id.set(rid)
    try:
        repo.save(_summary(date(2026, 11, 14), "management", ("Иванов Иван", "1", "v1", True)))
        repo.save(_summary(date(2026, 11, 14), "management", ("Иванов Иван", "1", "v2", True)))
        repo.save(_summary(date(2026, 11, 14), "team", ("Иванов Иван", "1", "команде", True)))
    finally:
        current_run_id.reset(tok)
    assert [s.text for s in repo.load_summary(rid, "management").statuses] == ["v2"]
    assert repo.load_summary(rid, "team").statuses[0].text == "команде"
    assert repo.load_summary(rid, "other") is None
    assert set(store.detail(rid)["statuses"]) == {"management", "team"}


def test_logged_llm_records_success_and_error_with_context(pg):
    class Flaky:
        model_name = "m"

        def __init__(self): self.n = 0

        def generate(self, system, user):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("timeout")
            return "ответ"

    rid = PostgresRunStore(pg).start(date(2026, 11, 14), "x")
    llm = LoggedLLMClient(Flaky(), PostgresCallLog(pg))
    t1, t2 = current_run_id.set(rid), current_person.set("Иванов Иван")
    try:
        assert llm.generate("sys", "usr") == "ответ"
        with pytest.raises(RuntimeError):
            llm.generate("sys", "usr2")
    finally:
        current_run_id.reset(t1); current_person.reset(t2)

    with pg.session() as s:
        calls = s.scalars(select(LLMCall).order_by(LLMCall.id)).all()
    assert [(c.status, c.run_id, c.full_name, c.model) for c in calls] == [
        ("success", rid, "Иванов Иван", "m"), ("error", rid, "Иванов Иван", "m")]
    assert calls[0].response == "ответ" and "timeout" in calls[1].error and calls[1].duration_ms >= 0


def test_logging_failure_does_not_break_generation():
    class BrokenLog:
        def record(self, call: LLMCallRecord): raise ConnectionError("db down")

    class Ok:
        def generate(self, system, user): return "ok"

    assert LoggedLLMClient(Ok(), BrokenLog()).generate("s", "u") == "ok"


def _seed(pg, day, source):
    store, repo, log = PostgresRunStore(pg), PostgresHistoryRepository(pg), PostgresCallLog(pg)
    rid = store.start(day, source, None)
    store.save_raw(rid, Transcript.from_plain_text("Роман. Привет."))
    store.save_attributed(rid, Transcript([Segment(0, 1, "Привет.", "Гончар Роман")]))
    store.save_report(rid, "management", "отчёт")
    tok = current_run_id.set(rid)
    try:
        repo.save(_summary(day, "management", ("Гончар Роман", "3924", "статус", True)))
    finally:
        current_run_id.reset(tok)
    log.record(LLMCallRecord("management", "m", "s", "u", "success", 1.0, "r", None, "Гончар Роман", rid))
    return rid


def _counts(pg):
    from sqlalchemy import func
    from audio_assistant.client.storage.models import (
        MeetingRun, PersonStatusRecord, Report, SpeakerUtterance, TranscriptRecord, LLMCall)
    with pg.session() as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m))
                for m in (MeetingRun, TranscriptRecord, SpeakerUtterance, PersonStatusRecord, Report, LLMCall)}


def test_delete_by_id_removes_children_and_history_context(pg):
    store, repo = PostgresRunStore(pg), PostgresHistoryRepository(pg)
    a = _seed(pg, date(2026, 11, 13), "a.wav")
    b = _seed(pg, date(2026, 11, 14), "b.wav")

    dry = store.delete(run_id=a, dry_run=True)
    assert dry["dry_run"] and dry["deleted"]["meeting_runs"] == 1 and _counts(pg)["meeting_runs"] == 2

    res = store.delete(run_id=a)
    assert res["run_ids"] == [a] and res["deleted"]["person_statuses"] == 1 and res["deleted"]["llm_calls"] == 1
    counts = _counts(pg)
    assert counts["meeting_runs"] == 1 and counts["person_statuses"] == 1 and counts["llm_calls"] == 1
    # статус удалённой встречи не должен остаться «прошлым статусом»
    assert repo.previous_status("Гончар Роман", date(2026, 11, 14), "management") is None
    assert store.detail(b)["run"]["id"] == b


def test_delete_by_file_name_date_and_all(pg):
    store = PostgresRunStore(pg)
    _seed(pg, date(2026, 11, 13), "a.wav")
    _seed(pg, date(2026, 11, 13), "b.wav")
    _seed(pg, date(2026, 11, 14), "a.wav")

    assert store.delete(file_name="nope.wav")["run_ids"] == []
    assert len(store.delete(file_name="a.wav", meeting_date=date(2026, 11, 13))["run_ids"]) == 1  # AND
    assert _counts(pg)["meeting_runs"] == 2
    assert len(store.delete(meeting_date=date(2026, 11, 13))["run_ids"]) == 1
    assert len(store.delete(file_name="a.wav")["run_ids"]) == 1 and _counts(pg)["meeting_runs"] == 0

    _seed(pg, date(2026, 11, 15), "c.wav")
    PostgresCallLog(pg).record(LLMCallRecord("team", "m", "s", "u", "error", 1.0, None, "x"))  # без run_id
    res = store.delete(everything=True)
    assert res["deleted"]["llm_calls"] == 2 and set(_counts(pg).values()) == {0}
