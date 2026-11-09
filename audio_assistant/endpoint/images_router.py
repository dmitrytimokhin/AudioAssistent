"""Независимый разбор картинок: загрузка фото, разбор промптом (vision), история разборов."""
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from audio_assistant.client.images import ImageAnalyzer, ImageResult
from audio_assistant.client.meeting_kinds import MeetingKinds
from audio_assistant.endpoint.assistant_entities import (
    ImageAnalysisDeleteResponse,
    ImageAnalysisOut,
    ImageAnalyzeRequest,
    ImageFileOut,
    ImageResultOut,
    ImageUploadResponse,
)
from audio_assistant.endpoint.assistant_settings import get_assistant_settings
from observability.logging.setup import get_logger

logger = get_logger(__name__)

# Подключается внутрь assistant_router (prefix /assistant), своего lifespan не имеет.
images_router = APIRouter(prefix="/images", tags=["Audio assistant: images"])


def _analyzer(req: Request) -> ImageAnalyzer:
    pipeline = getattr(req.app.state, "assistant_pipeline", None)
    if pipeline is None or pipeline.image_analyzer is None:
        raise HTTPException(status_code=500, detail="Assistant pipeline not initialized")
    return pipeline.image_analyzer


def _store(req: Request):
    analyzer = _analyzer(req)
    if not analyzer.persists:
        raise HTTPException(status_code=501, detail="История разборов требует ASSISTANT_HISTORY_BACKEND=postgres")
    return analyzer._store


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (LookupError, FileNotFoundError)):
        return HTTPException(status_code=404, detail=exc.args[0] if exc.args else str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=501, detail=str(exc))
    logger.error("Image request failed", error=str(exc), error_type=type(exc).__name__)
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


async def _call(func, *args):
    try:
        return await run_in_threadpool(func, *args)
    except Exception as exc:
        raise _http_error(exc) from exc


def _split_tags(tags: str) -> list[str]:
    return MeetingKinds.normalize_tags(tags.split(","))


def _out(result: ImageResult) -> ImageResultOut:
    return ImageResultOut(files=result.files, analysis_id=result.analysis_id, model=result.model,
                          duration_ms=result.duration_ms, text=result.text, error=result.error)


async def _read_limited(upload: UploadFile) -> bytes:
    limit = get_assistant_settings().image_max_upload_mb * 1024 * 1024
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"{upload.filename}: больше {limit // 1024 // 1024} МБ")
    return data


# ── файлы ────────────────────────────────────────────────────────────────
@images_router.get("", response_model=list[ImageFileOut])
async def assistant_images(req: Request):
    """Фото в ASSISTANT_IMAGES_DIR (data/images), свежие первыми."""
    return await _call(_analyzer(req).list_files)


@images_router.post("", response_model=ImageUploadResponse)
async def assistant_upload_images(req: Request, files: list[UploadFile] = File(...),
                                  overwrite: bool = Form(False)):
    """Загрузка одного или нескольких фото в data/images под их именами (jpg, png, webp, bmp, gif)."""
    analyzer = _analyzer(req)
    saved = []
    for upload in files:
        data = await _read_limited(upload)
        saved.append(await _call(lambda u=upload, d=data: analyzer.save_file(u.filename or "", d, overwrite)))
    logger.info("Images uploaded", files=saved)
    return ImageUploadResponse(saved=saved)


# ── разбор ───────────────────────────────────────────────────────────────
@images_router.post("/analyze", response_model=list[ImageResultOut])
async def assistant_analyze_images(body: ImageAnalyzeRequest, req: Request):
    """Фото из data/images + промпт -> текст модели. mode=together: один запрос на все фото (один результат),
    mode=each: отдельный запрос и отдельный результат на каждое. Результаты пишутся в Postgres (save=true)."""
    analyzer = _analyzer(req)
    tags = MeetingKinds.normalize_tags(body.tags)
    results = await _call(lambda: analyzer.analyze(
        body.files, prompt_name=body.prompt_name, prompt=body.prompt, system=body.system, tags=tags,
        mode=body.mode, json_schema=body.json_schema, save=body.save))
    return [_out(r) for r in results]


@images_router.post("/analyze-upload", response_model=list[ImageResultOut])
async def assistant_analyze_uploaded_images(
    req: Request,
    files: list[UploadFile] = File(...),
    prompt_name: str | None = Form(None),
    prompt: str | None = Form(None),
    system: str | None = Form(None),
    tags: str = Form("", description="теги через запятую"),
    mode: str = Form("together"),
    save: bool = Form(True),
):
    """Загрузка и разбор одним вызовом. Фото сохраняются в data/images (занятое имя получает суффикс _1, _2...)."""
    analyzer = _analyzer(req)
    names = []
    for upload in files:
        data = await _read_limited(upload)
        names.append(await _call(lambda u=upload, d=data: analyzer.save_file(u.filename or "", d, unique=True)))
    results = await _call(lambda: analyzer.analyze(
        names, prompt_name=prompt_name, prompt=prompt, system=system, tags=_split_tags(tags),
        mode=mode, save=save))
    return [_out(r) for r in results]


# ── история разборов ─────────────────────────────────────────────────────
@images_router.get("/analyses", response_model=list[ImageAnalysisOut])
async def assistant_image_analyses(req: Request, limit: int = Query(20, ge=1, le=1000),
                                   tag: str | None = None, file_name: str | None = None):
    """Сохранённые разборы, свежие первыми; фильтры по тегу и имени файла."""
    store = _store(req)
    return await _call(lambda: store.list(limit, tag, file_name))


@images_router.get("/analyses/{analysis_id}", response_model=ImageAnalysisOut)
async def assistant_image_analysis(analysis_id: int, req: Request):
    return await _call(_store(req).get, analysis_id)


@images_router.delete("/analyses/{analysis_id}", response_model=ImageAnalysisDeleteResponse)
async def assistant_delete_image_analysis(analysis_id: int, req: Request, dry_run: bool = Query(False)):
    """Удаляет запись о разборе из Postgres; сам файл фото в data/images остаётся."""
    store = _store(req)
    result = await _call(lambda: store.delete(analysis_id=analysis_id, dry_run=dry_run))
    if not result["analysis_ids"]:
        raise HTTPException(status_code=404, detail=f"Разбор картинок {analysis_id} не найден")
    return result


@images_router.delete("/analyses", response_model=ImageAnalysisDeleteResponse)
async def assistant_delete_all_image_analyses(req: Request, all: bool = Query(False), dry_run: bool = Query(False)):
    if not all:
        raise HTTPException(status_code=422, detail="Укажите all=true (удаляет ВСЕ разборы) или id в пути")
    store = _store(req)
    return await _call(lambda: store.delete(everything=True, dry_run=dry_run))
