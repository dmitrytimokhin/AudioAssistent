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
        default=6000,
        description="Размер чанка разметки в символах. Транскрипт короче — один вызов модели; "
                    "ориентир ~2,5 символа на токен, должно помещаться в OLLAMA max_context_length с запасом",
    )
    attribution_context_units: int = Field(default=3, description="Сколько размеченных фраз передавать из прошлого чанка")
    attribution_unit_max_words: int = Field(default=30, description="Максимум слов во фразе без пунктуации и имён")

    transcriber_backend: Literal["mlx_whisper", "faster_whisper"] = Field(default="mlx_whisper")
    whisper_model: str | None = Field(
        default=None,
        description="По умолчанию: mlx-community/whisper-large-v3-turbo (mlx) / large-v3 (faster)",
    )
    whisper_language: str = Field(default="ru")

    @field_validator("team_file", "prompts_dir", "audio_dir", "history_dir", "output_dir")
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
