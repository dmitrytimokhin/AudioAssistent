"""Сводка по встречам за период в таблицу (Excel).

Схема «map»: для каждой встречи отдельный запрос к LLM (транскрипт + промпт сводки) возвращает строки таблицы
по JSON-схеме, код склеивает строки всех встреч и пишет xlsx. Неделя дейликов целиком не отправляется в
модель: для 4B это не поместилось бы в контекст, а ошибка одной встречи не ломает остальные.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from observability.logging.setup import get_logger

from .analysis import meeting_values
from .domain import Team, Transcript
from .meeting_kinds import MeetingKinds
from .meeting_text import fill, render_transcript, split_text
from .ports import LLMClient, RunStore
from .prompts import META_COLUMNS as META_HEADERS, DigestProfile, FilePromptRepository
from .run_context import llm_context
from .xlsx_export import write_digest_xlsx

logger = get_logger(__name__)

DEFAULT_PERIOD_DAYS = 7


@dataclass
class DigestJob:
    digest_id: int
    profile: DigestProfile
    runs: list[dict]
    source: str
    date_from: date
    date_to: date


def rows_schema(columns: tuple[str, ...]) -> dict:
    """Структурированный ответ: {"rows": [{колонка: текст, ...}]}."""
    item = {"type": "object", "properties": {c: {"type": "string"} for c in columns}, "required": list(columns)}
    return {"type": "object", "properties": {"rows": {"type": "array", "items": item}}, "required": ["rows"]}


def parse_rows(raw: str, columns: tuple[str, ...]) -> list[dict]:
    """Ответ модели -> строки только с нужными колонками. Пустые строки отбрасываются."""
    data = json.loads(raw)
    items = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("в ответе нет списка rows")
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {c: _cell(item.get(c)) for c in columns}
        if any(row.values()):
            rows.append(row)
    return rows


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


class DigestBuilder:
    def __init__(self, llm: LLMClient, prompts: FilePromptRepository, team: Team, kinds: MeetingKinds,
                 store: RunStore, digests, output_dir: Path, chunk_chars: int):
        self._llm, self._prompts, self._team, self._kinds = llm, prompts, team, kinds
        self._store, self._digests, self._output_dir, self._chunk_chars = store, digests, output_dir, chunk_chars

    # ── подготовка: выбор встреч, запись в БД (быстро, ошибки параметров -- сразу) ──
    def prepare(self, name: str, date_from: date | None = None, date_to: date | None = None,
                kinds: list[str] | None = None, tags: list[str] | None = None,
                source: str | None = None, run_ids: list[int] | None = None) -> DigestJob:
        profile = self._prompts.digest(name)
        for kind in kinds or ():
            self._kinds.validate(kind)
        date_to = date_to or date.today()
        date_from = date_from or date_to - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
        if date_from > date_to:
            raise ValueError(f"date_from ({date_from}) позже date_to ({date_to})")
        use_kinds = list(kinds) if kinds is not None else list(profile.kinds)
        tags = self._kinds.normalize_tags(tags)
        use_source = source or profile.source

        runs: list[dict] = []
        if run_ids:
            runs = self._store.list_runs(limit=None, ids=run_ids, oldest_first=True)
            if missing := sorted(set(run_ids) - {r["id"] for r in runs}):
                raise LookupError(f"Запуски не найдены: {missing}")
        else:
            runs = self._store.list_runs(limit=None, kinds=use_kinds or None, date_from=date_from,
                                         date_to=date_to, oldest_first=True)
        for tag in tags:
            runs = [r for r in runs if tag in r["tags"]]
        if not runs:
            raise LookupError(f"Нет встреч за {date_from} — {date_to}"
                              f"{f' типов {use_kinds}' if use_kinds else ''}{f' с тегами {tags}' if tags else ''}")
        digest_id = self._digests.create(name, profile.title, date_from, date_to, use_kinds, tags,
                                         use_source, total=len(runs), headers=profile.headers)
        return DigestJob(digest_id, profile, runs, use_source, date_from, date_to)

    # ── выполнение (долго: запрос к модели на каждую встречу) ────────────
    def execute(self, job: DigestJob) -> None:
        profile, rows_total = job.profile, []
        try:
            for run in job.runs:
                meeting, rows = self._process_run(job, run)
                self._digests.add_meeting(job.digest_id, meeting, rows)
                rows_total.extend(rows)
            self._digests.finish(job.digest_id, self._write_file(job, rows_total))
            logger.info("Digest built", digest_id=job.digest_id, name=profile.name,
                        meetings=len(job.runs), rows=len(rows_total))
        except Exception as exc:
            logger.error("Digest failed", digest_id=job.digest_id, error=str(exc))
            self._digests.fail(job.digest_id, f"{type(exc).__name__}: {exc}")
            raise

    def _write_file(self, job: DigestJob, rows: list[dict]) -> str:
        meetings = self._digests.get(job.digest_id)["meetings"]
        name = f"{job.digest_id}_{job.profile.name}_{job.date_from}_{job.date_to}.xlsx"
        path = write_digest_xlsx(self._output_dir / "digests" / name, job.profile.headers, rows, meetings)
        return str(path.relative_to(self._output_dir))

    def _process_run(self, job: DigestJob, run: dict) -> tuple[dict, list[dict]]:
        base = {"run_id": run["id"], "meeting_date": run["meeting_date"], "source": run["source"],
                "kind": run.get("kind")}
        try:
            transcript, note = self._load(run["id"], job.source)
        except LookupError as exc:
            return {**base, "status": "пропущена", "rows": 0, "note": str(exc)}, []
        columns, rows, error = job.profile.columns, [], None
        values = meeting_values(run, self._kinds, self._team)
        with llm_context(run["id"], f"digest:{job.profile.name}"):
            for part in split_text(render_transcript(transcript, job.source if not note else "raw"),
                                   self._chunk_chars):
                try:
                    answer = self._llm.generate(
                        job.profile.system, fill(job.profile.user_template, transcript=part, **values),
                        json_schema=rows_schema(columns))
                    rows.extend(parse_rows(answer, columns))
                except Exception as exc:   # одна часть/встреча не должна ронять сводку целиком
                    error = f"{type(exc).__name__}: {exc}"
                    logger.warning("Digest chunk failed", run_id=run["id"], error=error)
        meta = {"date": str(run["meeting_date"]), "file": run["source"], "kind": run.get("kind") or "",
                "run_id": run["id"], "tags": ", ".join(run.get("tags") or [])}
        full_rows = [{**{META_HEADERS[m]: meta[m] for m in job.profile.meta_columns}, **row} for row in rows]
        status = "ошибка" if error and not rows else ("частично" if error else "ok")
        comment = "; ".join(x for x in (note, error) if x)
        return {**base, "status": status, "rows": len(full_rows), "note": comment}, full_rows

    def _load(self, run_id: int, source: str) -> tuple[Transcript, str]:
        """dialogue без разметки говорящих -> raw с пометкой в комментарии (встреча не теряется)."""
        if source == "dialogue":
            try:
                return self._store.load_attributed(run_id), ""
            except LookupError:
                return self._store.load_raw(run_id), "нет разметки говорящих, использован сырой транскрипт"
        return self._store.load_raw(run_id), ""

