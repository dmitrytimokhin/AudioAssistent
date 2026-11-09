from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_assistant.client.history_file import FileHistoryRepository
from audio_assistant.client.bootstrap import build_pipeline
from audio_assistant.endpoint.assistant_router import assistant_router
from audio_assistant.endpoint.assistant_settings import AssistantSettings


class FakeLLM:
    def generate(self, system, user):
        return "сделал задачу"


@pytest.fixture
def client(tmp_path):
    settings = AssistantSettings(history_dir=tmp_path, attribution_method="rules")
    app = FastAPI()
    app.include_router(assistant_router)  # без `with TestClient`: lifespan не запускается
    app.state.assistant_pipeline = build_pipeline(settings, FakeLLM(), FileHistoryRepository(tmp_path))
    return TestClient(app)


def test_summarize_endpoint(client):
    r = client.post("/assistant/summarize", json={
        "text": "Начали.\nРоман. Катим как есть.\nКатя. Анализ.", "meeting_date": "2026-11-14", "save": False})
    assert r.status_code == 200
    body = r.json()
    assert body["text"].startswith("Дейли 14.11.2026\nПроект 3924:\n1. Гончар Роман - сделал задачу")
    by_name = {s["full_name"]: s["has_data"] for s in body["statuses"]}
    assert by_name["Гончар Роман"] and by_name["Вертоградова Екатерина"] and not by_name["Тимофеев Сергей"]
    assert body["transcript"][0]["speaker"] is None


def test_unknown_audience_is_422(client):
    r = client.post("/assistant/summarize", json={"text": "Роман. Привет.", "audience": "nope"})
    assert r.status_code == 422 and "management" in r.json()["detail"]


def test_audiences_and_health(client):
    assert {"management", "team"} <= set(client.get("/assistant/audiences").json()["audiences"])
    assert client.get("/assistant/health").json()["participants"] == 9


def test_runs_requires_postgres(client):
    assert client.get("/assistant/runs").status_code == 501


# ── стадии на SQLite-хранилище и фейковом Whisper ───────────────────────────
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from audio_assistant.client.domain import Segment, Transcript
from audio_assistant.client.storage.repositories import PostgresHistoryRepository, PostgresRunStore, init_storage
from postgres_client.client.postgres_client import PostgresClient


class FakeWhisper:
    def transcribe(self, audio, prompt_hint=""):
        return Transcript([Segment(0, 5, "Привет, начинаем."), Segment(5, 9, "Роман. Катим как есть."),
                           Segment(9, 12, "Катя. Анализ готов.")])


@pytest.fixture
def staged(tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    pg = PostgresClient(engine)
    init_storage(pg)
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "20261101180957.WAV").write_bytes(b"RIFF")
    settings = AssistantSettings(history_dir=tmp_path, audio_dir=audio, attribution_method="rules")

    app = FastAPI()
    app.include_router(assistant_router)
    pipeline = build_pipeline(settings, FakeLLM(), PostgresHistoryRepository(pg), PostgresRunStore(pg))
    pipeline.transcriber = FakeWhisper()
    app.state.assistant_pipeline = pipeline

    from audio_assistant.endpoint import assistant_router as router_module
    router_module.get_assistant_settings = lambda: settings
    yield TestClient(app)
    from audio_assistant.endpoint.assistant_settings import get_assistant_settings
    router_module.get_assistant_settings = get_assistant_settings


def test_stages_one_by_one(staged):
    files = staged.get("/assistant/audio").json()
    assert files[0]["file_name"] == "20261101180957.WAV" and files[0]["recorded_at"] == "2026-11-01T18:09:57"

    t = staged.post("/assistant/transcribe", json={"file_name": "20261101180957.WAV"}).json()
    run_id = t["run_id"]
    assert t["meeting_date"] == "2026-11-01" and len(t["segments"]) == 3 and t["segments"][1]["speaker"] is None

    # статусы до атрибуции недоступны
    assert staged.post(f"/assistant/runs/{run_id}/summarize", json={}).status_code == 404

    a = staged.post(f"/assistant/runs/{run_id}/attribute").json()
    assert [(d["speaker"], d["text"]) for d in a["dialogue"]] == [
        (None, "Привет, начинаем."), ("Гончар Роман", "Катим как есть."),
        ("Вертоградова Екатерина", "Анализ готов.")]
    assert "Тимофеев Сергей" in a["without_utterances"]

    # отчёт до суммаризации невозможен
    assert staged.post(f"/assistant/runs/{run_id}/report", json={}).status_code == 404

    s = staged.post(f"/assistant/runs/{run_id}/summarize", json={"audience": "management"}).json()
    assert {x["full_name"]: x["has_data"] for x in s["statuses"]}["Гончар Роман"] is True

    r = staged.post(f"/assistant/runs/{run_id}/report", json={"audience": "management"}).json()
    assert r["text"].startswith("Дейли 01.11.2026\nПроект 3924:\n1. Гончар Роман - сделал задачу")

    d = staged.get(f"/assistant/runs/{run_id}").json()
    assert d["run"]["status"] == "reported"
    assert len(d["raw_transcript"]) == 3 and len(d["dialogue"]) == 3
    assert d["reports"]["management"] == r["text"] and "management" in d["statuses"]

    # другой аудитории — своя суммаризация и отчёт в том же запуске
    staged.post(f"/assistant/runs/{run_id}/process", json={"audience": "team"})
    assert set(staged.get(f"/assistant/runs/{run_id}").json()["reports"]) == {"management", "team"}


def test_process_file_and_errors(staged):
    r = staged.post("/assistant/process-file", json={"file_name": "20261101180957.WAV"})
    assert r.status_code == 200 and r.json()["meeting_date"] == "2026-11-01" and r.json()["run_id"]
    assert staged.post("/assistant/process-file", json={"file_name": "nope.wav"}).status_code == 404
    assert staged.post("/assistant/transcribe", json={"file_name": "../secret.wav"}).status_code == 422
    assert staged.get("/assistant/runs/999").status_code == 404
    assert staged.get("/assistant/runs").json()[0]["id"] == r.json()["run_id"]


def test_delete_endpoints(staged):
    run_a = staged.post("/assistant/process-file", json={"file_name": "20261101180957.WAV"}).json()["run_id"]
    run_b = staged.post("/assistant/process-file", json={"file_name": "20261101180957.WAV"}).json()["run_id"]

    # защита от случайной полной очистки и взаимоисключающие параметры
    assert staged.delete("/assistant/runs").status_code == 422
    assert staged.delete("/assistant/runs?all=true&meeting_date=2026-11-01").status_code == 422

    dry = staged.delete("/assistant/runs?file_name=20261101180957.WAV&dry_run=true").json()
    assert sorted(dry["run_ids"]) == [run_a, run_b] and dry["dry_run"]
    assert len(staged.get("/assistant/runs").json()) == 2

    assert staged.delete(f"/assistant/runs/{run_a}").json()["run_ids"] == [run_a]
    assert staged.delete(f"/assistant/runs/{run_a}").status_code == 404
    assert staged.delete("/assistant/runs?meeting_date=2026-11-01").json()["run_ids"] == [run_b]
    assert staged.delete("/assistant/runs?meeting_date=2026-11-01").json()["run_ids"] == []  # уже нет

    staged.post("/assistant/process-file", json={"file_name": "20261101180957.WAV"})
    assert staged.delete("/assistant/runs?all=true").json()["deleted"]["meeting_runs"] == 1
    assert staged.get("/assistant/runs").json() == []


def test_delete_requires_postgres(client):
    assert client.delete("/assistant/runs/1").status_code == 501
    assert client.delete("/assistant/runs?all=true").status_code == 501


def test_upload_and_transcribe(staged):
    wav = ("20261102090000.wav", b"RIFFdata", "audio/wav")
    r = staged.post("/assistant/audio", files={"file": wav})
    assert r.status_code == 200 and r.json()["recorded_at"] == "2026-11-02T09:00:00" and r.json()["run_id"] is None
    assert staged.post("/assistant/audio", files={"file": wav}).status_code == 409           # без overwrite
    assert staged.post("/assistant/audio", files={"file": wav}, data={"overwrite": "true"}).status_code == 200
    assert staged.post("/assistant/audio", files={"file": ("notes.txt", b"x", "text/plain")}).status_code == 422

    t = staged.post("/assistant/audio", files={"file": ("20261103100000.wav", b"RIFF", "audio/wav")},
                    data={"transcribe": "true"}).json()
    assert t["run_id"] and t["meeting_date"] == "2026-11-03" and len(t["segments"]) == 3
    assert "20261103100000.wav" in {f["file_name"] for f in staged.get("/assistant/audio").json()}


def test_custom_prompt(staged):
    seen = []
    pipeline = staged.app.state.assistant_pipeline
    pipeline.prompt_runner._llm = type("L", (), {"generate": lambda self, s, u: seen.append((s, u)) or "ответ"})()

    run_id = staged.post("/assistant/transcribe", json={"file_name": "20261101180957.WAV"}).json()["run_id"]
    r = staged.post("/assistant/llm", json={"run_id": run_id, "system": "Ты аналитик.",
                                            "prompt": "Выпиши решения: {transcript}\nФормат: {\"a\": 1}"})
    assert r.status_code == 200 and r.json()["text"] == "ответ" and r.json()["source"] == "raw"
    assert seen[0] == ("Ты аналитик.", "Выпиши решения: Привет, начинаем. Роман. Катим как есть. Катя. Анализ готов.\nФормат: {\"a\": 1}")

    # нет метки -> транскрипт в конце; dialogue требует стадию attribute
    assert staged.post("/assistant/llm", json={"run_id": run_id, "prompt": "Кратко", "source": "dialogue"}).status_code == 404
    staged.post(f"/assistant/runs/{run_id}/attribute")
    staged.post("/assistant/llm", json={"run_id": run_id, "prompt": "Кратко", "source": "dialogue"})
    assert seen[-1][1].startswith("Кратко\n\nТранскрипт:\n[???] Привет") and "[Гончар Роман] Катим" in seen[-1][1]

    # без запуска: текст из запроса или чистый промпт; ошибки
    staged.post("/assistant/llm", json={"prompt": "Привет", "transcript_text": "ТЕКСТ"})
    assert seen[-1][1] == "Привет\n\nТранскрипт:\nТЕКСТ"
    staged.post("/assistant/llm", json={"prompt": "Без текста"})
    assert seen[-1][1] == "Без текста"
    assert staged.post("/assistant/llm", json={"prompt": " "}).status_code == 422
    assert staged.post("/assistant/llm", json={"prompt": "x", "run_id": run_id, "transcript_text": "y"}).status_code == 422
    assert staged.post("/assistant/llm", json={"prompt": "x", "run_id": 999}).status_code == 404
    assert staged.get(f"/assistant/runs/{run_id}").json()["run"]["status"] != "error"


def test_table_views(staged):
    run_id = staged.post("/assistant/process-file", json={"file_name": "20261101180957.WAV"}).json()["run_id"]
    counts = staged.get("/assistant/tables").json()["counts"]
    assert set(counts) == {"meeting_runs", "transcripts", "speaker_utterances", "person_statuses", "reports", "llm_calls",
                           "analyses", "digests", "image_analyses"}
    assert counts["meeting_runs"] == 1 and counts["reports"] == 1

    t = staged.get("/assistant/tables/speaker_utterances", params={"run_id": run_id}).json()
    assert t["total"] == len(t["rows"]) == 3 and "text" in t["columns"] and t["rows"][0]["run_id"] == run_id
    assert staged.get("/assistant/tables/meeting_runs", params={"run_id": run_id}).json()["rows"][0]["id"] == run_id
    assert staged.get("/assistant/tables/meeting_runs", params={"run_id": 999}).json()["total"] == 0
    assert len(staged.get("/assistant/tables/person_statuses", params={"limit": 2}).json()["rows"]) == 2
    assert staged.get("/assistant/tables/pg_user").status_code == 404      # только свои таблицы


def test_transcribe_in_background(staged):
    import threading
    from pathlib import Path
    gate, started = threading.Event(), threading.Event()
    pipeline = staged.app.state.assistant_pipeline
    whisper = pipeline.transcriber

    def slow(audio, prompt_hint=""):
        from audio_assistant.client.run_context import report_progress
        report_progress(30, 60)
        started.set()
        gate.wait(5)
        return whisper.transcribe(audio)
    pipeline.transcriber = type("Slow", (), {"transcribe": staticmethod(slow)})()

    # то, что роутер делает при wait=false: запуск заводится сразу, работа идёт в фоне
    # (TestClient дожидается фоновых задач до ответа, поэтому поток запускаем сами)
    from audio_assistant.endpoint import assistant_router as router_module
    audio = router_module.get_assistant_settings().audio_dir / "20261101180957.WAV"
    run_id, created = pipeline.begin_transcription(audio, date(2026, 11, 1))
    assert created
    worker = threading.Thread(target=pipeline.finish_transcription, args=(run_id, audio))
    worker.start()
    assert started.wait(5)

    run = staged.get(f"/assistant/runs/{run_id}").json()["run"]
    assert run["status"] == "transcribing" and run["progress"] == 0.5

    # повторный клик: тот же запуск, второй транскрибации нет (регистр имени не важен)
    again = staged.post("/assistant/transcribe", json={"file_name": "20261101180957.WAV", "wait": False}).json()
    assert again["run_id"] == run_id and again["already_running"] and again["status"] == "transcribing"
    assert len(staged.get("/assistant/runs").json()) == 1

    gate.set()
    worker.join(5)
    run = staged.get(f"/assistant/runs/{run_id}").json()["run"]
    assert run["status"] == "transcribed" and run["progress"] is None
    assert len(staged.get(f"/assistant/runs/{run_id}").json()["raw_transcript"]) == 3


def test_background_transcription_endpoint_and_failure(staged):
    r = staged.post("/assistant/transcribe", json={"file_name": "20261101180957.WAV", "wait": False}).json()
    assert r["status"] == "transcribing" and r["segments"] == []
    assert staged.get(f"/assistant/runs/{r['run_id']}").json()["run"]["status"] == "transcribed"   # фон уже отработал

    pipeline = staged.app.state.assistant_pipeline
    pipeline.transcriber = type("Broken", (), {"transcribe": lambda self, a, prompt_hint="": 1 / 0})()
    bad = staged.post("/assistant/transcribe", json={"file_name": "20261101180957.WAV", "wait": False}).json()
    run = staged.get(f"/assistant/runs/{bad['run_id']}").json()["run"]
    assert run["status"] == "error" and "ZeroDivisionError" in run["error"]


def test_interrupted_transcription_marked_on_start(staged):
    store = staged.app.state.assistant_pipeline.store
    run_id = store.start(date(2026, 11, 1), "a.wav")
    store.set_stage(run_id, "transcribing")
    assert store.fail_interrupted() == 1
    run = staged.get(f"/assistant/runs/{run_id}").json()["run"]
    assert run["status"] == "error" and "Прервано" in run["error"]
