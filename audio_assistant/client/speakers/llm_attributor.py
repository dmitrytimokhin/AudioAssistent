"""Контекстная разметка каждой фразы; модель выбирает автора, код сохраняет текст.

Чанки получают предыдущие размеченные фразы и следующие фразы для контекста.
Неполный или некорректный ответ переводит только текущий чанк на осторожные правила.
"""
from __future__ import annotations

import json
from string import Template

from observability.logging.setup import get_logger

from ..domain import Segment, Team, Transcript
from ..ports import LLMClient
from ..prompts import FilePromptRepository
from .name_cue import NameCueAttributor
from .units import NameIndex, build_units

logger = get_logger(__name__)

HOST = "ведущий"          # метка ведущего в схеме ответа; в Transcript это speaker=None
UNCHANGED = "без изменений"
TASK = "speaker_attribution"


class BadModelOutput(ValueError):
    """Ответ модели нельзя использовать (не JSON, чужой участник, номер вне чанка...)."""


class LLMSpeakerAttributor:
    def __init__(self, llm: LLMClient, team: Team, prompts: FilePromptRepository,
                 fallback: NameCueAttributor, chunk_chars: int = 6000,
                 context_units: int = 12, unit_max_words: int = 30):
        if chunk_chars < 1 or context_units < 0 or unit_max_words < 1:
            raise ValueError("Размеры чанка и фразы должны быть положительными, контекст — неотрицательным")
        team.validate()
        self._llm, self._team, self._prompts, self._fallback = llm, team, prompts, fallback
        self._chunk_chars, self._context_units, self._max_words = chunk_chars, context_units, unit_max_words
        self._names = NameIndex(team)
        self._aliases = {p.full_name: ", ".join(p.aliases) or p.full_name for p in team.participants}

    # ── публичный интерфейс (порт SpeakerAttributor) ───────────────────────
    def attribute(self, transcript: Transcript) -> Transcript:
        units = build_units(transcript, self._team, self._max_words)
        chunks = self._chunks(units)
        result: list[Segment] = []
        current: str | None = None
        fallbacks: list[int] = []

        for index, (lo, hi) in enumerate(chunks):
            try:
                labels, current_after = self._label_chunk(units, lo, hi, result, current)
                segs = [Segment(u.start, u.end, u.text, labels[i]) for i, u in enumerate(units[lo:hi])]
                current = current_after       # решение по последней фразе чанка действует уже в следующем
            except Exception as exc:  # BadModelOutput, сеть, таймаут: чанк уходит на правила
                logger.warning("LLM attribution failed, chunk falls back to rules",
                               chunk=index + 1, of=len(chunks), error=str(exc))
                fallbacks.append(index + 1)
                segs, current = self._fallback.run(Transcript(units[lo:hi]), current)
            result.extend(segs)

        logger.info("LLM attribution done", units=len(units), chunks=len(chunks), fallback_chunks=fallbacks)
        return Transcript(result, transcript.source, {
            "method": "llm", "units": len(units), "chunks": len(chunks), "fallback_chunks": fallbacks})

    # ── чанки ───────────────────────────────────────────────────────────
    def _chunks(self, units: list[Segment]) -> list[tuple[int, int]]:
        chunks, lo, size = [], 0, 0
        for i, unit in enumerate(units):
            if size and size + len(unit.text) > self._chunk_chars:
                chunks.append((lo, i))
                lo, size = i, 0
            size += len(unit.text) + 8     # + номер и перевод строки
        if lo < len(units):
            chunks.append((lo, len(units)))
        return chunks

    @staticmethod
    def _schema(options: list[str]) -> dict:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {"decisions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"unit": {"type": "integer"},
                               "speaker": {"type": "string", "enum": options}},
                "required": ["unit", "speaker"]}}},
            "required": ["decisions"],
        }

    def _roster(self) -> str:
        return "\n".join([f"- {HOST} (ведущий или неизвестный автор)"]
                         + [f"- {n} (в речи: {aliases})" for n, aliases in self._aliases.items()])

    def _label_chunk(self, units: list[Segment], lo: int, hi: int, done: list[Segment],
                     current: str | None) -> tuple[list[str | None], str | None]:
        task = self._prompts.task(TASK)
        previous = done[-self._context_units:] if self._context_units else []
        context = "\n".join(f"- [{s.speaker or HOST}] {s.text}" for s in previous) or "(нет контекста)"

        def line(i: int) -> str:
            names = self._names.find(units[i].text)
            return f"{i + 1}: {units[i].text}" + (f"   [названо: {', '.join(names)}]" if names else "")

        # Будущие фразы помогают отличить обращение от ответа; они не размечаются этим вызовом.
        following = "\n".join(line(i) for i in range(hi, min(len(units), hi + self._context_units)))
        user = Template(task.user_template).safe_substitute(
            speakers=self._roster(), current_speaker=current or HOST, context=context,
            following=following or "(конец записи)", first=lo + 1, last=hi,
            units="\n".join(line(i) for i in range(lo, hi)),
            decide=", ".join(map(str, range(lo + 1, hi + 1))))
        raw = self._llm.generate(task.system, user,
                                 json_schema=self._schema([HOST, *self._aliases]))
        return self._labels_from(raw, lo, hi)

    def _labels_from(self, raw: str, lo: int, hi: int) -> tuple[list[str | None], str | None]:
        try:
            rows = json.loads(raw)["decisions"]
            if not isinstance(rows, list):
                raise ValueError("decisions должен быть массивом")
            decisions = {}
            for row in rows:
                number, target = row["unit"], row["speaker"]
                if type(number) is not int or not lo < number <= hi or number in decisions:
                    raise ValueError(f"неверный или повторный номер фразы: {number!r}")
                if not isinstance(target, str) or target not in (HOST, *self._aliases):
                    raise ValueError(f"неизвестный говорящий: {target!r}")
                decisions[number] = None if target == HOST else target
            if set(decisions) != set(range(lo + 1, hi + 1)):
                raise ValueError("нужен автор каждой фразы чанка")
        except (ValueError, KeyError, TypeError) as exc:
            raise BadModelOutput(f"не JSON по схеме: {exc}") from exc
        labels = [decisions[i] for i in range(lo + 1, hi + 1)]
        return labels, labels[-1]
