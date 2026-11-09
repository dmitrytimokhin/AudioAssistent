import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from audio_assistant.client.domain import Transcript
from audio_assistant.client.pipeline import Pipeline, PipelineResult
from audio_assistant.client.run_context import get_progress
from audio_assistant.client.recordings import (
    AUDIO_EXTENSIONS,
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
    LLMPromptRequest,
    LLMPromptResponse,
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
    TableRowsResponse,
    TablesResponse,
    TranscribeRequest,
    TranscribeResponse,
    UploadResponse,
    UtteranceOut,
)
from audio_assistant.endpoint.assistant_lifespan import assistant_lifespan
from audio_assistant.endpoint.assistant_settings import get_assistant_settings
from audio_assistant.endpoint.images_router import images_router
from audio_assistant.endpoint.meetings_router import meetings_router
from observability.logging.setup import get_logger

logger = get_logger(__name__)

assistant_router = APIRouter(
    prefix="/assistant",
    tags=["Audio assistant"],
    lifespan=assistant_lifespan,
)


# Тип встречи, разбор, сводки и проверка моделей -- в отдельном модуле, под тем же префиксом /assistant
assistant_router.include_router(meetings_router)
assistant_router.include_router(images_router)


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


def _transcribe_in_background(pipeline: Pipeline, run_id: int, path: Path) -> None:
    try:
        pipeline.finish_transcription(run_id, path)
    except Exception as exc:   # статус error уже записан в запуск, здесь только лог
        logger.error("Background transcription failed", run_id=run_id, error=str(exc))


def _split_tags(tags: str) -> list[str]:
    return [t for t in (x.strip() for x in tags.split(",")) if t]


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


@assistant_router.post("/audio", response_model=UploadResponse)
async def assistant_upload_audio(
    req: Request,
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
    transcribe: bool = Form(False),
    meeting_date: date | None = Form(None),
    kind: str | None = Form(None),
    tags: str = Form("", description="теги через запятую"),
):
    """Загрузка записи в data/audio под её именем. transcribe=true сразу запускает стадию 1
    (kind и tags относятся к ней)."""
    original = Path(file.filename or "").name
    if not original or Path(original).suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"Нужен аудиофайл: {sorted(AUDIO_EXTENSIONS)}")
    audio_dir = get_assistant_settings().audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / original
    if path.exists() and not overwrite:
        raise HTTPException(status_code=409, detail=f"{original} уже есть; overwrite=true заменит файл")
    with path.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    recorded_at = parse_recording_datetime(original)
    response = UploadResponse(file_name=original, size_bytes=path.stat().st_size, recorded_at=recorded_at)
    if transcribe:
        pipeline = _get_pipeline(req)
        day = _meeting_day(meeting_date, recorded_at)
        response.run_id = await _call(pipeline.stage_transcribe, path, day, recorded_at, kind, _split_tags(tags))
        response.meeting_date = day
        response.segments = _segments_out(await _call(pipeline.store.load_raw, response.run_id))
    logger.info("Audio uploaded", filename=original, size_bytes=response.size_bytes, run_id=response.run_id)
    return response


# ── отдельные стадии: каждую можно вызвать и перепроверить независимо ────────
@assistant_router.post("/transcribe", response_model=TranscribeResponse)
async def assistant_transcribe(body: TranscribeRequest, req: Request, background: BackgroundTasks):
    """Стадия 1. Файл из data/audio -> Whisper -> сырой транскрипт. Создаёт запуск (run_id).

    wait=true (по умолчанию) отвечает, когда транскрипт готов. wait=false отвечает сразу со статусом
    transcribing, а работа идёт в фоне: статус и прогресс в GET /runs/{id}. Файл, который уже
    транскрибируется, второй раз не запускается (already_running=true).
    """
    pipeline = _get_pipeline(req)
    path = await _call(_audio_path, body.file_name)
    recorded_at = parse_recording_datetime(path.name)
    day = _meeting_day(body.meeting_date, recorded_at)
    if not body.wait:
        run_id, created = await _call(pipeline.begin_transcription, path, day, recorded_at,
                                      body.kind, body.tags)
        if created:
            background.add_task(_transcribe_in_background, pipeline, run_id, path)
        return TranscribeResponse(run_id=run_id, meeting_date=day, recorded_at=recorded_at,
                                  status="transcribing", already_running=not created)
    run_id = await _call(pipeline.stage_transcribe, path, day, recorded_at, body.kind, body.tags)
    raw = await _call(pipeline.store.load_raw, run_id)
    return TranscribeResponse(run_id=run_id, meeting_date=day, recorded_at=recorded_at,
                              segments=_segments_out(raw))


@assistant_router.post("/llm", response_model=LLMPromptResponse)
async def assistant_llm(body: LLMPromptRequest, req: Request):
    """Свой промпт к модели поверх транскрипта запуска (или переданного текста). Не меняет статусы и отчёты;
    вызов пишется в журнал llm_calls (audience="custom")."""
    pipeline = _get_pipeline(req)
    result = await _call(lambda: pipeline.run_prompt(
        body.prompt, body.run_id, body.source, body.transcript_text, body.system, body.json_schema))
    return LLMPromptResponse(run_id=body.run_id, source=body.source if body.run_id else None,
                             model=result.model, duration_ms=result.duration_ms, text=result.text)


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
async def assistant_runs(
    req: Request,
    limit: int = Query(20, ge=1, le=1000),
    kind: str | None = Query(None, description="тип встречи"),
    tag: str | None = Query(None, description="тег"),
    date_from: date | None = Query(None, description="дата встречи от (включительно)"),
    date_to: date | None = Query(None, description="дата встречи до (включительно)"),
):
    """Последние запуски с фильтрами по типу, тегу и периоду (только history_backend=postgres)."""
    pipeline = _get_pipeline(req)
    await _call(pipeline.require_stages)
    runs = await _call(lambda: pipeline.store.list_runs(
        limit, kind=kind, tag=tag, date_from=date_from, date_to=date_to))
    return [{**r, "progress": get_progress(r["id"])} for r in runs]


@assistant_router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def assistant_run_detail(run_id: int, req: Request):
    """Всё по запуску: сырой транскрипт, диалог по говорящим, статусы и отчёты по аудиториям."""
    pipeline = _get_pipeline(req)
    await _call(pipeline.require_stages)
    detail = await _call(pipeline.store.detail, run_id)
    detail["run"]["progress"] = get_progress(run_id)
    return detail


# ── удаление (только history_backend=postgres) ─────────────────────────────
@assistant_router.get("/tables", response_model=TablesResponse)
async def assistant_tables(req: Request):
    """Сколько строк в каждой таблице Postgres."""
    return TablesResponse(counts=await _call(_get_pipeline(req).store.table_counts))


@assistant_router.get("/tables/{name}", response_model=TableRowsResponse)
async def assistant_table_rows(name: str, req: Request, run_id: int | None = None,
                               limit: int = Query(100, ge=1, le=5000)):
    """Строки таблицы (свежие первыми), опционально по запуску. В ноутбуке: pd.DataFrame(rows)."""
    return await _call(_get_pipeline(req).store.table_rows, name, run_id, limit)


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
    result = await _call(pipeline.run_transcript, transcript, audience, day, body.save, body.kind, body.tags)
    return _to_response(result, day, audience, with_transcript=True)


@assistant_router.post("/process-file", response_model=SummaryResponse)
async def assistant_process_file(body: ProcessFileRequest, req: Request):
    """Файл из data/audio -> все стадии подряд. Долгая операция: Whisper + LLM на каждого."""
    pipeline = _get_pipeline(req)
    audience = _resolve_audience(pipeline, body.audience)
    path = await _call(_audio_path, body.file_name)
    recorded_at = parse_recording_datetime(path.name)
    day = _meeting_day(body.meeting_date, recorded_at)
    result = await _call(pipeline.run_audio, path, audience, day, body.save, recorded_at, body.kind, body.tags)
    return _to_response(result, day, audience, body.include_transcript)


@assistant_router.post("/process-audio", response_model=SummaryResponse)
async def assistant_process_audio(
    req: Request,
    file: UploadFile = File(...),
    audience: str | None = Form(None),
    meeting_date: date | None = Form(None),
    save: bool = Form(True),
    include_transcript: bool = Form(False),
    kind: str | None = Form(None),
    tags: str = Form("", description="теги через запятую"),
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
        result = await _call(pipeline.run_audio, path, name, day, save, recorded_at, kind, _split_tags(tags))
    return _to_response(result, day, name, include_transcript)
