"""Типы встреч, разбор встреч своим промптом, сводки в Excel, проверка моделей."""
from datetime import date
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from audio_assistant.client.digest import DigestJob
from audio_assistant.client.pipeline import Pipeline
from audio_assistant.endpoint.assistant_entities import (
    AnalyzeRequest,
    AnalyzeResponse,
    DigestDeleteResponse,
    DigestDetail,
    DigestOut,
    DigestRequest,
    KindOut,
    ModelCheckOut,
    ModelsResponse,
    PromptsResponse,
    RunOut,
    RunMetaRequest,
)
from audio_assistant.endpoint.assistant_settings import get_assistant_settings
from observability.logging.setup import get_logger

logger = get_logger(__name__)

# Подключается внутрь assistant_router (prefix /assistant), своего lifespan не имеет.
meetings_router = APIRouter(tags=["Audio assistant: meetings"])

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _pipeline(req: Request) -> Pipeline:
    pipeline = getattr(req.app.state, "assistant_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=500, detail="Assistant pipeline not initialized")
    return pipeline


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (LookupError, FileNotFoundError)):   # KeyError (нет промпта) -- тоже LookupError
        return HTTPException(status_code=404, detail=exc.args[0] if exc.args else str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=501, detail=str(exc))
    logger.error("Meetings request failed", error=str(exc), error_type=type(exc).__name__)
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


async def _call(func, *args):
    try:
        return await run_in_threadpool(func, *args)
    except Exception as exc:
        raise _http_error(exc) from exc


# ── справочники ──────────────────────────────────────────────────────────
@meetings_router.get("/prompts", response_model=PromptsResponse)
async def assistant_prompts(req: Request):
    """Что можно выбрать: типы встреч (и есть ли для них промпт), сводки, аудитории статусов."""
    pipeline = _pipeline(req)
    have = set(pipeline.prompts.meeting_prompts())
    kinds = [KindOut(name=n, title=pipeline.kinds.get(n).title, prompt=pipeline.kinds.get(n).prompt,
                     prompt_exists=pipeline.kinds.get(n).prompt in have) for n in pipeline.kinds.names]
    return PromptsResponse(kinds=kinds, meeting_prompts=sorted(have), digests=pipeline.prompts.digests(),
                           image_prompts=pipeline.prompts.image_prompts(), audiences=pipeline.prompts.available())


@meetings_router.get("/models", response_model=ModelsResponse)
async def assistant_models(req: Request):
    """Проверка моделей и зависимостей прямо сейчас: Ollama и её модель, Whisper, ffmpeg, Postgres, папки, промпты."""
    checker = getattr(req.app.state, "assistant_readiness", None)
    if checker is None:
        raise HTTPException(status_code=500, detail="Readiness checker not initialized")
    results = await run_in_threadpool(checker.run)
    return ModelsResponse(ready=checker.ready(results), checks=[ModelCheckOut(**r.to_dict()) for r in results])


# ── тип и теги встречи ───────────────────────────────────────────────────
@meetings_router.patch("/runs/{run_id}", response_model=RunOut)
async def assistant_set_meta(run_id: int, body: RunMetaRequest, req: Request):
    """Тип (kind) и теги встречи. Меняются только переданные поля."""
    pipeline = _pipeline(req)
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    return await _call(lambda: pipeline.set_meta(run_id, **fields))


# ── разбор встречи промптом её типа ──────────────────────────────────────
@meetings_router.post("/runs/{run_id}/analyze", response_model=AnalyzeResponse)
async def assistant_analyze(run_id: int, req: Request, body: AnalyzeRequest | None = None):
    """Разбор встречи промптом из prompts/meetings/<тип>/ (или prompts/meetings/<prompt>/). Результат сохраняется
    в analyses и виден в GET /runs/{id}; повтор с тем же промптом заменяет прежний."""
    body = body or AnalyzeRequest()
    pipeline = _pipeline(req)
    result = await _call(lambda: pipeline.analyze(run_id, body.prompt, body.source))
    return AnalyzeResponse(run_id=run_id, prompt=result.prompt_name, source=result.source,
                           model=result.model, chunks=result.chunks, text=result.text)


# ── сводки по периоду (Excel) ────────────────────────────────────────────
def _run_digest_in_background(builder, job: DigestJob) -> None:
    try:
        builder.execute(job)
    except Exception as exc:   # статус error уже записан в сводку
        logger.error("Background digest failed", digest_id=job.digest_id, error=str(exc))


@meetings_router.post("/digests", response_model=DigestOut)
async def assistant_create_digest(body: DigestRequest, req: Request, background: BackgroundTasks):
    """Собирает таблицу по встречам за период (по умолчанию 7 дней): на каждую встречу запрос к модели с промптом
    prompts/digests/<name>/, строки склеиваются и пишутся в xlsx (data/output/digests/). wait=false -- сразу ответ."""
    pipeline = _pipeline(req)
    await _call(pipeline.require_stages)
    builder = pipeline.digest_builder
    if builder is None:
        raise HTTPException(status_code=501, detail="Сводки требуют ASSISTANT_HISTORY_BACKEND=postgres")
    job = await _call(lambda: builder.prepare(
        body.name, body.date_from, body.date_to, body.kinds, body.tags, body.source, body.run_ids))
    if body.wait:
        await _call(builder.execute, job)
    else:
        background.add_task(_run_digest_in_background, builder, job)
    return await _call(builder._digests.get, job.digest_id)


@meetings_router.get("/digests", response_model=list[DigestOut])
async def assistant_digests(req: Request, limit: int = Query(20, ge=1, le=200)):
    builder = _digest_builder(req)
    return await _call(builder._digests.list, limit)


@meetings_router.get("/digests/{digest_id}", response_model=DigestDetail)
async def assistant_digest(digest_id: int, req: Request):
    """Ход и результат: статус, встречи (что обработано), строки таблицы. В ноутбуке: pd.DataFrame(rows)."""
    return await _call(_digest_builder(req)._digests.get, digest_id)


@meetings_router.get("/digests/{digest_id}/xlsx")
async def assistant_digest_xlsx(digest_id: int, req: Request):
    digest = await _call(_digest_builder(req)._digests.get, digest_id)
    if digest["status"] != "done" or not digest["file_name"]:
        raise HTTPException(status_code=409, detail=f"Сводка {digest_id}: статус {digest['status']}, файла ещё нет")
    path = Path(get_assistant_settings().output_dir) / digest["file_name"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Файл {digest['file_name']} удалён с диска")
    return FileResponse(path, media_type=XLSX_TYPE, filename=path.name)


@meetings_router.delete("/digests/{digest_id}", response_model=DigestDeleteResponse)
async def assistant_delete_digest(digest_id: int, req: Request, dry_run: bool = Query(False)):
    """Сводка из Postgres и её xlsx-файл на диске."""
    return await _delete_digests(req, digest_id=digest_id, everything=False, dry_run=dry_run)


@meetings_router.delete("/digests", response_model=DigestDeleteResponse)
async def assistant_delete_all_digests(req: Request, all: bool = Query(False), dry_run: bool = Query(False)):
    if not all:
        raise HTTPException(status_code=422, detail="Укажите all=true (удаляет ВСЕ сводки) или id в пути")
    return await _delete_digests(req, digest_id=None, everything=True, dry_run=dry_run)


def _digest_builder(req: Request):
    builder = _pipeline(req).digest_builder
    if builder is None:
        raise HTTPException(status_code=501, detail="Сводки требуют ASSISTANT_HISTORY_BACKEND=postgres")
    return builder


async def _delete_digests(req: Request, *, digest_id: int | None, everything: bool, dry_run: bool):
    store = _digest_builder(req)._digests
    result = await _call(lambda: store.delete(digest_id=digest_id, everything=everything, dry_run=dry_run))
    if digest_id is not None and not result["digest_ids"]:
        raise HTTPException(status_code=404, detail=f"Сводка {digest_id} не найдена")
    if not dry_run:
        root = Path(get_assistant_settings().output_dir)
        for name in result["files"]:
            (root / name).unlink(missing_ok=True)
    return result
