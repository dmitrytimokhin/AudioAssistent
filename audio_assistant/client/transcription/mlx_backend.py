from __future__ import annotations

import os
import shutil
from pathlib import Path

from observability.metrics.decorators import track_external_service

from ..domain import Segment, Transcript


_FFMPEG_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")   # Homebrew на Apple Silicon / Intel


def ensure_ffmpeg() -> None:
    """mlx-whisper вызывает ffmpeg как внешнюю программу. Jupyter/IDE, запущенные не из терминала,
    часто не имеют каталога Homebrew в PATH -- добавляем его, если там есть ffmpeg."""
    if shutil.which("ffmpeg"):
        return
    for directory in _FFMPEG_DIRS:
        if (Path(directory) / "ffmpeg").exists():
            os.environ["PATH"] = f"{os.environ.get('PATH', '')}{os.pathsep}{directory}"
            return
    raise RuntimeError("ffmpeg не найден: brew install ffmpeg")


class MlxWhisperTranscriber:
    """Whisper на Apple Silicon через mlx-whisper (pip install -e .[mlx])."""

    def __init__(self, model: str = "mlx-community/whisper-large-v3-turbo", language: str = "ru"):
        self._model = model
        self._language = language
        self._state = "unknown"   # unknown | ready | missing | downloading | error: ...

    # ── файлы модели (проверка и докачка при старте сервиса, без загрузки в память) ──
    def status(self) -> str:
        if self._state in ("unknown", "missing"):
            self._state = "ready" if self._cached() else "missing"
        return self._state

    def _cached(self) -> bool:
        if Path(self._model).exists():
            return True
        from huggingface_hub import snapshot_download
        try:
            snapshot_download(self._model, local_files_only=True)
            return True
        except Exception:
            return False

    def preload(self) -> None:
        """Скачивает модель, если её нет в кэше HF. Память не занимает: в неё модель грузится при первой записи."""
        if self.status() == "ready":
            return
        from huggingface_hub import snapshot_download
        self._state = "downloading"
        try:
            snapshot_download(self._model)
            self._state = "ready"
        except Exception as exc:
            self._state = f"error: {type(exc).__name__}: {exc}"
            raise

    @track_external_service("whisper", "transcribe")
    def transcribe(self, audio: Path, prompt_hint: str = "") -> Transcript:
        import mlx_whisper  # ленивый импорт: зависимость опциональна

        ensure_ffmpeg()

        result = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=self._model,
            language=self._language,
            initial_prompt=prompt_hint or None,
            condition_on_previous_text=False,  # меньше «зацикливаний» на длинных записях
        )
        segments = [
            Segment(float(s["start"]), float(s["end"]), s["text"].strip())
            for s in result["segments"]
            if s["text"].strip()
        ]
        return Transcript(segments, source=str(audio))
