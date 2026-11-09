#!/usr/bin/env bash
# Сервис на хосте (Whisper через Metal, ~10 раз быстрее CPU в Docker), Postgres в Docker.
# Порт и параметры БД берутся из .env (POSTGRES_HOST=localhost, POSTGRES_PORT=порт хоста).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8005}"
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"   # ffmpeg для mlx-whisper

docker compose up -d postgres
docker compose stop app 2>/dev/null || true            # контейнер app занимал бы тот же порт

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
