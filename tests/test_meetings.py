"""Типы встреч, теги, разбор встреч, сводки в Excel, проверка моделей."""
import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from audio_assistant.client.bootstrap import build_pipeline
from audio_assistant.client.domain import Segment, Transcript
from audio_assistant.client.meeting_kinds import MeetingKinds, MeetingKind
from audio_assistant.client.meeting_text import fill, split_text
from audio_assistant.client.digest import parse_rows
from audio_assistant.client.readiness import CheckResult, ReadinessChecker
from audio_assistant.client.storage.digest_store import PostgresDigestStore
from audio_assistant.client.storage.repositories import (
    PostgresHistoryRepository, PostgresRunStore, _add_missing_columns, init_storage)
from audio_assistant.endpoint import assistant_router as router_module
from audio_assistant.endpoint.assistant_router import assistant_router
from audio_assistant.endpoint.assistant_settings import AssistantSettings
from postgres_client.client.postgres_client import PostgresClient


class ScriptedLLM:
    """Обычные запросы -> текст; запросы с json_schema (сводка) -> строки по колонкам из схемы."""

    def __init__(self):
        self.calls = []
        self.fail_on: str | None = None

    def generate(self, system, user, json_schema=None):
        self.calls.append((system, user, json_schema))
        if self.fail_on and self.fail_on in user:
            raise RuntimeError("модель недоступна")
        if json_schema is not None:
            cols = list(json_schema["properties"]["rows"]["items"]["properties"])
            return json.dumps({"rows": [{c: f"{c}-значение" for c in cols}]}, ensure_ascii=False)
        return "разбор встречи"


class FakeWhisper:
    def transcribe(self, audio, prompt_hint=""):
        return Transcript([Segment(0, 5, "Привет, начинаем."), Segment(5, 9, "Роман. Катим как есть.")])


@pytest.fixture
def env(tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    pg = PostgresClient(engine)
    init_storage(pg)
    audio = tmp_path / "audio"
    audio.mkdir()
    for name in ("20261102090000.WAV", "20261103090000.WAV", "20261110090000.WAV"):
        (audio / name).write_bytes(b"RIFF")
    settings = AssistantSettings(history_dir=tmp_path, audio_dir=audio, output_dir=tmp_path / "out",
                                 attribution_method="rules")
    llm = ScriptedLLM()
    store = PostgresRunStore(pg)
    pipeline = build_pipeline(settings, llm, PostgresHistoryRepository(pg), store, PostgresDigestStore(pg))
    pipeline.transcriber = FakeWhisper()
    app = FastAPI()
    app.include_router(assistant_router)
    app.state.assistant_pipeline = pipeline
    original = router_module.get_assistant_settings
    from audio_assistant.endpoint import meetings_router as meetings_module
    router_module.get_assistant_settings = meetings_module.get_assistant_settings = lambda: settings
    yield type("Env", (), {"client": TestClient(app), "llm": llm, "pipeline": pipeline, "store": store,
                           "settings": settings, "app": app, "pg": pg})
    router_module.get_assistant_settings = meetings_module.get_assistant_settings = original


def transcribe(env, name, kind=None, tags=()):
    r = env.client.post("/assistant/transcribe", json={"file_name": name, "kind": kind, "tags": list(tags)})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


# ── тип и теги ──────────────────────────────────────────────────────────
def test_kind_and_tags_saved_filtered_and_patched(env):
    daily = transcribe(env, "20261102090000.WAV", "daily", ["проект 3924", " срочно ", "срочно", ""])
    one = transcribe(env, "20261103090000.WAV", "one_to_one")
    other = transcribe(env, "20261110090000.WAV")

    runs = {r["id"]: r for r in env.client.get("/assistant/runs").json()}
    assert runs[daily]["kind"] == "daily" and runs[daily]["tags"] == ["проект 3924", "срочно"]
    assert runs[other]["kind"] is None and runs[other]["tags"] == []

    ids = lambda **q: sorted(r["id"] for r in env.client.get("/assistant/runs", params=q).json())  # noqa: E731
    assert ids(kind="daily") == [daily] and ids(tag="срочно") == [daily]
    assert ids(date_from="2026-11-03", date_to="2026-11-09") == [one]
    assert ids(kind="daily", tag="нет такого") == []

    # правка: меняется только переданное
    r = env.client.patch(f"/assistant/runs/{other}", json={"kind": "management"})
    assert r.status_code == 200 and r.json()["kind"] == "management" and r.json()["tags"] == []
    r = env.client.patch(f"/assistant/runs/{other}", json={"tags": ["клиент А"]}).json()
    assert r["kind"] == "management" and r["tags"] == ["клиент А"]
    assert env.client.patch(f"/assistant/runs/{other}", json={"kind": None}).json()["kind"] is None

    assert env.client.patch(f"/assistant/runs/{other}", json={"kind": "dayli"}).status_code == 422
    assert env.client.patch("/assistant/runs/999", json={"kind": "daily"}).status_code == 404
    bad = env.client.post("/assistant/transcribe", json={"file_name": "20261102090000.WAV", "kind": "dayli"})
    assert bad.status_code == 422 and "daily" in bad.json()["detail"]


def test_upload_and_process_file_accept_kind(env):
    r = env.client.post("/assistant/audio", files={"file": ("20261201100000.wav", b"RIFF", "audio/wav")},
                        data={"transcribe": "true", "kind": "one_to_one", "tags": "а, б ,а"}).json()
    run = env.client.get(f"/assistant/runs/{r['run_id']}").json()["run"]
    assert run["kind"] == "one_to_one" and run["tags"] == ["а", "б"]
    p = env.client.post("/assistant/process-file", json={"file_name": "20261102090000.WAV",
                                                         "kind": "daily", "tags": ["x"]}).json()
    assert env.client.get(f"/assistant/runs/{p['run_id']}").json()["run"]["kind"] == "daily"


def test_prompts_listing(env):
    body = env.client.get("/assistant/prompts").json()
    kinds = {k["name"]: k for k in body["kinds"]}
    assert set(kinds) == {"daily", "one_to_one", "business_customer", "management", "other"}
    assert all(k["prompt_exists"] for k in kinds.values()) and "weekly_dailies" in body["digests"]


def test_old_database_gets_new_columns():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE meeting_runs (id INTEGER PRIMARY KEY, meeting_date DATE, source TEXT)"))
    _add_missing_columns(PostgresClient(engine))
    with engine.begin() as conn:
        assert {"kind", "tags"} <= {row[1] for row in conn.execute(text("PRAGMA table_info(meeting_runs)"))}


# ── разбор встречи ──────────────────────────────────────────────────────
def test_analyze_uses_kind_prompt_and_saves(env):
    run = transcribe(env, "20261102090000.WAV", "daily", ["t1"])
    env.client.post(f"/assistant/runs/{run}/attribute")
    r = env.client.post(f"/assistant/runs/{run}/analyze")
    assert r.status_code == 200 and r.json()["prompt"] == "daily" and r.json()["text"] == "разбор встречи"
    system, user, schema = env.llm.calls[-1]
    assert "Дата встречи: 2026-11-02" in user and "Теги: t1" in user and "[Гончар Роман] Катим" in user
    assert "{transcript}" not in user and schema is None
    assert env.client.get(f"/assistant/runs/{run}").json()["analyses"] == {"daily": "разбор встречи"}

    env.llm.calls.clear()
    other = env.client.post(f"/assistant/runs/{run}/analyze", json={"prompt": "management", "source": "raw"}).json()
    assert other["prompt"] == "management" and "Привет, начинаем." in env.llm.calls[-1][1]
    assert set(env.client.get(f"/assistant/runs/{run}").json()["analyses"]) == {"daily", "management"}


def test_analyze_errors(env):
    run = transcribe(env, "20261102090000.WAV")                       # без типа
    assert env.client.post(f"/assistant/runs/{run}/analyze", json={"source": "raw"}).status_code == 422
    assert env.client.post(f"/assistant/runs/{run}/analyze",
                           json={"prompt": "нет_такого", "source": "raw"}).status_code == 404
    # dialogue без стадии attribute не подменяется сырым текстом молча
    assert env.client.post(f"/assistant/runs/{run}/analyze", json={"prompt": "daily"}).status_code == 404
    assert env.client.post("/assistant/runs/999/analyze", json={"prompt": "daily"}).status_code == 404
    assert env.llm.calls == []


def test_analyze_splits_long_transcript(env):
    env.pipeline.analyzer._chunk_chars = 20
    run = transcribe(env, "20261102090000.WAV", "daily")
    r = env.client.post(f"/assistant/runs/{run}/analyze", json={"source": "raw"}).json()
    assert r["chunks"] == len(env.llm.calls) >= 2 and r["text"].startswith("[Часть 1 из ")


# ── сводка ──────────────────────────────────────────────────────────────
def make_digest(env, **body):
    return env.client.post("/assistant/digests", json={"name": "weekly_dailies", **body})


def test_digest_period_kinds_tags_and_excel(env):
    d1 = transcribe(env, "20261102090000.WAV", "daily", ["a"])
    d2 = transcribe(env, "20261103090000.WAV", "daily")
    transcribe(env, "20261110090000.WAV", "daily")                    # вне периода
    one = transcribe(env, "20261103090000.WAV", "one_to_one")         # не тот тип
    env.client.post(f"/assistant/runs/{d1}/attribute")                # у d2 разметки нет -> сырой текст

    r = make_digest(env, date_from="2026-11-02", date_to="2026-11-09")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "done" and d["done"] == d["total"] == 2 and d["rows_count"] == 2
    detail = env.client.get(f"/assistant/digests/{d['id']}").json()
    assert [m["run_id"] for m in detail["meetings"]] == [d1, d2] and one not in [m["run_id"] for m in detail["meetings"]]
    assert detail["headers"] == ["Дата", "Сотрудник", "Проект", "Сделано", "Планы", "Блокеры"]
    assert detail["rows"][0] == {"Дата": "2026-11-02", "Сотрудник": "Сотрудник-значение", "Проект": "Проект-значение",
                                 "Сделано": "Сделано-значение", "Планы": "Планы-значение", "Блокеры": "Блокеры-значение"}
    assert "сырой транскрипт" in detail["meetings"][1]["note"] and detail["meetings"][0]["note"] == ""
    assert "Привет, начинаем." in env.llm.calls[-1][1]                # d2: источник — сырой текст

    xlsx = env.client.get(f"/assistant/digests/{d['id']}/xlsx")
    assert xlsx.status_code == 200 and "spreadsheetml" in xlsx.headers["content-type"]
    path = env.settings.output_dir / detail["file_name"]
    wb = load_workbook(path)
    assert wb.sheetnames == ["Данные", "Встречи"]
    assert [c.value for c in wb["Данные"][1]] == ["Дата", "Сотрудник", "Проект", "Сделано", "Планы", "Блокеры"]
    assert wb["Данные"].max_row == 3 and wb["Встречи"].max_row == 3

    # теги, явный список запусков, пустой выбор
    assert make_digest(env, date_from="2026-11-02", date_to="2026-11-09", tags=["a"]).json()["total"] == 1
    assert make_digest(env, run_ids=[one], kinds=["one_to_one"]).json()["total"] == 1
    assert make_digest(env, date_from="2026-01-01", date_to="2026-01-07").status_code == 404
    assert make_digest(env, date_from="2026-11-09", date_to="2026-11-02").status_code == 422
    assert make_digest(env, name="нет_такой").status_code == 404
    assert make_digest(env, kinds=["dayli"], date_from="2026-11-02").status_code == 422
    assert make_digest(env, run_ids=[999]).status_code == 404


def test_digest_survives_failed_meeting_and_background(env):
    a = transcribe(env, "20261102090000.WAV", "daily")
    transcribe(env, "20261103090000.WAV", "daily")
    env.llm.fail_on = "Дейли 2026-11-02"
    d = make_digest(env, date_from="2026-11-02", date_to="2026-11-09").json()
    assert d["status"] == "done" and d["rows_count"] == 1
    statuses = {m["run_id"]: m["status"] for m in env.client.get(f"/assistant/digests/{d['id']}").json()["meetings"]}
    assert statuses[a] == "ошибка"

    env.llm.fail_on = None
    bg = make_digest(env, date_from="2026-11-02", date_to="2026-11-09", wait=False).json()
    assert bg["status"] in ("running", "done")
    assert env.client.get(f"/assistant/digests/{bg['id']}").json()["status"] == "done"   # TestClient дождался фона


def test_digest_without_transcript_is_skipped(env):
    run = env.store.start(date(2026, 11, 4), "x.wav", None, "daily", [])       # запуск без транскрипта
    d = make_digest(env, run_ids=[run]).json()
    assert d["rows_count"] == 0 and env.client.get(f"/assistant/digests/{d['id']}").json()["meetings"][0]["status"] == "пропущена"


def test_digest_listing_and_delete(env):
    transcribe(env, "20261102090000.WAV", "daily")
    first = make_digest(env, date_from="2026-11-02", date_to="2026-11-09").json()
    second = make_digest(env, date_from="2026-11-02", date_to="2026-11-09").json()
    assert [d["id"] for d in env.client.get("/assistant/digests").json()] == [second["id"], first["id"]]
    assert "rows" not in env.client.get("/assistant/digests").json()[0]

    path = env.settings.output_dir / env.client.get(f"/assistant/digests/{first['id']}").json()["file_name"]
    assert path.exists()
    dry = env.client.delete(f"/assistant/digests/{first['id']}", params={"dry_run": True}).json()
    assert dry["digest_ids"] == [first["id"]] and path.exists()
    assert env.client.delete(f"/assistant/digests/{first['id']}").status_code == 200 and not path.exists()
    assert env.client.get(f"/assistant/digests/{first['id']}").status_code == 404
    assert env.client.delete(f"/assistant/digests/{first['id']}").status_code == 404
    assert env.client.delete("/assistant/digests").status_code == 422
    assert env.client.delete("/assistant/digests", params={"all": True}).json()["digest_ids"] == [second["id"]]


def test_deleting_run_removes_its_analyses(env):
    run = transcribe(env, "20261102090000.WAV", "daily")
    env.client.post(f"/assistant/runs/{run}/analyze", json={"source": "raw"})
    assert env.client.get("/assistant/tables").json()["counts"]["analyses"] == 1
    deleted = env.client.delete(f"/assistant/runs/{run}").json()["deleted"]
    assert deleted["analyses"] == 1 and env.client.get("/assistant/tables").json()["counts"]["analyses"] == 0


# ── проверка моделей ────────────────────────────────────────────────────
class FakeOllama:
    def __init__(self, reachable=True, models=("qwen3.5:4b",)):
        self.reachable, self.models = reachable, models
        self.settings = type("S", (), {"ollama_url": "http://x:11434", "model_name": "qwen3.5:4b"})()

    def connect(self):
        return self.reachable

    def has_model(self, name=None):
        return name in self.models


class FakeModelFiles:
    _model = "large-v3-turbo"

    def __init__(self, state):
        self._s = state

    def status(self):
        return self._s


def checks(**kw):
    base = dict(ollama=FakeOllama(), transcriber=FakeModelFiles("ready"), transcriber_backend="faster_whisper")
    return {r.name: r for r in ReadinessChecker(**{**base, **kw}).run()}


def test_readiness_statuses():
    ok = checks()
    assert ok["ollama"].status == "ok" and ok["whisper"].status == "ok" and ok["ffmpeg"].status == "ok"

    assert checks(ollama=FakeOllama(reachable=False))["ollama"].status == "error"
    missing = checks(ollama=FakeOllama(models=("другая:1b",)))["ollama"]
    assert missing.status == "error" and "ollama pull qwen3.5:4b" in missing.detail

    assert checks(transcriber=FakeModelFiles("missing"))["whisper"].status == "warn"
    assert checks(transcriber=FakeModelFiles("downloading"))["whisper"].status == "loading"
    assert checks(transcriber=FakeModelFiles("error: сеть"))["whisper"].status == "error"
    assert ReadinessChecker.ready([CheckResult("a", "ok", ""), CheckResult("b", "loading", "")]) is False
    assert ReadinessChecker.ready([CheckResult("a", "ok", ""), CheckResult("b", "warn", "")]) is True


def test_readiness_reports_prompts_for_kinds(env):
    kinds = MeetingKinds([MeetingKind("daily", "Дейли", "daily"), MeetingKind("retro", "Ретро", "retro")])
    result = checks(prompts=env.pipeline.prompts, kinds=kinds)["prompts"]
    assert result.status == "warn" and "retro" in result.detail


def test_startup_check_strict_refuses_to_start(env):
    from audio_assistant.endpoint.assistant_lifespan import _start_checks
    strict = AssistantSettings(startup_check="strict", preload_whisper=False)
    bad = type("P", (), {"transcriber": FakeModelFiles("ready"), "prompts": env.pipeline.prompts,
                         "kinds": env.pipeline.kinds})()
    with pytest.raises(RuntimeError, match="ollama"):
        _start_checks(strict, bad, FakeOllama(models=()), None)
    _start_checks(strict, bad, FakeOllama(), None)                        # всё есть -> стартует
    _start_checks(AssistantSettings(startup_check="warn", preload_whisper=False), bad, FakeOllama(models=()), None)
    assert _start_checks(AssistantSettings(startup_check="off"), bad, FakeOllama(models=()), None) is not None


def test_models_endpoint(env):
    env.app.state.assistant_readiness = ReadinessChecker(
        ollama=FakeOllama(models=()), transcriber=FakeModelFiles("ready"), transcriber_backend="faster_whisper")
    body = env.client.get("/assistant/models").json()
    assert body["ready"] is False
    assert {c["name"]: c["status"] for c in body["checks"]}["ollama"] == "error"


# ── утилиты ─────────────────────────────────────────────────────────────
def test_helpers():
    assert fill("a {date} {x}", transcript="T", date="1").endswith("Транскрипт:\nT")
    assert fill("{transcript}|{date}", transcript="T", date="1") == "T|1"
    assert fill('JSON {"a": 1} {transcript}', transcript="T") == 'JSON {"a": 1} T'
    text_ = "\n".join(f"[Роман] фраза {i}. Ещё одна." for i in range(100))
    chunks = split_text(text_, 300)
    assert max(map(len, chunks)) <= 300 and "\n".join(chunks) == text_
    assert max(map(len, split_text("слово " * 500, 100))) <= 100
    assert split_text("", 100) == [""]
    assert parse_rows('{"rows":[{"a":"x","b":["1","2"]},{"a":"","b":null},"мусор"]}', ("a", "b")) == [
        {"a": "x", "b": "1\n2"}]
    with pytest.raises(ValueError):
        parse_rows('{"rows": 5}', ("a",))
    with pytest.raises(ValueError):
        MeetingKinds([MeetingKind("daily", "Д", "daily")]).validate("x")
