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


class MeetingMeta(BaseModel):
    """Что это за встреча: тип из config/meeting_kinds.yaml и произвольные теги."""

    kind: Optional[str] = None
    tags: list[str] = []


class UploadResponse(BaseModel):
    file_name: str
    size_bytes: int
    recorded_at: Optional[datetime] = None
    run_id: Optional[int] = None                 # если запрошена транскрибация
    meeting_date: Optional[date] = None
    segments: Optional[list[SegmentOut]] = None


class LLMPromptRequest(BaseModel):
    """Свой промпт к модели. Транскрипт подставляется в `{transcript}` (нет метки -> дописывается в конец).

    Текст берётся из запуска (run_id + source) или передаётся как transcript_text;
    без обоих промпт уходит модели как есть.
    """

    prompt: str
    system: Optional[str] = None
    run_id: Optional[int] = None
    source: Literal["raw", "dialogue"] = "raw"   # raw — как расслышал Whisper; dialogue — с метками говорящих
    transcript_text: Optional[str] = None
    json_schema: Optional[dict] = None           # структурированный ответ по JSON-схеме


class LLMPromptResponse(BaseModel):
    run_id: Optional[int] = None
    source: Optional[str] = None
    model: str
    duration_ms: float
    text: str


# ── сквозные запуски ────────────────────────────────────────────────────────
class SummarizeRequest(BaseModel):
    """Расшифровка встречи текстом (строка = фраза) -> статус."""

    text: str
    audience: Optional[str] = None       # по умолчанию ASSISTANT_DEFAULT_AUDIENCE
    meeting_date: Optional[date] = None  # по умолчанию сегодня
    save: bool = True                    # False — не писать историю и журнал запуска
    kind: Optional[str] = None
    tags: list[str] = []


class ProcessFileRequest(BaseModel):
    """Файл из data/audio -> транскрипт -> говорящие -> статус."""

    file_name: str
    audience: Optional[str] = None
    meeting_date: Optional[date] = None  # по умолчанию дата из имени файла, иначе сегодня
    save: bool = True
    include_transcript: bool = False
    kind: Optional[str] = None
    tags: list[str] = []


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
    kind: Optional[str] = None     # тип встречи (config/meeting_kinds.yaml)
    tags: list[str] = []
    wait: bool = True    # False — сразу вернуть run_id, транскрибация идёт в фоне (статус и прогресс: GET /runs/{id})


class TranscribeResponse(BaseModel):
    run_id: int
    meeting_date: date
    recorded_at: Optional[datetime] = None
    status: str = "transcribed"       # transcribing — при wait=false, пока идёт фоновая работа
    already_running: bool = False     # файл уже транскрибировался: возвращён существующий запуск
    segments: list[SegmentOut] = []


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
    kind: Optional[str] = None
    tags: list[str] = []
    status: str                     # created|transcribing|transcribed|attributed|summarized|reported|error
    progress: Optional[float] = None  # 0..1, пока идёт транскрибация (faster_whisper)
    error: Optional[str] = None
    created_at: str
    updated_at: str


class RunDetailResponse(BaseModel):
    run: RunOut
    raw_transcript: list[SegmentOut]
    dialogue: list[UtteranceOut]
    statuses: dict[str, list[StatusOut]]   # по аудиториям
    reports: dict[str, str]                # по аудиториям
    analyses: dict[str, str] = {}          # разборы встречи: имя промпта -> текст


class TablesResponse(BaseModel):
    counts: dict[str, int]          # строк в каждой таблице


class TableRowsResponse(BaseModel):
    table: str
    total: int                      # строк по фильтру (в rows не больше limit)
    columns: list[str]
    rows: list[dict]


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


# ── тип встречи, разбор, сводки, модели ─────────────────────────────────────
class RunMetaRequest(BaseModel):
    """Меняются только переданные поля: {"kind": "daily"} не трогает теги; {"kind": null} снимает тип."""

    kind: Optional[str] = None
    tags: Optional[list[str]] = None


class KindOut(BaseModel):
    name: str
    title: str
    prompt: str
    prompt_exists: bool             # есть prompts/meetings/<prompt>/{system,user}.md


class PromptsResponse(BaseModel):
    kinds: list[KindOut]
    meeting_prompts: list[str]
    digests: list[str]
    image_prompts: list[str] = []
    audiences: list[str]


class AnalyzeRequest(BaseModel):
    prompt: Optional[str] = None    # папка в prompts/meetings/; по умолчанию промпт типа встречи
    source: Literal["dialogue", "raw"] = "dialogue"


class AnalyzeResponse(BaseModel):
    run_id: int
    prompt: str
    source: str
    model: str
    chunks: int                     # на сколько частей пришлось разрезать транскрипт
    text: str


class DigestRequest(BaseModel):
    """Сводка по встречам за период в таблицу. Период по умолчанию: последние 7 дней."""

    name: str                                   # папка prompts/digests/<name>
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    kinds: Optional[list[str]] = None           # по умолчанию из digest.yaml
    tags: list[str] = []                        # встреча должна иметь все указанные теги
    run_ids: Optional[list[int]] = None         # явный список встреч вместо периода
    source: Optional[Literal["dialogue", "raw"]] = None
    wait: bool = True                           # False -- сразу ответ, ход в GET /digests/{id}


class DigestOut(BaseModel):
    id: int
    name: str
    title: str
    date_from: str
    date_to: str
    kinds: list[str]
    tags: list[str]
    source: str
    status: str                                 # running | done | error
    error: Optional[str] = None
    done: int
    total: int
    rows_count: int
    file_name: Optional[str] = None             # относительно data/output
    created_at: str
    updated_at: str


class DigestDetail(DigestOut):
    headers: list[str]                          # порядок колонок: pd.DataFrame(rows, columns=headers)
    meetings: list[dict]                        # итог по каждой встрече
    rows: list[dict]


class DigestDeleteResponse(BaseModel):
    dry_run: bool
    digest_ids: list[int]
    files: list[str]


class ModelCheckOut(BaseModel):
    name: str
    status: str                                 # ok | loading | warn | error
    detail: str


class ModelsResponse(BaseModel):
    ready: bool
    checks: list[ModelCheckOut]


# ── независимый разбор картинок ─────────────────────────────────────────────
class ImageFileOut(BaseModel):
    file_name: str
    size_bytes: int
    modified_at: datetime


class ImageUploadResponse(BaseModel):
    saved: list[str]


class ImageAnalyzeRequest(BaseModel):
    """Фото из data/images + промпт. Ровно одно из: prompt_name (папка prompts/images/) или prompt (свой текст)."""

    files: list[str]
    prompt_name: Optional[str] = None
    prompt: Optional[str] = None
    system: Optional[str] = None              # только вместе со своим prompt
    tags: list[str] = []
    mode: Literal["together", "each"] = "together"   # together: одним запросом; each: отдельный разбор на каждое фото
    json_schema: Optional[dict] = None
    save: bool = True                         # False -- не писать в Postgres


class ImageResultOut(BaseModel):
    files: list[str]
    analysis_id: Optional[int] = None         # id в Postgres (None при save=false или ошибке)
    model: str = ""
    duration_ms: float = 0.0
    text: str = ""
    error: Optional[str] = None               # при mode=each: сбой одного фото не роняет остальные


class ImageAnalysisOut(BaseModel):
    id: int
    images: list[dict]
    prompt_name: Optional[str] = None
    system_prompt: str
    prompt: str
    model: str
    tags: list[str]
    text: str
    duration_ms: float
    created_at: str


class ImageAnalysisDeleteResponse(BaseModel):
    dry_run: bool
    analysis_ids: list[int]
