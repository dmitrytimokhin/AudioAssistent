from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class OllamaSettings(BaseSettings):
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="URL хостинга модели Ollama",
        alias="ollama_url"
    )

    model_name: str = Field(
        default="qwen3.5:4b",
        description="Наименование модели",
        alias="ollama_model_name"
    )

    request_timeout_seconds: int = Field(
        default=300,
        description="Таймаут на запрос к хостингу (локальная модель на длинном контексте отвечает долго)"
    )

    max_context_length: int = Field(
        default=8192,
        description="Максимальное число токенов в контексте модели (передаётся как num_ctx)",
        alias="max_context_length"
    )

    reserved_tokens_for_response: int = Field(
        default=512,
        description="Зарезервировано под ответ (используется в truncate)",
        alias="reserved_tokens_for_response"
    )

    temperature: float = Field(
        default=0.1,
        description="Температура модели",
        alias="ollama_temperature",
    )

    max_tokens: int = Field(
        default=512,
        description="Максимальное количество токенов на ответ",
        alias="ollama_max_tokens",
    )

    think: bool = Field(
        default=False,
        description="Режим «размышлений» (qwen3.5/qwen3). Для коротких статусов выключен: иначе токены уходят в рассуждения",
        alias="ollama_think",
    )

    model_config = SettingsConfigDict(
        frozen=True,
        case_sensitive=False,
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore"
    )


@lru_cache()
def get_ollama_settings() -> OllamaSettings:
    """
    Получить настройки приложения

    Используют @lru_cache для кэширования - настройки загружаются один раз
    и переиспользуются при последующих вызовах

    :return: экземпляр OllamaSettings
    """
    return OllamaSettings()
