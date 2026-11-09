from __future__ import annotations

from pathlib import Path

from observability.metrics.decorators import track_external_service

from ..domain import Segment, Transcript
from ..run_context import report_progress


class FasterWhisperTranscriber:
    """CPU-вариант (pip install -e .[faster])."""

    def __init__(self, model: str = "large-v3", language: str = "ru", compute_type: str = "int8",
                 beam_size: int = 5, cpu_threads: int = 0):
        self._model = model
        self._language = language
        self._compute_type = compute_type
        self._beam_size = beam_size
        self._cpu_threads = cpu_threads  # 0 = умолчание CTranslate2 (замер: 4 потока не медленнее 8)
        self._whisper = None  # грузится при первой транскрибации и живёт весь процесс
        self._state = "unknown"   # unknown | ready | missing | downloading | error: ...

    # ── файлы модели (проверка и докачка при старте сервиса, без загрузки в память) ──
    def status(self) -> str:
        if self._state in ("unknown", "missing"):
            self._state = "ready" if self._cached() else "missing"
        return self._state

    def _cached(self) -> bool:
        from faster_whisper.utils import download_model
        try:
            download_model(self._model, local_files_only=True)
            return True
        except Exception:
            return False

    def preload(self) -> None:
        if self.status() == "ready":
            return
        from faster_whisper.utils import download_model
        self._state = "downloading"
        try:
            download_model(self._model)
            self._state = "ready"
        except Exception as exc:
            self._state = f"error: {type(exc).__name__}: {exc}"
            raise

    @track_external_service("whisper", "transcribe")
    def transcribe(self, audio: Path, prompt_hint: str = "") -> Transcript:
        from faster_whisper import WhisperModel

        if self._whisper is None:
            self._whisper = WhisperModel(self._model, device="cpu", compute_type=self._compute_type,
                                       cpu_threads=self._cpu_threads)
        raw, info = self._whisper.transcribe(
            str(audio),
            language=self._language,
            initial_prompt=prompt_hint or None,
            vad_filter=True,
            condition_on_previous_text=False,
            beam_size=self._beam_size,
        )
        segments = []
        for s in raw:  # ленивый генератор: после каждого сегмента известно, докуда дошли
            report_progress(s.end, info.duration)
            if s.text.strip():
                segments.append(Segment(s.start, s.end, s.text.strip()))
        return Transcript(segments, source=str(audio))
