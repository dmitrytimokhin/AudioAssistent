"""Проверка готовности: все модели и зависимости, без которых пайплайн не заработает.

Каждая проверка возвращает статус: ok | loading (идёт докачка модели) | warn (работать можно) | error.
Вызывается при старте сервиса (лог, при ASSISTANT_STARTUP_CHECK=strict -- отказ стартовать)
и по запросу GET /assistant/models.
"""
from __future__ import annotations

import os
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from observability.logging.setup import get_logger

logger = get_logger(__name__)


@dataclass
class CheckResult:
    name: str
    status: str      # ok | loading | warn | error
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


class ReadinessChecker:
    """Собирает проверки из готовых зависимостей (ничего не создаёт сам: DIP)."""

    def __init__(self, *, ollama=None, transcriber=None, transcriber_backend: str = "",
                 audio_dir: Path | None = None, output_dir: Path | None = None,
                 prompts=None, kinds=None, postgres=None):
        self._ollama, self._transcriber, self._backend = ollama, transcriber, transcriber_backend
        self._audio_dir, self._output_dir = audio_dir, output_dir
        self._prompts, self._kinds, self._postgres = prompts, kinds, postgres

    def run(self) -> list[CheckResult]:
        checks: list[Callable[[], CheckResult]] = [
            self._check_ollama, self._check_vision, self._check_whisper, self._check_ffmpeg,
            self._check_postgres, self._check_dirs, self._check_prompts]
        results = []
        for check in checks:
            try:
                results.append(check())
            except Exception as exc:   # проверка не должна ронять сервис
                results.append(CheckResult(check.__name__.removeprefix("_check_"), "error",
                                           f"{type(exc).__name__}: {exc}"))
        return results

    @staticmethod
    def ready(results: list[CheckResult]) -> bool:
        return all(r.status in ("ok", "warn") for r in results)

    # ── отдельные проверки ───────────────────────────────────────────────
    def _check_ollama(self) -> CheckResult:
        client = self._ollama
        if client is None:
            return CheckResult("ollama", "error", "клиент Ollama не подключён")
        model = client.settings.model_name
        if not client.connect():
            return CheckResult("ollama", "error", f"Ollama недоступна ({client.settings.ollama_url})")
        if not client.has_model(model):
            return CheckResult("ollama", "error", f"модели {model} нет в Ollama: ollama pull {model}")
        return CheckResult("ollama", "ok", f"{client.settings.ollama_url}, модель {model}")

    def _check_vision(self) -> CheckResult:
        """Разбор картинок работает, только если модель умеет vision (иначе Ollama вернёт ошибку или проигнорирует фото)."""
        client = self._ollama
        capabilities = getattr(client, "capabilities", None)
        if capabilities is None:
            return CheckResult("vision", "warn", "возможности модели не определены")
        found = capabilities(client.settings.model_name)
        if "vision" in found:
            return CheckResult("vision", "ok", f"модель {client.settings.model_name} принимает изображения")
        return CheckResult("vision", "warn", "модель не сообщает vision: разбор картинок может не работать"
                                             if found else "возможности модели не прочитаны (Ollama недоступна?)")

    def _check_whisper(self) -> CheckResult:
        status_of = getattr(self._transcriber, "status", None)
        model = getattr(self._transcriber, "_model", "?")
        if status_of is None:
            return CheckResult("whisper", "warn", f"{self._backend}: проверка файлов модели не поддерживается")
        state = status_of()
        if state == "ready":
            return CheckResult("whisper", "ok", f"{self._backend}, модель {model} в кэше")
        if state == "downloading":
            return CheckResult("whisper", "loading", f"{self._backend}: докачивается модель {model}")
        if state == "missing":
            return CheckResult("whisper", "warn", f"модели {model} нет в кэше: скачается при первой транскрибации "
                                                  "(~1,5 ГБ) или при старте, если ASSISTANT_PRELOAD_WHISPER=true")
        return CheckResult("whisper", "error", state)

    def _check_ffmpeg(self) -> CheckResult:
        if self._backend != "mlx_whisper":
            return CheckResult("ffmpeg", "ok", "не нужен для faster_whisper (декодер встроен)")
        from .transcription.mlx_backend import ensure_ffmpeg
        ensure_ffmpeg()
        return CheckResult("ffmpeg", "ok", shutil.which("ffmpeg") or "найден")

    def _check_postgres(self) -> CheckResult:
        if self._postgres is None:
            return CheckResult("postgres", "warn", "не используется (history_backend=file)")
        ok = self._postgres.ping()
        return CheckResult("postgres", "ok" if ok else "error", "доступен" if ok else "нет соединения")

    def _check_dirs(self) -> CheckResult:
        problems = []
        for label, path in (("audio_dir", self._audio_dir), ("output_dir", self._output_dir)):
            if path is None:
                continue
            Path(path).mkdir(parents=True, exist_ok=True)
            if not os.access(path, os.W_OK):
                problems.append(f"{label} {path} недоступна для записи")
        return CheckResult("directories", "error" if problems else "ok", "; ".join(problems) or "доступны для записи")

    def _check_prompts(self) -> CheckResult:
        if self._prompts is None or self._kinds is None:
            return CheckResult("prompts", "warn", "каталог промптов не подключён")
        have = set(self._prompts.meeting_prompts())
        missing = [k for k in self._kinds.names if self._kinds.get(k).prompt not in have]
        if missing:
            return CheckResult("prompts", "warn",
                               f"нет prompts/meetings/<тип>/ для типов {missing}: разбор этих встреч не заработает")
        return CheckResult("prompts", "ok", f"аудитории {self._prompts.available()}, "
                                            f"разбор встреч {sorted(have)}, сводки {self._prompts.digests()}")


def preload_in_background(transcriber) -> threading.Thread | None:
    """Докачка модели Whisper в фоне: старт сервиса не ждёт, статус виден в /assistant/models."""
    preload = getattr(transcriber, "preload", None)
    if preload is None:
        return None

    def work() -> None:
        try:
            preload()
        except Exception as exc:
            logger.error("Whisper model preload failed", error=str(exc))

    thread = threading.Thread(target=work, name="whisper-preload", daemon=True)
    thread.start()
    return thread


def log_results(results: list[CheckResult]) -> None:
    for r in results:
        log = {"ok": logger.info, "loading": logger.info, "warn": logger.warning}.get(r.status, logger.error)
        log(f"[CHECK] {r.name}: {r.status}", detail=r.detail)
