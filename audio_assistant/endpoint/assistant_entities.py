from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel


class StatusOut(BaseModel):
    full_name: str
    project_id: str
    text: str
    has_data: bool


class SegmentOut(BaseModel):
    start: float
    end: float
    speaker: Optional[str] = None
    text: str


class UtteranceOut(BaseModel):
    """Реплика в диалоге: подряд идущие фразы одного говорящего склеены."""

    speaker: Optional[str] = None   # None — до первой именной метки
    start: float
    end: float
    text: str


class AudioFileOut(BaseModel):
    file_name: str
    size_bytes: int
    recorded_at: Optional[datetime] = None   # из имени файла YYYYMMDDHHMMSS


# ── сквозные запуски ────────────────────────────────────────────────────────
class SummarizeRequest(BaseModel):
    """Расшифровка встречи текстом (строка = фраза) -> статус."""

    text: str
    audience: Optional[str] = None       # по умолчанию ASSISTANT_DEFAULT_AUDIENCE
    meeting_date: Optional[date] = None  # по умолчанию сегодня
    save: bool = True                    # False — не писать историю и журнал запуска


class ProcessFileRequest(BaseModel):
    """Файл из data/audio -> транскрипт -> говорящие -> статус."""

    file_name: str
    audience: Optional[str] = None
    meeting_date: Optional[date] = None  # по умолчанию дата из имени файла, иначе сегодня
    save: bool = True
    include_transcript: bool = False


class SummaryResponse(BaseModel):
    run_id: Optional[int] = None
    meeting_date: date
    audience: str
    text: str                       # готовый статус по раскладке аудитории
    statuses: list[StatusOut]
    transcript: Optional[list[SegmentOut]] = None


# ── отдельные стадии ───────────────────────────────────────────────────────
class TranscribeRequest(BaseModel):
    file_name: str
    meeting_date: Optional[date] = None


class TranscribeResponse(BaseModel):
    run_id: int
    meeting_date: date
    recorded_at: Optional[datetime] = None
    segments: list[SegmentOut]


class AttributeRequest(BaseModel):
    """method не задан -> ASSISTANT_ATTRIBUTION_METHOD."""

    method: Optional[Literal["llm", "rules"]] = None


class StageRequest(BaseModel):
    audience: Optional[str] = None
    attribution_method: Optional[Literal["llm", "rules"]] = None   # только для /process


class AttributeResponse(BaseModel):
    run_id: int
    method: str                     # llm | rules
    chunks: Optional[int] = None    # llm: сколько вызовов модели
    fallback_chunks: list[int] = []  # llm: номера чанков, размеченных правилами из-за сбоя модели
    dialogue: list[UtteranceOut]
    without_utterances: list[str]   # участники, которым не досталось ни одной реплики


class SummarizeStageResponse(BaseModel):
    run_id: int
    audience: str
    statuses: list[StatusOut]


class ReportResponse(BaseModel):
    run_id: int
    audience: str
    text: str


class RunOut(BaseModel):
    id: int
    meeting_date: str
    recorded_at: Optional[str] = None
    source: str
    status: str                     # created|transcribed|attributed|summarized|reported|error
    error: Optional[str] = None
    created_at: str
    updated_at: str


class RunDetailResponse(BaseModel):
    run: RunOut
    raw_transcript: list[SegmentOut]
    dialogue: list[UtteranceOut]
    statuses: dict[str, list[StatusOut]]   # по аудиториям
    reports: dict[str, str]                # по аудиториям


class DeleteResponse(BaseModel):
    dry_run: bool
    run_ids: list[int]              # пусто при delete all
    deleted: dict[str, int]         # строк по таблицам


class AudiencesResponse(BaseModel):
    default: str
    audiences: list[str]


class AssistantHealthResponse(BaseModel):
    status: str
    history_backend: str
    transcriber_backend: str
    stages_available: bool
    audiences: list[str]
    participants: int
