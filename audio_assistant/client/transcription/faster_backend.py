from __future__ import annotations

from pathlib import Path

from observability.metrics.decorators import track_external_service

from ..domain import Segment, Transcript


class FasterWhisperTranscriber:
    """CPU-вариант (pip install -e .[faster])."""

    def __init__(self, model: str = "large-v3", language: str = "ru", compute_type: str = "int8"):
        self._model = model
        self._language = language
        self._compute_type = compute_type
        self._whisper = None  # грузится при первой транскрибации и живёт весь процесс

    @track_external_service("whisper", "transcribe")
    def transcribe(self, audio: Path, prompt_hint: str = "") -> Transcript:
        from faster_whisper import WhisperModel

        if self._whisper is None:
            self._whisper = WhisperModel(self._model, device="cpu", compute_type=self._compute_type)
        raw, _ = self._whisper.transcribe(
            str(audio),
            language=self._language,
            initial_prompt=prompt_hint or None,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        segments = [Segment(s.start, s.end, s.text.strip()) for s in raw if s.text.strip()]
        return Transcript(segments, source=str(audio))
