"""Разметка говорящих моделью, чанками.

Правила встречи (смысл которых оценивает модель; формулировки в prompts/_tasks/speaker_attribution):
  * нет имени -> говорит предыдущий;
  * имя передаёт слово -> всё после него отдаётся названному, а до него остаётся предыдущему.

Как это устроено, чтобы маленькая модель не портила текст и не выдумывала участников:
  * код режет транскрипт на пронумерованные фразы, причём сразу после каждого имени (units.py), и сам
    распознаёт имена в любых падежах;
  * решение нужно только по фразам с именем: "кто говорит со следующей фразы" (названный / ведущий /
    без изменений). Остальные фразы наследуют говорящего. Свободная расстановка границ целиком
    маленькой моделью зацикливалась, а выбор из 2-3 вариантов по каждому имени она делает надёжно;
  * ответ ограничен схемой (enum из тех, кто назван в чанке), текст собирается кодом дословно.

Если всё помещается в chunk_chars, это один вызов; иначе чанки идут по порядку, и в каждый передаются
текущий говорящий и последние размеченные фразы. Некорректный ответ по чанку -> этот чанк размечается
правилами (NameCueAttributor), остальные остаются за моделью.
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
                 context_units: int = 3, unit_max_words: int = 30):
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

    # ── один чанк ───────────────────────────────────────────────────────
    def _named(self, units: list[Segment], lo: int, hi: int) -> dict[int, list[str]]:
        """Номера (с 1) фраз чанка, в которых названы участники, и кто именно."""
        found = {i + 1: self._names.find(units[i].text) for i in range(lo, hi)}
        return {n: names for n, names in found.items() if names}

    @staticmethod
    def _schema(options: list[str]) -> dict:
        return {
            "type": "object",
            "properties": {"decisions": {"type": "array", "items": {
                "type": "object",
                "properties": {"unit": {"type": "integer"},
                               "next_speaker": {"type": "string", "enum": options}},
                "required": ["unit", "next_speaker"]}}},
            "required": ["decisions"],
        }

    def _roster(self, names: list[str]) -> str:
        return "\n".join([f"- {HOST} (тот, кто ведёт встречу и передаёт слово)"]
                         + [f"- {n} (в речи: {self._aliases[n]})" for n in names])

    def _label_chunk(self, units: list[Segment], lo: int, hi: int, done: list[Segment],
                     current: str | None) -> tuple[list[str | None], str | None]:
        """Говорящий каждой фразы чанка и говорящий ПОСЛЕ его последней фразы."""
        named = self._named(units, lo, hi)
        if not named:                               # имён нет -> говорящий не меняется, модель не нужна
            return [current] * (hi - lo), current

        task = self._prompts.task(TASK)
        mentioned = list(dict.fromkeys(n for names in named.values() for n in names))
        context = "\n".join(f"- [{s.speaker or HOST}] {s.text}" for s in done[-self._context_units:]) \
            or "(начало записи)"

        def line(i: int) -> str:
            names = named.get(i + 1)
            return f"{i + 1}: {units[i].text}" + (f"   [названо: {', '.join(names)}]" if names else "")

        user = Template(task.user_template).safe_substitute(
            speakers=self._roster(mentioned), current_speaker=current or HOST, context=context,
            first=lo + 1, last=hi, units="\n".join(line(i) for i in range(lo, hi)),
            decide=", ".join(map(str, named)))
        raw = self._llm.generate(task.system, user,
                                 json_schema=self._schema([UNCHANGED, HOST, *mentioned]))
        return self._labels_from(raw, lo, hi, current, named)

    @staticmethod
    def _labels_from(raw: str, lo: int, hi: int, current: str | None,
                     named: dict[int, list[str]]) -> tuple[list[str | None], str | None]:
        """Решения модели -> (говорящий каждой фразы чанка, говорящий после последней фразы).

        Решение по фразе N вступает в силу со следующей фразы. Строго: решение по фразе без имени или
        в пользу участника, которого в этой фразе не называли, считается браком (так выглядит зацикливание)."""
        try:
            decisions = {int(d["unit"]): d["next_speaker"] for d in json.loads(raw)["decisions"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise BadModelOutput(f"не JSON по схеме: {exc}") from exc
        for number, target in decisions.items():
            if number not in named:
                raise BadModelOutput(f"решение по фразе {number} без имени (чанк {lo + 1}-{hi})")
            if target not in (UNCHANGED, HOST, *named[number]):
                raise BadModelOutput(f"в фразе {number} не назван {target!r}")
        labels, speaker = [], current
        for number in range(lo + 1, hi + 1):
            labels.append(speaker)                  # фраза принадлежит тому, кто говорил до неё
            target = decisions.get(number, UNCHANGED)
            if target != UNCHANGED:
                speaker = None if target == HOST else target
        return labels, speaker
