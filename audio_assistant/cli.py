"""Локальный запуск без FastAPI и PostgreSQL: Ollama + файловая история."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from observability import init_observability

from audio_assistant.client.bootstrap import build_history, build_pipeline
from audio_assistant.client.llm_adapter import OllamaLLMAdapter
from audio_assistant.client.pipeline import Pipeline, load_transcript
from audio_assistant.client.recordings import parse_recording_datetime
from audio_assistant.endpoint.assistant_settings import AssistantSettings, get_assistant_settings
from ollama_client.client.ollama_client import OllamaClient
from ollama_client.endpoint.ollama_settings import get_ollama_settings


def build_local_pipeline(settings: AssistantSettings | None = None) -> tuple[Pipeline, AssistantSettings]:
    """Общая сборка для CLI и ноутбука."""
    settings = settings or get_assistant_settings()
    ollama_settings = get_ollama_settings()
    client = OllamaClient(ollama_settings)
    llm = OllamaLLMAdapter(client, ollama_settings.temperature, ollama_settings.max_tokens)
    return build_pipeline(settings, llm, build_history(settings)), settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="audio-assistant")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe", help="аудио -> transcript.json")
    t.add_argument("audio", type=Path)

    for name, help_ in (("run", "аудио -> статус"), ("summarize", "транскрипт (.json/.txt) -> статус")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("input", type=Path)
        p.add_argument("--audience", help="папка из prompts/")
        p.add_argument("--date", type=date.fromisoformat,
                       help="по умолчанию: дата из имени файла (YYYYMMDDHHMMSS), иначе сегодня")
        p.add_argument("--no-save", action="store_true", help="не писать в историю")

    args = ap.parse_args(argv)
    init_observability()
    pipeline, settings = build_local_pipeline()
    out = settings.output_dir
    out.mkdir(parents=True, exist_ok=True)

    if args.cmd == "transcribe":
        tr = pipeline.transcribe(args.input)
        target = out / f"{args.input.stem}.transcript.json"
        target.write_text(json.dumps(tr.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Транскрипт: {target}")
        return 0

    audience = args.audience or settings.default_audience
    save = not args.no_save
    recorded_at = parse_recording_datetime(args.input.name)
    day = args.date or (recorded_at.date() if recorded_at else date.today())
    if args.cmd == "run":
        result = pipeline.run_audio(args.input, audience, day, save, recorded_at)
    else:
        result = pipeline.run_transcript(load_transcript(args.input), audience, day, save)

    stem = f"{day.isoformat()}_{audience}"
    (out / f"{stem}.transcript.json").write_text(
        json.dumps(result.transcript.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"{stem}.txt").write_text(result.text, encoding="utf-8")
    print(result.text)
    return 0
