import re
from typing import Optional
from requests.exceptions import RequestException, Timeout, ConnectionError
import requests

from ollama_client.endpoint.ollama_settings import OllamaSettings
from ollama_client.client.ollama_connection import ollama_connection, ollama_list_models
from observability.logging.setup import get_logger
from observability.metrics.decorators import track_external_service

logger = get_logger(__name__)

# Часть моделей (qwen3*, deepseek-r1) может вернуть рассуждения прямо в content.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OllamaClient:
    def __init__(self, settings: OllamaSettings):
        self.settings = settings
        self.is_connected = False

    def connect(self) -> bool:
        """Проверяет подключение к Ollama и устанавливает флаг готовности."""
        self.is_connected = ollama_connection(self.settings)
        return self.is_connected

    def list_models(self) -> list[str]:
        """Имена локальных моделей. Пустой список, если Ollama недоступна."""
        try:
            return [m["name"] for m in ollama_list_models(self.settings)]
        except (RequestException, KeyError, ValueError) as e:
            logger.warning("Failed to list Ollama models", error=str(e))
            return []

    def capabilities(self, model_name: Optional[str] = None) -> list[str]:
        """Возможности модели по /api/show: completion, vision, thinking, tools. Пусто, если узнать не удалось."""
        try:
            response = requests.post(f"{self.settings.ollama_url}/api/show",
                                     json={"model": model_name or self.settings.model_name}, timeout=10)
            response.raise_for_status()
            return list(response.json().get("capabilities") or [])
        except (RequestException, ValueError) as e:
            logger.warning("Failed to read Ollama model capabilities", error=str(e))
            return []

    def has_model(self, model_name: Optional[str] = None) -> bool:
        return (model_name or self.settings.model_name) in self.list_models()

    def query(
        self,
        instruction: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        model_name: Optional[str] = None,
        json_schema: Optional[dict] = None,
        images: Optional[list[str]] = None,
    ) -> str:
        """
        instruction — системный промпт (system role).
        prompt      — сообщение пользователя (user role).
        images      — картинки в base64 (нужна модель с возможностью vision).
        Сигнатура аналогична YaLLMClient.get_generation().
        """
        user = {"role": "user", "content": prompt, **({"images": images} if images else {})}
        messages = [{"role": "system", "content": instruction}, user]
        return self.chat(messages, temperature, max_tokens, model_name, json_schema)

    @track_external_service("ollama", "chat")
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        model_name: Optional[str] = None,
        json_schema: Optional[dict] = None,
    ) -> str:
        """
        json_schema — структурированный вывод Ollama: ответ гарантированно валидный JSON
        по схеме (в том числе enum допустимых значений).

        Произвольная история сообщений [{"role": system|user|assistant, "content": ...}].
        Через неё передаётся контекст из PostgreSQL (прошлые статусы и т.п.).
        """
        if not self.is_connected:
            raise RuntimeError("Ollama client is not connected")

        effective_model = model_name or self.settings.model_name
        logger.info("Ollama request", model=effective_model, messages=len(messages))
        logger.debug("Generation params", temperature=temperature, max_tokens=max_tokens)

        payload = {
            "model": effective_model,
            "messages": messages,
            "stream": False,
            "think": self.settings.think,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.settings.max_context_length,
            },
        }

        if json_schema is not None:
            payload["format"] = json_schema

        try:
            url = f"{self.settings.ollama_url}/api/chat"
            response = requests.post(url=url, json=payload, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()

            content = response.json().get("message", {}).get("content", "")
            content = _THINK_BLOCK.sub("", content).strip()
            if not content:
                raise ValueError("Empty response from Ollama")

            logger.info("Ollama response received", content_length=len(content))
            return content

        except ConnectionError as e:
            logger.error("Failed to connect to Ollama", error=str(e))
            raise RuntimeError("Failed to connect to Ollama (ConnectionError)") from e

        except Timeout as e:
            logger.error("Ollama request timed out", timeout=self.settings.request_timeout_seconds, error=str(e))
            raise RuntimeError(f"Ollama request timed out ({self.settings.request_timeout_seconds}s)") from e

        except RequestException as e:
            # `if e.response` здесь неверно: bool(Response) == response.ok, то есть
            # для 4xx/5xx это False и статус всегда терялся бы в "unknown".
            status = e.response.status_code if e.response is not None else "unknown"
            logger.error("Ollama HTTP request failed", status=status, error=str(e))
            raise RuntimeError(f"Ollama HTTP request failed: status {status}") from e

        except (ValueError, KeyError, TypeError) as e:
            logger.error("Invalid response format from Ollama", error=str(e))
            raise RuntimeError("Invalid response format from Ollama") from e

        except Exception as e:
            logger.error("Unexpected error from Ollama", error=str(e))
            raise RuntimeError("Unexpected error from Ollama") from e
