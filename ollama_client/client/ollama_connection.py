import requests
from requests.exceptions import ConnectionError, Timeout
from ollama_client.endpoint.ollama_settings import OllamaSettings

def ollama_connection(settings: OllamaSettings) -> bool:
    """
    Проверяет доступность Ollama сервера по endpoint.
    """
    try:
        response = requests.get(f"{settings.ollama_url}/api/tags", timeout=5)
        return response.status_code == 200
    except (ConnectionError, Timeout):
        return False


def ollama_list_models(settings: OllamaSettings) -> list[dict]:
    """Список локальных моделей (GET /api/tags): name, size, details."""
    response = requests.get(f"{settings.ollama_url}/api/tags", timeout=5)
    response.raise_for_status()
    return response.json().get("models", [])
