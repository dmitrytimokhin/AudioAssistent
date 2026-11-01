"""Composition root: единственное место, где выбираются конкретные реализации."""
from __future__ import annotations

from audio_assistant.endpoint.assistant_settings import AssistantSettings

from .history_file import FileHistoryRepository
from .pipeline import Pipeline
from .ports import HistoryRepository, LLMClient, RunStore
from .prompts import FilePromptRepository
from .rendering import StatusRenderer
from .speakers.llm_attributor import LLMSpeakerAttributor
from .speakers.name_cue import NameCueAttributor
from .summarizer import DailySummarizer
from .team_loader import load_team
from .transcription.faster_backend import FasterWhisperTranscriber
from .transcription.mlx_backend import MlxWhisperTranscriber

_DEFAULT_MODELS = {"mlx_whisper": "mlx-community/whisper-large-v3-turbo", "faster_whisper": "large-v3"}
_TRANSCRIBERS = {"mlx_whisper": MlxWhisperTranscriber, "faster_whisper": FasterWhisperTranscriber}


def build_transcriber(settings: AssistantSettings):
    model = settings.whisper_model or _DEFAULT_MODELS[settings.transcriber_backend]
    return _TRANSCRIBERS[settings.transcriber_backend](model=model, language=settings.whisper_language)


def build_history(settings: AssistantSettings) -> FileHistoryRepository:
    return FileHistoryRepository(settings.history_dir)


def build_pipeline(settings: AssistantSettings, llm: LLMClient, history: HistoryRepository,
                   store: RunStore | None = None) -> Pipeline:
    team = load_team(settings.team_file)
    prompts = FilePromptRepository(settings.prompts_dir)
    rules = NameCueAttributor(team)
    return Pipeline(
        transcriber=build_transcriber(settings),
        attributor=rules,
        llm_attributor=LLMSpeakerAttributor(
            llm, team, prompts, rules, chunk_chars=settings.attribution_chunk_chars,
            context_units=settings.attribution_context_units,
            unit_max_words=settings.attribution_unit_max_words),
        attribution_method=settings.attribution_method,
        summarizer=DailySummarizer(llm, history, team),
        prompts=prompts,
        history=history,
        renderer=StatusRenderer(),
        team=team,
        store=store,
    )
