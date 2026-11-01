#!/usr/bin/env python3
"""Запись из data/audio -> готовые txt в data/output через ручки запущенного сервиса.

    python scripts/process_audio.py                          # последняя запись, аудитория по умолчанию
    python scripts/process_audio.py 20261101180957.WAV       # конкретный файл
    python scripts/process_audio.py --audience management --audience team
    python scripts/process_audio.py --audience all           # все аудитории из prompts/
    python scripts/process_audio.py --all-new                # все записи, для которых ещё нет результата
    python scripts/process_audio.py --replace                # сначала удалить прошлые запуски этого файла
    python scripts/process_audio.py --attribution rules      # разметка говорящих правилами вместо модели

Для каждой записи пишет в --out (по умолчанию data/output):
    <имя файла>.dialogue.txt          диалог по говорящим
    <имя файла>_<аудитория>.txt       итоговый статус

Только стандартная библиотека: работает без venv, лишь бы был Python 3.
Whisper выполняется один раз на запись, остальные аудитории строятся по сохранённому транскрипту.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 3600  # Whisper на CPU по часовой записи работает долго


def call(base: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        sys.exit(f"Ошибка {e.code} на {method} {path}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Сервис недоступен ({base}): {e.reason}\n"
                 "Запущен ли он? См. docs/RUNNING.md (docker compose up -d).")


def dialogue_text(dialogue: list[dict]) -> str:
    return "\n".join(f"[{u['speaker'] or 'ведущий'}] {u['text']}" for u in dialogue) + "\n"


def process(base: str, file_name: str, audiences: list[str], out: Path, replace: bool,
            attribution: str | None) -> None:
    stem = Path(file_name).stem
    started = time.perf_counter()
    if replace:
        r = call(base, "DELETE", "/assistant/runs?" + urllib.parse.urlencode({"file_name": file_name}))
        if r["run_ids"]:
            print(f"  удалены прошлые запуски: {r['run_ids']}")

    print(f"  [1/3] транскрибация {file_name} ...", flush=True)
    t = call(base, "POST", "/assistant/transcribe", {"file_name": file_name})
    run_id = t["run_id"]

    out.mkdir(parents=True, exist_ok=True)
    for i, audience in enumerate(audiences):
        print(f"  [{2 if i == 0 else '…'}/3] говорящие и статусы для «{audience}» (run_id={run_id}) ...", flush=True)
        res = call(base, "POST", f"/assistant/runs/{run_id}/process",
                   {"audience": audience, "attribution_method": attribution})
        target = out / f"{stem}_{audience}.txt"
        target.write_text(res["text"] + "\n", encoding="utf-8")
        print(f"        -> {target}")

    detail = call(base, "GET", f"/assistant/runs/{run_id}")
    dialogue = out / f"{stem}.dialogue.txt"
    dialogue.write_text(dialogue_text(detail["dialogue"]), encoding="utf-8")
    print(f"  [3/3] диалог -> {dialogue}")
    print(f"  готово за {time.perf_counter() - started:.0f} с, run_id={run_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="имя файла в data/audio (по умолчанию — самая свежая запись)")
    ap.add_argument("--audience", action="append", help="повторяемый; all — все аудитории")
    ap.add_argument("--all-new", action="store_true", help="обработать все записи без готового результата")
    ap.add_argument("--replace", action="store_true", help="удалить прошлые запуски этого файла перед обработкой")
    ap.add_argument("--attribution", choices=("llm", "rules"),
                    help="разметка говорящих: llm (модель, чанками) или rules; по умолчанию из настроек сервиса")
    ap.add_argument("--url", default=os.getenv("ASSISTANT_URL", "http://localhost:8000"))
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "output")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    info = call(base, "GET", "/assistant/audiences")
    wanted = args.audience or [info["default"]]
    audiences = info["audiences"] if "all" in wanted else wanted

    files = call(base, "GET", "/assistant/audio")  # свежие первыми
    if not files:
        sys.exit("В data/audio нет записей (.wav .mp3 .m4a ...)")
    if args.all_new:
        todo = [f["file_name"] for f in files if not (args.out / f"{Path(f['file_name']).stem}.dialogue.txt").exists()]
        if not todo:
            print("Новых записей нет.")
            return 0
    else:
        todo = [args.file or files[0]["file_name"]]

    for name in todo:
        print(f"{name}:")
        process(base, name, audiences, args.out, args.replace, args.attribution)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
