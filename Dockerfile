FROM python:3.12-slim

# ffmpeg нужен Whisper для чтения WAV/MP3/M4A
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости отдельным слоем: пересборка при правке кода не переустанавливает пакеты.
# В контейнере Whisper работает на CPU (faster-whisper): mlx-whisper требует Apple Metal.
COPY pyproject.toml .
RUN python - <<'PY' > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt
import tomllib
p = tomllib.load(open("pyproject.toml", "rb"))["project"]
print("\n".join(p["dependencies"] + p["optional-dependencies"]["faster"]))
PY

COPY app app
COPY audio_assistant audio_assistant
COPY observability observability
COPY ollama_client ollama_client
COPY postgres_client postgres_client
# config/ и prompts/ в compose монтируются поверх, чтобы править без пересборки
COPY config config
COPY prompts prompts

ENV PYTHONUNBUFFERED=1 \
    ASSISTANT_TRANSCRIBER_BACKEND=faster_whisper \
    ASSISTANT_AUDIO_DIR=/app/data/audio
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/healthz/liveness', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
