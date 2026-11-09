from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AssistantSettings(BaseSettings):
    """Env-переменные с префиксом ASSISTANT_. Относительные пути считаются от корня проекта."""

    team_file: Path = Field(default=Path("config/team.yaml"), description="Состав дейли и проекты")
    prompts_dir: Path = Field(default=Path("prompts"), description="Папки аудиторий")
    audio_dir: Path = Field(default=Path("data/audio"), description="Записи встреч (WAV и др.)")
    history_dir: Path = Field(default=Path("data/history"), description="История для history_backend=file")
    output_dir: Path = Field(default=Path("data/output"), description="Результаты CLI")
    default_audience: str = Field(default="management", description="Папка из prompts/")

    history_backend: Literal["postgres", "file"] = Field(
        default="postgres",
        description="postgres — прод (история, журнал запусков и вызовов LLM); file — локальная отладка",
    )

    attribution_method: Literal["llm", "rules"] = Field(
        default="llm",
        description="Как размечать говорящих: llm — модель чанками (при сбое чанк размечается правилами); "
                    "rules — только регулярные выражения по именным меткам",
    )
    attribution_chunk_chars: int = Field(
        default=6000, ge=1,
        description="Размер чанка разметки в символах. Транскрипт короче — один вызов модели; "
                    "ориентир ~2,5 символа на токен, должно помещаться в OLLAMA max_context_length с запасом",
    )
    attribution_context_units: int = Field(default=12, ge=0, description="Сколько предыдущих и следующих фраз передавать для контекста")
    attribution_unit_max_words: int = Field(default=30, ge=1, description="Максимум слов во фразе без пунктуации и имён")

    images_dir: Path = Field(default=Path("data/images"), description="Загруженные фото для разбора картинок")
    image_max_side: int = Field(
        default=1568, ge=256,
        description="Длинная сторона фото в пикселях перед отправкой модели: больше -- дороже и медленнее, не точнее")
    image_max_per_request: int = Field(default=8, ge=1, description="Сколько картинок в одном запросе к модели")
    image_max_upload_mb: int = Field(default=25, ge=1, description="Максимальный размер одного загружаемого фото")
    kinds_file: Path = Field(default=Path("config/meeting_kinds.yaml"), description="Типы встреч")
    analysis_chunk_chars: int = Field(
        default=12000,
        description="Разбор встреч и сводки: транскрипт длиннее режется на части по этому размеру (символы), "
                    "иначе Ollama молча отбросит не поместившееся в контекст",
    )
    startup_check: Literal["off", "warn", "strict"] = Field(
        default="warn",
        description="Проверка моделей и зависимостей при старте: warn -- в лог, strict -- не стартовать при ошибке",
    )
    preload_whisper: bool = Field(
        default=True,
        description="При старте в фоне докачать модель Whisper (иначе она качается при первой транскрибации)",
    )

    transcriber_backend: Literal["mlx_whisper", "faster_whisper"] = Field(default="mlx_whisper")
    whisper_model: str | None = Field(
        default=None,
        description="По умолчанию: mlx-community/whisper-large-v3-turbo (mlx) / large-v3 (faster)",
    )
    whisper_language: str = Field(default="ru")
    whisper_beam_size: int = Field(default=5, ge=1, description="faster_whisper: 5 = точнее (умолчание), 1 = жадный поиск; на замере выигрыш небольшой")
    whisper_cpu_threads: int = Field(default=0, ge=0, description="faster_whisper: 0 = умолчание библиотеки; больше потоков не всегда быстрее")

    @field_validator("team_file", "kinds_file", "images_dir", "prompts_dir", "audio_dir", "history_dir", "output_dir")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else PROJECT_ROOT / v

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",
        case_sensitive=False,
        env_file=".env",
        extra="ignore",
    )


@lru_cache()
def get_assistant_settings() -> AssistantSettings:
    return AssistantSettings()
