from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    """Запрос к /chat эндпоинту."""

    prompt: str        # сообщение пользователя (user role)
    instruction: str   # системный промпт (system role)
    model_name: Optional[str] = None   # по умолчанию — из настроек
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ChatResponse(BaseModel):
    """Ответ от LLM-движка."""

    response: str
    error: Optional[str] = None


class ModelsResponse(BaseModel):
    """Локальные модели Ollama."""

    default_model: str
    models: list[str]


class OllamaHealthResponse(BaseModel):
    status: str
    url: str
    model: str
    model_available: bool
