"""Разметка говорящих моделью: нарезка на фразы, схема ответа, чанки, fallback на правила."""
import json
from datetime import date

import pytest

from audio_assistant.client.domain import Segment, Transcript
from audio_assistant.client.llm_adapter import LoggedLLMClient, OllamaLLMAdapter
from audio_assistant.client.ports import NullCallLog
from audio_assistant.client.prompts import FilePromptRepository
from audio_assistant.client.speakers.llm_attributor import HOST, UNCHANGED, LLMSpeakerAttributor
from audio_assistant.client.speakers.name_cue import NameCueAttributor
from audio_assistant.client.speakers.units import NameIndex, build_units

from tests.conftest import ROOT

CPU = [
    (0, 28.9, "Раз, два, три, четыре, пять, шесть. Сегодня начнем с Дейли, начнем с Ромы. Я сегодня сделал то-то-то, "
              "провел три эксперимента, получил такой результат. Спасибо, Рома. Теперь переходим к Денису. "
              "Я сегодня провел такой-то эксперимент, получил первый, второй, третий результат. Спасибо, Денис. Теперь Катя."),
    (28.9, 39.8, "я ищу задачу самозанятых а вернее их идентификацию спасибо катя следующий"),
    (39.8, 51.2, "паша я делаю и п вернее предсказание перехода самозанятых в п спасибо паша"),
    (51.2, 65.4, "равен я заканчиваю прогноз за компаний спасибо"),
]


def transcript(rows=CPU):
    return Transcript([Segment(*r) for r in rows])


class ScriptedLLM:
    """Возвращает ответ, посчитанный функцией от (user, schema); запоминает вызовы."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def generate(self, system, user, json_schema=None):
        self.calls.append((system, user, json_schema))
        return self.answer(user, json_schema) if callable(self.answer) else self.answer


def build(team, llm, **kw):
    prompts = FilePromptRepository(ROOT / "prompts")
    return LLMSpeakerAttributor(llm, team, prompts, NameCueAttributor(team), **kw)


def decisions(*items):
    """Ответ модели: кто говорит со СЛЕДУЮЩЕЙ фразы после фразы с именем."""
    return json.dumps({"decisions": [{"unit": u, "next_speaker": n} for u, n in items]}, ensure_ascii=False)


def index_of(units, fragment):
    return next(i for i, u in enumerate(units, 1) if fragment in u.text)


# ── нарезка ────────────────────────────────────────────────────────────────
def test_units_cut_after_each_name_and_keep_text(team):
    units = build_units(transcript(), team)
    texts = [u.text for u in units]
    assert "я ищу задачу самозанятых а вернее их идентификацию спасибо катя" in texts
    assert "следующий паша" in texts                      # метка, разорванная границей сегмента
    assert "Сегодня начнем с Дейли, начнем с Ромы." in texts
    joined = " ".join(texts)
    assert joined.split() == " ".join(r[2] for r in CPU).split()    # ни одного слова не потеряно


def test_long_unpunctuated_piece_is_wrapped(team):
    units = build_units(Transcript([Segment(0, 1, " ".join(["слово"] * 70))]), team, max_words=30)
    assert [len(u.text.split()) for u in units] == [24, 24, 22]


def test_name_index_declensions_and_no_false_positive(team):
    idx = NameIndex(team)
    assert idx.find("начнем с Ромы, потом к Кате и Паше") == ["Гончар Роман", "Вертоградова Екатерина", "Зеленский Павел"]
    assert idx.find("Катим как есть") == []


# ── контекстная разметка ───────────────────────────────────────────────────
def labels(*names):
    return json.dumps({"decisions": [{"unit": i, "speaker": name}
                                    for i, name in enumerate(names, 1)]}, ensure_ascii=False)


def test_contextual_interruption_and_return_without_name(team):
    raw = Transcript([Segment(0, 1, "Начнем с Ромы. Я закончил отчёт. Паша, проверь. "
                                    "Да, это Паша. Проверю сегодня. А я пока продолжу свой отчёт.")])
    units = build_units(raw, team)
    # Имена разбивают предложения, но все слова сохраняются; обращение остаётся Роману.
    expected = [HOST, "Гончар Роман", "Гончар Роман", "Гончар Роман",
                "Зеленский Павел", "Зеленский Павел", "Гончар Роман"]
    assert len(units) == len(expected)
    llm = ScriptedLLM(labels(*expected))
    out = build(team, llm).attribute(raw)
    assert out.meta["fallback_chunks"] == []
    assert [s.speaker or HOST for s in out.segments] == expected
    assert out.text.split() == raw.text.split()
    schema = llm.calls[0][2]
    assert "Агафонов Алексей" in schema["properties"]["decisions"]["items"]["properties"]["speaker"]["enum"]


def test_no_names_still_analyzed(team):
    llm = ScriptedLLM(labels(HOST, HOST))
    out = build(team, llm).attribute(Transcript([Segment(0, 1, "Задача готова. Когда релиз?")]))
    assert len(llm.calls) == 1 and out.meta["fallback_chunks"] == []


def test_chunk_has_previous_and_future_context(team):
    llm = ScriptedLLM(lambda user, schema: json.dumps({"decisions": [
        {"unit": int(user.split("Требуется решение по фразам: ")[1].split()[0]),
         "speaker": "Гончар Роман"}]}))
    raw = Transcript([Segment(i, i+1, t) for i, t in enumerate(
        ["Первый отчёт готов.", "Когда релиз?", "Продолжаю отчёт."])])
    out = build(team, llm, chunk_chars=1, context_units=1).attribute(raw)
    assert out.meta["fallback_chunks"] == [] and len(llm.calls) == 3
    assert "2: Когда релиз?" in llm.calls[0][1]
    assert "[Гончар Роман] Первый отчёт готов." in llm.calls[1][1]
    assert "3: Продолжаю отчёт." in llm.calls[1][1]


@pytest.mark.parametrize("bad", ["не json", '{"x": 1}', '{"decisions": []}',
    '{"decisions": [{"unit": 1, "speaker": "чужой"}]}',
    '{"decisions": [{"unit": 99, "speaker": "ведущий"}]}',
    '{"decisions": [{"unit": true, "speaker": "ведущий"}]}',
    '{"decisions": [{"unit": 1, "speaker": "ведущий"}, {"unit": 1, "speaker": "ведущий"}]}'])
def test_bad_model_output_falls_back(team, bad):
    out = build(team, ScriptedLLM(bad)).attribute(transcript())
    assert out.meta["fallback_chunks"] == [1]
    assert "Гончар Роман" in {s.speaker for s in out.segments}


def test_exception_fallback_keeps_current_speaker(team):
    count = 0
    def answer(user, schema):
        nonlocal count
        count += 1
        if count == 2:
            raise TimeoutError("timeout")
        return labels("Гончар Роман")
    out = build(team, ScriptedLLM(answer), chunk_chars=1).attribute(
        Transcript([Segment(0, 1, "Мой отчёт готов."), Segment(1, 2, "Паша, проверь.")]))
    assert out.meta["fallback_chunks"] == [2, 3]
    assert {s.speaker for s in out.segments} == {"Гончар Роман"}


# ── пайплайн, адаптер, клиент ──────────────────────────────────────────────
def test_pipeline_selects_method_and_rejects_unknown(team, tmp_path):
    from audio_assistant.client.bootstrap import build_pipeline
    from audio_assistant.client.history_file import FileHistoryRepository
    from audio_assistant.endpoint.assistant_settings import AssistantSettings

    llm = ScriptedLLM(decisions())
    pipeline = build_pipeline(AssistantSettings(history_dir=tmp_path, attribution_method="llm"), llm,
                              FileHistoryRepository(tmp_path))
    raw = transcript()
    assert pipeline.attribute(raw).meta["method"] == "llm" and len(llm.calls) == 1
    assert pipeline.attribute(raw, "rules").meta["method"] == "rules" and len(llm.calls) == 1
    with pytest.raises(ValueError, match="Неизвестный метод"):
        pipeline.attribute(raw, "magic")


def test_adapter_and_logged_client_pass_json_schema_and_raise_token_limit():
    class FakeOllama:
        is_connected = True
        class settings:  # noqa: N801
            model_name = "m"

        def __init__(self): self.kw = None
        def query(self, **kw): self.kw = kw; return "{}"

    fake = FakeOllama()
    llm = LoggedLLMClient(OllamaLLMAdapter(fake, 0.1, 512), NullCallLog())
    llm.generate("s", "u")
    assert fake.kw["json_schema"] is None and fake.kw["max_tokens"] == 512
    llm.generate("s", "u", json_schema={"type": "object"})
    assert fake.kw["json_schema"] == {"type": "object"} and fake.kw["max_tokens"] >= 2048


def test_ollama_client_sends_format(monkeypatch):
    import requests
    from ollama_client.client.ollama_client import OllamaClient
    from ollama_client.endpoint.ollama_settings import OllamaSettings

    sent = {}

    class Resp:
        def raise_for_status(self): ...
        def json(self): return {"message": {"content": '{"turns": []}'}}

    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.update(json) or Resp())
    client = OllamaClient(OllamaSettings())
    client.is_connected = True
    client.query("sys", "usr", 0.1, 100, json_schema={"type": "object"})
    assert sent["format"] == {"type": "object"} and sent["think"] is False
    sent.clear()
    client.query("sys", "usr", 0.1, 100)
    assert "format" not in sent
