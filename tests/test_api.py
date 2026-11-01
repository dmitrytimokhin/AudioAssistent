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
