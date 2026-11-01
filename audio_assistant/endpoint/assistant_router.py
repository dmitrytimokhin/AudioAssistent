import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from audio_assistant.client.domain import Transcript
from audio_assistant.client.pipeline import Pipeline, PipelineResult
from audio_assistant.client.recordings import (
    list_recordings,
    parse_recording_datetime,
    resolve_recording,
)
from audio_assistant.endpoint.assistant_entities import (
    AssistantHealthResponse,
    AttributeRequest,
    AttributeResponse,
    AudiencesResponse,
    DeleteResponse,
    AudioFileOut,
    ProcessFileRequest,
    ReportResponse,
    RunDetailResponse,
    RunOut,
    SegmentOut,
    StageRequest,
    StatusOut,
    SummarizeRequest,
    SummarizeStageResponse,
    SummaryResponse,
    TranscribeRequest,
    TranscribeResponse,
    UtteranceOut,
)
from audio_assistant.endpoint.assistant_lifespan import assistant_lifespan
from audio_assistant.endpoint.assistant_settings import get_assistant_settings
from observability.logging.setup import get_logger

logger = get_logger(__name__)

assistant_router = APIRouter(
    prefix="/assistant",
    tags=["Audio assistant"],
    lifespan=assistant_lifespan,
)


# ── общее ─────────────────────────────────────────────────────────────────
def _get_pipeline(req: Request) -> Pipeline:
    pipeline = getattr(req.app.state, "assistant_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=500, detail="Assistant pipeline not initialized")
    return pipeline


def _resolve_audience(pipeline: Pipeline, audience: str | None) -> str:
    name = audience or get_assistant_settings().default_audience
    if name not in pipeline.prompts.available():
        raise HTTPException(
            status_code=422,
            detail=f"Unknown audience '{name}'. Available: {pipeline.prompts.available()}",
        )
    return name


def _http_error(exc: Exception) -> HTTPException:
    """Единое отображение ошибок пайплайна в HTTP-коды."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (LookupError, FileNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=501, detail=str(exc))
    logger.error("Assistant request failed", error=str(exc), error_type=type(exc).__name__)
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


async def _call(func, *args):
    """Блокирующий шаг (Whisper, LLM, БД) -- в threadpool, ошибки -- в HTTP-коды."""
    try:
        return await run_in_threadpool(func, *args)
    except Exception as exc:
        raise _http_error(exc) from exc


def _segments_out(transcript: Transcript) -> list[SegmentOut]:
    return [SegmentOut(start=s.start, end=s.end, speaker=s.speaker, text=s.text)
            for s in transcript.segments]


def _dialogue_out(transcript: Transcript) -> list[UtteranceOut]:
    return [UtteranceOut(speaker=s.speaker, start=s.start, end=s.end, text=s.text)
            for s in transcript.merge_turns().segments]


def _to_response(result: PipelineResult, day: date, audience: str, with_transcript: bool) -> SummaryResponse:
    return SummaryResponse(
        run_id=result.run_id,
        meeting_date=day,
        audience=audience,
        text=result.text,
        statuses=[StatusOut(**vars(s)) for s in result.summary.statuses],
        transcript=_segments_out(result.transcript.merge_turns()) if with_transcript else None,
    )


def _meeting_day(explicit: date | None, recorded_at: datetime | None) -> date:
    return explicit or (recorded_at.date() if recorded_at else date.today())


def _audio_path(file_name: str) -> Path:
    return resolve_recording(get_assistant_settings().audio_dir, file_name)


# ── справочные ─────────────────────────────────────────────────────────────
@assistant_router.get("/health", response_model=AssistantHealthResponse)
async def assistant_health(req: Request):
    pipeline = _get_pipeline(req)
    settings = get_assistant_settings()
    return AssistantHealthResponse(
        status="ok",
        history_backend=settings.history_backend,
        transcriber_backend=settings.transcriber_backend,
        stages_available=pipeline.store.supports_stages,
        audiences=pipeline.prompts.available(),
        participants=len(pipeline.team.participants),
    )


@assistant_router.get("/audiences", response_model=AudiencesResponse)
async def assistant_audiences(req: Request):
    pipeline = _get_pipeline(req)
    return AudiencesResponse(default=get_assistant_settings().default_audience,
                             audiences=pipeline.prompts.available())


@assistant_router.get("/audio", response_model=list[AudioFileOut])
async def assistant_audio_files():
    """Записи в ASSISTANT_AUDIO_DIR (data/audio); время записи берётся из имени файла."""
    return list_recordings(get_assistant_settings().audio_dir)


# ── отдельные стадии: каждую можно вызвать и перепроверить независимо ────────
@assistant_router.post("/transcribe", response_model=TranscribeResponse)
async def assistant_transcribe(body: TranscribeRequest, req: Request):
    """Стадия 1. Файл из data/audio -> Whisper -> сырой транскрипт. Создаёт запуск (run_id)."""
    pipeline = _get_pipeline(req)
    path = await _call(_audio_path, body.file_name)
    recorded_at = parse_recording_datetime(path.name)
    day = _meeting_day(body.meeting_date, recorded_at)
    run_id = await _call(pipeline.stage_transcribe, path, day, recorded_at)
    raw = await _call(pipeline.store.load_raw, run_id)
    return TranscribeResponse(run_id=run_id, meeting_date=day, recorded_at=recorded_at,
                              segments=_segments_out(raw))


@assistant_router.post("/runs/{run_id}/attribute", response_model=AttributeResponse)
async def assistant_attribute(run_id: int, req: Request, body: AttributeRequest | None = None):
    """Стадия 2. Сырой транскрипт -> разбивка по говорящим. Можно повторять и сравнивать методы:
    llm (модель размечает чанками) или rules (регулярные выражения по именным меткам)."""
    pipeline = _get_pipeline(req)
    attributed = await _call(pipeline.stage_attribute, run_id, body.method if body else None)
    spoken = set(attributed.text_by_speaker())
    meta = attributed.meta
    return AttributeResponse(
        run_id=run_id,
        method=meta.get("method", "existing"),
        chunks=meta.get("chunks"),
        fallback_chunks=meta.get("fallback_chunks", []),
        dialogue=_dialogue_out(attributed),
        without_utterances=[p.full_name for p in pipeline.team.participants
                            if p.full_name not in spoken],
    )


@assistant_router.post("/runs/{run_id}/summarize", response_model=SummarizeStageResponse)
async def assistant_summarize_stage(run_id: int, body: StageRequest, req: Request):
    """Стадия 3. Реплики -> статус каждого сотрудника (LLM). Повтор заменяет статусы аудитории."""
    pipeline = _get_pipeline(req)
    audience = _resolve_audience(pipeline, body.audience)
    summary = await _call(pipeline.stage_summarize, run_id, audience)
    return SummarizeStageResponse(run_id=run_id, audience=audience,
                                  statuses=[StatusOut(**vars(s)) for s in summary.statuses])


@assistant_router.post("/runs/{run_id}/report", response_model=ReportResponse)
async def assistant_report(run_id: int, body: StageRequest, req: Request):
    """Стадия 4. Готовые статусы -> итоговый текст по раскладке аудитории (без LLM)."""
    pipeline = _get_pipeline(req)
    audience = _resolve_audience(pipeline, body.audience)
    text = await _call(pipeline.stage_report, run_id, audience)
    return ReportResponse(run_id=run_id, audience=audience, text=text)


@assistant_router.post("/runs/{run_id}/process", response_model=SummaryResponse)
async def assistant_process_run(run_id: int, body: StageRequest, req: Request):
    """Стадии 2-4 подряд по уже сохранённому сырому транскрипту."""
    pipeline = _get_pipeline(req)
    audience = _resolve_audience(pipeline, body.audience)
    result = await _call(pipeline.stage_process, run_id, audience, body.attribution_method)
    day = await _call(pipeline.store.run_day, run_id)
    return _to_response(result, day, audience, with_transcript=False)


@assistant_router.get("/runs", response_model=list[RunOut])
async def assistant_runs(req: Request, limit: int = Query(20, ge=1, le=200)):
    """Последние запуски (только history_backend=postgres)."""
    pipeline = _get_pipeline(req)
    await _call(pipeline.require_stages)
    return await _call(pipeline.store.list_runs, limit)


@assistant_router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def assistant_run_detail(run_id: int, req: Request):
    """Всё по запуску: сырой транскрипт, диалог по говорящим, статусы и отчёты по аудиториям."""
    pipeline = _get_pipeline(req)
    await _call(pipeline.require_stages)
    return await _call(pipeline.store.detail, run_id)


# ── удаление (только history_backend=postgres) ─────────────────────────────
@assistant_router.delete("/runs/{run_id}", response_model=DeleteResponse)
async def assistant_delete_run(run_id: int, req: Request,
                               dry_run: bool = Query(False, description="только посчитать строки")):
    """Запуск по id и всё связанное: транскрипты, реплики, статусы, отчёты, журнал вызовов LLM."""
    pipeline = _get_pipeline(req)
    await _call(pipeline.require_stages)
    result = await _call(lambda: pipeline.store.delete(run_id=run_id, dry_run=dry_run))
    if not result["run_ids"]:
        raise HTTPException(status_code=404, detail=f"Запуск {run_id} не найден")
    return result


@assistant_router.delete("/runs", response_model=DeleteResponse)
async def assistant_delete_runs(
    req: Request,
    file_name: str | None = Query(None, description="имя аудиофайла (meeting_runs.source)"),
    meeting_date: date | None = Query(None, description="дата встречи YYYY-MM-DD"),
    all: bool = Query(False, description="очистить ВСЕ таблицы; нельзя сочетать с фильтрами"),
    dry_run: bool = Query(False, description="только посчитать строки"),
):
    """Удаление по имени файла и/или дате (фильтры через AND) либо целиком (all=true).
    Рекомендуется сначала вызвать с dry_run=true."""
    pipeline = _get_pipeline(req)
    await _call(pipeline.require_stages)
    if all and (file_name or meeting_date):
        raise HTTPException(status_code=422, detail="all=true нельзя сочетать с file_name/meeting_date")
    if not all and not (file_name or meeting_date):
        raise HTTPException(
            status_code=422,
            detail="Укажите file_name и/или meeting_date, либо all=true для полной очистки")
    return await _call(lambda: pipeline.store.delete(
        file_name=file_name, meeting_date=meeting_date, everything=all, dry_run=dry_run))


# ── сквозные запуски ──────────────────────────────────────────────────────
@assistant_router.post("/summarize", response_model=SummaryResponse)
async def assistant_summarize(body: SummarizeRequest, req: Request):
    """Текст расшифровки -> атрибуция по именам -> статус для выбранной аудитории."""
    pipeline = _get_pipeline(req)
    audience = _resolve_audience(pipeline, body.audience)
    day = body.meeting_date or date.today()
    transcript = Transcript.from_plain_text(body.text, source="api:text")
    result = await _call(pipeline.run_transcript, transcript, audience, day, body.save)
    return _to_response(result, day, audience, with_transcript=True)


@assistant_router.post("/process-file", response_model=SummaryResponse)
async def assistant_process_file(body: ProcessFileRequest, req: Request):
    """Файл из data/audio -> все стадии подряд. Долгая операция: Whisper + LLM на каждого."""
    pipeline = _get_pipeline(req)
    audience = _resolve_audience(pipeline, body.audience)
    path = await _call(_audio_path, body.file_name)
    recorded_at = parse_recording_datetime(path.name)
    day = _meeting_day(body.meeting_date, recorded_at)
    result = await _call(pipeline.run_audio, path, audience, day, body.save, recorded_at)
    return _to_response(result, day, audience, body.include_transcript)


@assistant_router.post("/process-audio", response_model=SummaryResponse)
async def assistant_process_audio(
    req: Request,
    file: UploadFile = File(...),
    audience: str | None = Form(None),
    meeting_date: date | None = Form(None),
    save: bool = Form(True),
    include_transcript: bool = Form(False),
):
    """Загрузка аудио -> все стадии подряд. Для записей, которых ещё нет в data/audio."""
    pipeline = _get_pipeline(req)
    name = _resolve_audience(pipeline, audience)
    original = Path(file.filename or "audio").name
    recorded_at = parse_recording_datetime(original)
    day = _meeting_day(meeting_date, recorded_at)

    with tempfile.TemporaryDirectory(prefix="audio_") as tmp:
        path = Path(tmp) / original
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        logger.info("Audio received", filename=original, size_bytes=path.stat().st_size)
        result = await _call(pipeline.run_audio, path, name, day, save, recorded_at)
    return _to_response(result, day, name, include_transcript)
