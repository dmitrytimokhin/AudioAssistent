"""Composition root: единственное место, где выбираются конкретные реализации."""
from __future__ import annotations

from audio_assistant.endpoint.assistant_settings import AssistantSettings

from .analysis import MeetingAnalyzer
from .digest import DigestBuilder
from .images import ImageAnalyzer
from .history_file import FileHistoryRepository
from .meeting_kinds import load_meeting_kinds
from .pipeline import Pipeline
from .ports import HistoryRepository, LLMClient, RunStore
from .prompt_runner import PromptRunner
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
    options = ({"beam_size": settings.whisper_beam_size, "cpu_threads": settings.whisper_cpu_threads}
               if settings.transcriber_backend == "faster_whisper" else {})
    return _TRANSCRIBERS[settings.transcriber_backend](model=model, language=settings.whisper_language, **options)


def build_history(settings: AssistantSettings) -> FileHistoryRepository:
    return FileHistoryRepository(settings.history_dir)


def build_pipeline(settings: AssistantSettings, llm: LLMClient, history: HistoryRepository,
                   store: RunStore | None = None, digests=None, image_store=None) -> Pipeline:
    team = load_team(settings.team_file)
    prompts = FilePromptRepository(settings.prompts_dir)
    rules = NameCueAttributor(team)
    kinds = load_meeting_kinds(settings.kinds_file)
    digest_builder = (DigestBuilder(llm, prompts, team, kinds, store, digests, settings.output_dir,
                                    settings.analysis_chunk_chars)
                      if store is not None and digests is not None else None)
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
        prompt_runner=PromptRunner(llm),
        kinds=kinds,
        analyzer=MeetingAnalyzer(llm, prompts, team, kinds, settings.analysis_chunk_chars),
        digest_builder=digest_builder,
        image_analyzer=ImageAnalyzer(llm, prompts, settings.images_dir, image_store,
                                     settings.image_max_side, settings.image_max_per_request),
    )
