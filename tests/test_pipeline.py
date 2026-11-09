from datetime import date
from pathlib import Path

import pytest

from audio_assistant.client.domain import Team, Participant, Project
from audio_assistant.client.history_file import FileHistoryRepository
from audio_assistant.client.pipeline import load_transcript
from audio_assistant.client.prompts import FilePromptRepository
from audio_assistant.client.rendering import StatusRenderer
from audio_assistant.client.speakers.name_cue import NameCueAttributor
from audio_assistant.client.summarizer import DailySummarizer

ROOT = Path(__file__).resolve().parent.parent


class FakeLLM:
    def __init__(self):
        self.calls = []

    def generate(self, system, user):
        self.calls.append((system, user))
        return "1. Иванов Иван - сделал задачу"


def test_prompt_repository_lists_and_rejects_unknown():
    repo = FilePromptRepository(ROOT / "prompts")
    assert {"management", "project_managers", "team"} <= set(repo.available())
    with pytest.raises(KeyError):
        repo.get("nope")


def test_full_flow_with_fake_llm(team, tmp_path):
    llm, history = FakeLLM(), FileHistoryRepository(tmp_path)
    transcript = NameCueAttributor(team).attribute(
        load_transcript(ROOT / "examples" / "sample_transcript.txt"))
    profile = FilePromptRepository(ROOT / "prompts").get("management")
    summary = DailySummarizer(llm, history, team).summarize(transcript, profile, date(2026, 11, 14))
    text = StatusRenderer().render(summary, profile.layout)

    assert text.startswith("Дейли 14.11.2026\nПроект 3924:\n1. Гончар Роман - сделал задачу")
    assert len(llm.calls) == 9  # все участники говорили -> по вызову на каждого
    assert "Тимофеев Сергей - сделал задачу" in text


def test_silent_participant_skips_llm_and_history_feeds_next_day(tmp_path):
    team = Team((Project("1", (Participant("Иванов Иван", "1", ("Иван",)),
                               Participant("Петров Пётр", "1", ("Пётр",)))),))
    llm, history = FakeLLM(), FileHistoryRepository(tmp_path)
    profile = FilePromptRepository(ROOT / "prompts").get("management")
    summarizer = DailySummarizer(llm, history, team)
    attr = NameCueAttributor(team)

    from audio_assistant.client.domain import Transcript
    day1 = attr.attribute(Transcript.from_plain_text("Иван. Делал А."))
    s1 = summarizer.summarize(day1, profile, date(2026, 11, 13))
    history.save(s1)
    assert [s.has_data for s in s1.statuses] == [True, False]
    assert len(llm.calls) == 1

    summarizer.summarize(attr.attribute(Transcript.from_plain_text("Иван. Делал Б.")),
                         profile, date(2026, 11, 14))
    assert "сделал задачу" in llm.calls[-1][1]  # прошлый статус попал в промпт


def test_llm_failure_on_one_person_keeps_others(team, tmp_path):
    class FlakyLLM:
        def generate(self, system, user):
            if "Гончар Роман" in user:
                raise RuntimeError("ollama down")
            return "ок"

    transcript = NameCueAttributor(team).attribute(
        load_transcript(ROOT / "examples" / "sample_transcript.txt"))
    profile = FilePromptRepository(ROOT / "prompts").get("management")
    summary = DailySummarizer(FlakyLLM(), FileHistoryRepository(tmp_path), team).summarize(
        transcript, profile, date(2026, 11, 14))

    by_name = {s.full_name: s for s in summary.statuses}
    assert not by_name["Гончар Роман"].has_data
    assert by_name["Гончар Роман"].text == profile.layout.error_text
    assert by_name["Губриенко Денис"].text == "ок"


def test_pipeline_marks_run_failed_when_transcription_breaks(team, tmp_path):
    from audio_assistant.client.pipeline import Pipeline

    class Store:
        supports_stages = True

        def __init__(self): self.events = []
        def start(self, day, source, recorded_at=None, kind=None, tags=None): self.events.append("start"); return 7
        def save_raw(self, run_id, transcript): self.events.append("raw")
        def save_attributed(self, run_id, transcript): self.events.append("attributed")
        def set_stage(self, run_id, stage): self.events.append(stage)
        def save_report(self, run_id, audience, text): self.events.append("report")
        def fail(self, run_id, error): self.events.append(f"fail:{run_id}:{error}")

    class BrokenTranscriber:
        def transcribe(self, audio, prompt_hint=""): raise OSError("no ffmpeg")

    history, store = FileHistoryRepository(tmp_path), Store()
    pipeline = Pipeline(BrokenTranscriber(), NameCueAttributor(team),
                        DailySummarizer(FakeLLM(), history, team),
                        FilePromptRepository(ROOT / "prompts"), history, StatusRenderer(), team, store)
    with pytest.raises(OSError):
        pipeline.run_audio(Path("x.m4a"), "management", date(2026, 11, 14))
    assert store.events == ["start", "fail:7:OSError: no ffmpeg"]
    with pytest.raises(KeyError):               # неизвестная аудитория не создаёт запуск
        pipeline.run_audio(Path("x.m4a"), "nope", date(2026, 11, 14))
    assert store.events == ["start", "fail:7:OSError: no ffmpeg"]
