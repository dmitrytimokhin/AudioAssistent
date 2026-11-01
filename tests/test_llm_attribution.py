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


# ── разметка ───────────────────────────────────────────────────────────────
def test_decisions_label_text_verbatim_and_schema_is_narrowed(team):
    units = build_units(transcript(), team)
    at = lambda frag: index_of(units, frag)   # noqa: E731
    llm = ScriptedLLM(decisions(
        (at("начнем с Ромы"), "Гончар Роман"), (at("Спасибо, Рома"), HOST),
        (at("переходим к Денису"), "Губриенко Денис"), (at("Спасибо, Денис"), HOST),
        (at("Теперь Катя"), "Вертоградова Екатерина"), (at("спасибо катя"), UNCHANGED),
        (at("следующий паша"), "Зеленский Павел"), (at("спасибо паша"), HOST),
    ))
    out = build(team, llm).attribute(transcript())

    assert out.meta["chunks"] == 1 and out.meta["fallback_chunks"] == [] and len(llm.calls) == 1
    # слова не потеряны и не изменены
    assert " ".join(s.text for s in out.segments).split() == " ".join(r[2] for r in CPU).split()
    by = [(s.speaker, s.text[:20]) for s in out.merge_turns().segments]
    assert by == [
        (None, "Раз, два, три, четыр"),
        ("Гончар Роман", "Я сегодня сделал то-"),
        (None, "Теперь переходим к Д"),                    # фраза с именем остаётся за прежним говорящим
        ("Губриенко Денис", "Я сегодня провел так"),
        (None, "Теперь Катя."),
        ("Вертоградова Екатерина", "я ищу задачу самозан"),
        ("Зеленский Павел", "я делаю и п вернее п"),
        (None, "равен я заканчиваю п"),
    ]

    _, user, schema = llm.calls[0]
    enum = schema["properties"]["decisions"]["items"]["properties"]["next_speaker"]["enum"]
    # допустимы только «без изменений», ведущий и названные в чанке: придумать «Алексея» модель не может
    assert enum[:2] == [UNCHANGED, HOST] and set(enum[2:]) == {
        "Гончар Роман", "Губриенко Денис", "Вертоградова Екатерина", "Зеленский Павел"}
    assert "[названо: Зеленский Павел]" in user and "Агафонов Алексей" not in user
    assert "Требуется решение по фразам:" in user


def test_no_names_means_no_llm_call_and_previous_speaker_stays(team):
    llm = ScriptedLLM("не должен вызываться")
    out = build(team, llm).attribute(Transcript([Segment(0, 1, "Сделал задачу."), Segment(1, 2, "Всё работает.")]))
    assert llm.calls == [] and {s.speaker for s in out.segments} == {None}


def test_model_omitting_decisions_changes_nobody(team):
    out = build(team, ScriptedLLM(decisions())).attribute(transcript())
    assert {s.speaker for s in out.segments} == {None} and out.meta["fallback_chunks"] == []


def test_decision_on_last_unit_of_chunk_carries_into_next_chunk(team):
    """Регрессия: «Алексей.» последняя фраза чанка -> со следующего чанка говорит Алексей."""
    rows = [(i, i + 1, t) for i, t in enumerate(
        ["Начнем с Ромы.", "Сделал первую задачу.", "Сделал вторую задачу.", "Спасибо, Рома."])]
    calls = []

    def answer(user, schema):
        calls.append(user)
        return decisions((1, "Гончар Роман")) if len(calls) == 1 else decisions((4, HOST))

    out = build(team, ScriptedLLM(answer), chunk_chars=30).attribute(Transcript([Segment(*r) for r in rows]))
    assert out.meta["chunks"] == 4 and len(calls) == 2 and out.meta["fallback_chunks"] == []
    assert [s.speaker for s in out.segments] == [None, "Гончар Роман", "Гончар Роман", "Гончар Роман"]
    assert "Текущий говорящий на момент начала фрагмента: Гончар Роман" in calls[1]
    assert "(начало записи)" in calls[0] and "[Гончар Роман] Сделал вторую задачу." in calls[1]


@pytest.mark.parametrize("bad", [
    "не json",
    '{"x": 1}',
    '{"decisions": [{"unit": 3, "next_speaker": "ведущий"}]}',                 # решение по фразе без имени
    '{"decisions": [{"unit": 1, "next_speaker": "Зеленский Павел"}]}',         # в этой фразе его не называли
    '{"decisions": [{"unit": 99, "next_speaker": "ведущий"}]}',                # вне чанка
])
def test_bad_model_output_falls_back_to_rules_for_that_chunk(team, bad):
    out = build(team, ScriptedLLM(bad)).attribute(transcript())
    assert out.meta["fallback_chunks"] == [1]
    # правила дают верную раскладку по реальной записи
    assert {s.speaker for s in out.segments} >= {"Гончар Роман", "Губриенко Денис",
                                                  "Вертоградова Екатерина", "Зеленский Павел"}


def test_llm_exception_falls_back_only_failed_chunk(team):
    rows = [(i, i + 1, t) for i, t in enumerate(
        ["Начнем с Ромы.", "Сделал первую задачу.", "Теперь Катя.", "Сделала вторую задачу."])]
    state = {"n": 0}

    def answer(user, schema):
        state["n"] += 1
        if state["n"] == 2:
            raise TimeoutError("ollama timeout")
        return decisions((1, "Гончар Роман"))

    out = build(team, ScriptedLLM(answer), chunk_chars=30).attribute(Transcript([Segment(*r) for r in rows]))
    assert out.meta["fallback_chunks"] == [3]
    # правила продолжили с текущего говорящего (Роман) и передали слово Кате; чанк 4 без имён унаследовал её
    assert [(s.speaker, s.text) for s in out.segments] == [
        (None, "Начнем с Ромы."), ("Гончар Роман", "Сделал первую задачу."),
        ("Вертоградова Екатерина", "Сделала вторую задачу.")]


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
