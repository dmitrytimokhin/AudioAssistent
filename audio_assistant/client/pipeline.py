from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from observability.logging.setup import get_logger

from .analysis import AnalysisResult, MeetingAnalyzer
from .digest import DigestBuilder
from .domain import DailySummary, Team, Transcript
from .images import ImageAnalyzer
from .meeting_kinds import MeetingKinds
from .ports import HistoryRepository, NullRunStore, RunStore, SpeakerAttributor, Transcriber
from .prompt_runner import PromptResult, PromptRunner
from .prompts import FilePromptRepository
from .rendering import StatusRenderer
from .run_context import clear_progress, current_audience, current_person, current_run_id
from .summarizer import DailySummarizer

logger = get_logger(__name__)


@dataclass
class PipelineResult:
    transcript: Transcript
    summary: DailySummary
    text: str
    run_id: int | None = None


class Pipeline:
    """Оркестратор. Все зависимости внедряются снаружи (см. bootstrap.py).

    Три уровня API:
      * чистые шаги (transcribe / attribute / summarize / render) — ничего не сохраняют;
      * стадии (stage_*) — работают по run_id, читают вход и пишут результат в RunStore;
      * сквозные запуски (run_audio / run_transcript) — все стадии подряд.
    """

    def __init__(self, transcriber: Transcriber, attributor: SpeakerAttributor,
                 summarizer: DailySummarizer, prompts: FilePromptRepository,
                 history: HistoryRepository, renderer: StatusRenderer, team: Team,
                 store: RunStore | None = None, llm_attributor: SpeakerAttributor | None = None,
                 attribution_method: str = "rules",
                 prompt_runner: PromptRunner | None = None, kinds: MeetingKinds | None = None,
                 analyzer: MeetingAnalyzer | None = None, digest_builder: DigestBuilder | None = None,
                 image_analyzer: ImageAnalyzer | None = None):
        self.prompt_runner = prompt_runner
        self.kinds, self.analyzer, self.digest_builder = kinds, analyzer, digest_builder
        self.image_analyzer = image_analyzer
        self.transcriber, self.attributor, self.summarizer = transcriber, attributor, summarizer
        self.llm_attributor, self.attribution_method = llm_attributor, attribution_method
        self.prompts, self.history, self.renderer, self.team = prompts, history, renderer, team
        self.store = store or NullRunStore()
        # Whisper занимает все ядра: два одновременных запуска не быстрее, а медленнее двух по очереди
        self._transcribe_lock = threading.Lock()

    # ── чистые шаги ────────────────────────────────────────────────────────
    def transcribe(self, audio: Path) -> Transcript:
        return self.transcriber.transcribe(audio, prompt_hint=self.team.names_hint())

    def attribute(self, transcript: Transcript, method: str | None = None) -> Transcript:
        """method: "llm" | "rules"; по умолчанию из настроек. Размеченный транскрипт возвращается как есть."""
        if any(s.speaker for s in transcript.segments):  # говорящие уже размечены
            return transcript
        method = method or self.attribution_method
        if method == "rules":
            return self.attributor.attribute(transcript)
        if method == "llm":
            if self.llm_attributor is None:
                raise ValueError("Разметка моделью не настроена")
            return self.llm_attributor.attribute(transcript)
        raise ValueError(f"Неизвестный метод разметки: {method!r} (llm | rules)")

    def summarize(self, transcript: Transcript, audience: str, day: date) -> DailySummary:
        return self.summarizer.summarize(transcript, self.prompts.get(audience), day)

    def render(self, summary: DailySummary, audience: str) -> str:
        return self.renderer.render(summary, self.prompts.get(audience).layout)

    # ── стадии с сохранением (нужен RunStore с supports_stages) ───────────────
    def require_stages(self) -> None:
        if not self.store.supports_stages:
            raise NotImplementedError("Стадии по run_id требуют ASSISTANT_HISTORY_BACKEND=postgres")

    def meta(self, kind: str | None, tags: list[str] | None) -> tuple[str | None, list[str]]:
        """Проверенные тип и теги встречи (опечатка в типе -> ValueError, а не потерянная встреча)."""
        if self.kinds is None:
            return kind, list(tags or [])
        return self.kinds.validate(kind), self.kinds.normalize_tags(tags)

    def begin_transcription(self, audio: Path, day: date, recorded_at: datetime | None = None,
                            kind: str | None = None, tags: list[str] | None = None
                            ) -> tuple[int, bool]:
        """Заводит запуск в стадии transcribing, не транскрибируя. (run_id, создан_ли_новый):
        если файл уже транскрибируется, возвращается тот же запуск (повторный клик не плодит дубли)."""
        self.require_stages()
        if (active := self.store.find_active(audio.name)) is not None:
            return active, False
        kind, tags = self.meta(kind, tags)
        run_id = self.store.start(day, audio.name, recorded_at, kind, tags)
        self.store.set_stage(run_id, "transcribing")
        return run_id, True

    def finish_transcription(self, run_id: int, audio: Path) -> None:
        """Сама транскрибация запущенного begin_transcription; в фоне, ошибка пишется в запуск."""
        try:
            with self._guard(run_id):
                with self._transcribe_lock:
                    raw = self.transcribe(audio)
                self.store.save_raw(run_id, raw)
                self.store.set_stage(run_id, "transcribed")
        finally:
            clear_progress(run_id)

    def stage_transcribe(self, audio: Path, day: date, recorded_at: datetime | None = None,
                         kind: str | None = None, tags: list[str] | None = None) -> int:
        """Транскрибация с ожиданием результата (для скриптов; ноутбуку удобнее wait=false)."""
        self.require_stages()
        kind, tags = self.meta(kind, tags)
        run_id = self.store.start(day, audio.name, recorded_at, kind, tags)
        self.store.set_stage(run_id, "transcribing")
        self.finish_transcription(run_id, audio)
        return run_id

    def stage_attribute(self, run_id: int, method: str | None = None) -> Transcript:
        self.require_stages()
        with self._guard(run_id):
            attributed = self.attribute(self.store.load_raw(run_id), method)
            self.store.save_attributed(run_id, attributed)
            self.store.set_stage(run_id, "attributed")
            return attributed

    def stage_summarize(self, run_id: int, audience: str) -> DailySummary:
        self.require_stages()
        self.prompts.get(audience)
        with self._guard(run_id, audience):
            summary = self.summarize(self.store.load_attributed(run_id), audience,
                                     self.store.run_day(run_id))
            self.history.save(summary)  # заменяет статусы (run_id, audience)
            self.store.set_stage(run_id, "summarized")
            return summary

    def stage_report(self, run_id: int, audience: str) -> str:
        self.require_stages()
        with self._guard(run_id, audience):
            summary = self.history.load_summary(run_id, audience)
            if summary is None:
                raise LookupError(f"Нет статусов run_id={run_id}, audience={audience}: "
                                  "сначала выполните стадию summarize")
            text = self.render(summary, audience)
            self.store.save_report(run_id, audience, text)
            self.store.set_stage(run_id, "reported")
            return text

    def stage_process(self, run_id: int, audience: str, method: str | None = None) -> PipelineResult:
        """Все стадии после транскрибации по сохранённому сырому транскрипту."""
        attributed = self.stage_attribute(run_id, method)
        summary = self.stage_summarize(run_id, audience)
        return PipelineResult(attributed, summary, self.stage_report(run_id, audience), run_id)

    # ── сквозные запуски ──────────────────────────────────────────────────
    def run_audio(self, audio: Path, audience: str, day: date, save: bool = True,
                  recorded_at: datetime | None = None, kind: str | None = None,
                  tags: list[str] | None = None) -> PipelineResult:
        return self._run(lambda: self.transcribe(audio), audio.name, audience, day, save, recorded_at,
                         kind, tags)

    def run_transcript(self, transcript: Transcript, audience: str, day: date,
                       save: bool = True, kind: str | None = None,
                       tags: list[str] | None = None) -> PipelineResult:
        return self._run(lambda: transcript, transcript.source, audience, day, save,
                         kind=kind, tags=tags)

    def _run(self, get_raw, source: str, audience: str, day: date, save: bool,
             recorded_at: datetime | None = None, kind: str | None = None,
             tags: list[str] | None = None) -> PipelineResult:
        self.prompts.get(audience)  # неизвестная аудитория — до создания запуска
        kind, tags = self.meta(kind, tags)
        if not save:
            attributed = self.attribute(get_raw())
            summary = self.summarize(attributed, audience, day)
            return PipelineResult(attributed, summary, self.render(summary, audience))

        run_id = self.store.start(day, source, recorded_at, kind, tags)
        with self._guard(run_id, audience):
            raw = get_raw()
            self.store.save_raw(run_id, raw)
            attributed = self.attribute(raw)
            self.store.save_attributed(run_id, attributed)
            summary = self.summarize(attributed, audience, day)
            self.history.save(summary)
            text = self.render(summary, audience)
            self.store.save_report(run_id, audience, text)
            self.store.set_stage(run_id, "reported")
        logger.info("Pipeline finished", run_id=run_id, audience=audience,
                    people=len(summary.statuses),
                    without_data=sum(not s.has_data for s in summary.statuses))
        return PipelineResult(attributed, summary, text, run_id)

    def run_prompt(self, prompt: str, run_id: int | None = None, source: str = "raw",
                   transcript_text: str | None = None, system: str | None = None,
                   json_schema: dict | None = None) -> PromptResult:
        """Свой промпт к LLM. Текст берётся из запуска (source: raw | dialogue) или из transcript_text.
        Вызов попадает в журнал llm_calls с audience="custom"; сбой не помечает запуск как error."""
        if self.prompt_runner is None:
            raise NotImplementedError("Запуск произвольных промптов не настроен")
        if source not in ("raw", "dialogue"):
            raise ValueError(f"Неизвестный source: {source!r} (raw | dialogue)")
        if run_id is not None and transcript_text is not None:
            raise ValueError("Укажите либо run_id, либо transcript_text")
        if run_id is not None:
            self.require_stages()
            text = (self.store.load_raw(run_id).text if source == "raw"
                    else self.store.load_attributed(run_id).render())
        else:
            text = transcript_text
        with self._guard(run_id, "custom", mark_failed=False):
            return self.prompt_runner.run(prompt, text, system, json_schema)

    # ── тип встречи, разбор и сводка ─────────────────────────────────────
    def set_meta(self, run_id: int, **fields) -> dict:
        """Меняет kind и/или tags (передаются только заданные поля). Стадии и результаты не трогает."""
        self.require_stages()
        if "kind" in fields:
            fields["kind"], _ = self.meta(fields["kind"], None)
        if "tags" in fields:
            _, fields["tags"] = self.meta(None, fields["tags"])
        return self.store.update_meta(run_id, **fields)

    def analyze(self, run_id: int, prompt: str | None = None, source: str = "dialogue") -> AnalysisResult:
        """Разбор встречи промптом её типа (или явно заданным prompts/meetings/<prompt>). Результат -> analyses."""
        self.require_stages()
        if self.analyzer is None:
            raise NotImplementedError("Разбор встреч не настроен")
        run = self.store.run_info(run_id)
        name = self.analyzer.prompt_for(run, prompt)   # ошибки промпта -- до дорогих вызовов модели
        self.prompts.meeting_prompt(name)
        transcript = self.store.load_attributed(run_id) if source == "dialogue" else self.store.load_raw(run_id)
        with self._guard(run_id, f"analysis:{name}", mark_failed=False):
            result = self.analyzer.analyze(run, transcript, source, name)
        self.store.save_analysis(run_id, result.prompt_name, result.source, result.model,
                                 result.chunks, result.text)
        return result

    @contextmanager
    def _guard(self, run_id: int | None, audience: str = "", mark_failed: bool = True):
        """Контекст запуска для журнала LLM + пометка запуска как упавшего при ошибке."""
        tokens = (current_run_id.set(run_id), current_audience.set(audience), current_person.set(None))
        try:
            yield
        except Exception as exc:
            logger.error("Pipeline stage failed", run_id=run_id, audience=audience, error=str(exc))
            if mark_failed:
                self.store.fail(run_id, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            current_run_id.reset(tokens[0])
            current_audience.reset(tokens[1])
            current_person.reset(tokens[2])


def load_transcript(path: Path) -> Transcript:
    """.json (сохранённый транскрипт) или .txt (по строке на фразу)."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return Transcript.from_dict(json.loads(raw))
    return Transcript.from_plain_text(raw, source=str(path))
