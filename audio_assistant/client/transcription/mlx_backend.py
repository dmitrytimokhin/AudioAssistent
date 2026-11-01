from __future__ import annotations

from pathlib import Path

from observability.metrics.decorators import track_external_service

from ..domain import Segment, Transcript


class MlxWhisperTranscriber:
    """Whisper на Apple Silicon через mlx-whisper (pip install -e .[mlx])."""

    def __init__(self, model: str = "mlx-community/whisper-large-v3-turbo", language: str = "ru"):
        self._model = model
        self._language = language

    @track_external_service("whisper", "transcribe")
    def transcribe(self, audio: Path, prompt_hint: str = "") -> Transcript:
        import mlx_whisper  # ленивый импорт: зависимость опциональна

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
