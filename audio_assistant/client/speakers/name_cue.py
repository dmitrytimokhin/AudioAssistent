"""Атрибуция говорящих по «именным меткам».

Начало реплики: имя в начале фразы, в том числе в обороте передачи слова
("Катя.", "Дальше Паша,", "Начнем с Ромы", "Теперь переходим к Денису", "Следующий Паша").
Конец реплики: "Спасибо, Рома": дальше говорит ведущий, пока не прозвучит новая метка.
Обороты передачи слова ловятся и внутри фразы ("Сегодня начнем с дейли, начнем с Ромы"), потому что Whisper
нередко ставит запятую вместо точки. Фраза, не дописанная до конца сегмента, переносится в следующий сегмент
("...спасибо катя следующий" | "паша я делаю..."). Имя в середине обычной фразы ("обсудил с Пашей") меткой
НЕ считается.
Всё до первой метки и после «спасибо, имя» относится к неизвестному говорящему (ведущему).
"""
from __future__ import annotations

import re

from ..domain import Participant, Segment, Team, Transcript

DEFAULT_CUE_PREFIXES = (
    "дальше", "далее", "теперь", "потом", "затем", "так", "ну", "и", "а",
    "следующий", "следующая", "слово", "передаю слово", "передаю", "давай", "давайте",
    "начнем с", "начинаем с", "переходим к", "перейдем к", "что у",
)
DEFAULT_CLOSING_WORDS = ("спасибо", "благодарю")
# Однозначные обороты передачи слова: срабатывают в любом месте фразы (в отличие от голого имени)
DEFAULT_HANDOFF_PHRASES = (
    "начнем с", "начинаем с", "переходим к", "перейдем к",
    "следующий", "следующая", "передаю слово", "слово",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TERMINAL = re.compile(r"[.!?…][\"»)]*$")


_CASE_ENDINGS = r"(?:а|я|у|ю|е|и|ы|ь|й|ой|ей|ом|ем|ою|ею)?"  # «Катим» не должно совпасть с «Катя»


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")  # длина строки сохраняется -> позиции совпадают


def _alias_pattern(alias: str) -> str:
    """Основа имени + падежное окончание: Катя/Кате/Катю, Паша/Пашу/Паше."""
    a = _norm(alias)
    stem = a[:-1] if len(a) > 3 and a[-1] in "аяйь" else a
    return re.escape(stem) + _CASE_ENDINGS


def iter_sentences(transcript: Transcript):
    """Фразы с учётом границ сегментов: недописанная фраза склеивается со следующим сегментом.
    Время склеенной фразы: начало первого сегмента, конец последнего."""
    carry: tuple[str, float] | None = None
    for seg in transcript.segments:
        parts = [p for p in _SENTENCE_SPLIT.split(seg.text.strip()) if p]
        for i, part in enumerate(parts):
            start = seg.start
            if carry:
                part, start, carry = f"{carry[0]} {part}", carry[1], None
            if i == len(parts) - 1 and not _TERMINAL.search(part):
                carry = (part, start)
                continue
            yield part, start, seg.end
    if carry and transcript.segments:
        yield carry[0], carry[1], transcript.segments[-1].end


class NameCueAttributor:
    def __init__(self, team: Team, cue_prefixes: tuple[str, ...] = DEFAULT_CUE_PREFIXES,
                 max_prefix_words: int = 3, closing_words: tuple[str, ...] = DEFAULT_CLOSING_WORDS,
                 handoff_phrases: tuple[str, ...] = DEFAULT_HANDOFF_PHRASES):
        team.validate()
        exact = "|".join(re.escape(_norm(a)) for p in team.participants
                         for a in dict.fromkeys((*p.aliases, *p.full_name.split())))
        closing = "|".join(closing_words)
        # именительный падеж: «спасибо Паше за помощь» (дательный) реплику не закрывает
        self._closing = re.compile(rf"(?:{closing})[\s,]+(?:{exact})\b[\s,.!;:\-–—]*") if closing_words else None
        self._matchers = [(p, self._compile(p, cue_prefixes, max_prefix_words))
                          for p in team.participants]
        strong = "|".join(r"\s+".join(map(re.escape, x.split()))
                          for x in sorted(handoff_phrases, key=len, reverse=True))
        self._handoffs = [
            (p, re.compile(rf"(?<!\w)(?:{strong})\s+(?:{self._alt(p)})\b[\s,:;.\-–—]*"))
            for p in team.participants] if handoff_phrases else []
        self._tail = {p.full_name: re.compile(rf"^(?:{self._alt(p)})\b[\s,:;\-–—]*")
                      for p in team.participants}

    @staticmethod
    def _alt(p: Participant) -> str:
        return "|".join(_alias_pattern(a) for a in dict.fromkeys((*p.aliases, p.full_name,
                                                                   *p.full_name.split())))

    def _compile(self, p: Participant, prefixes: tuple[str, ...], n: int) -> re.Pattern[str]:
        pre = "|".join(r"\s+".join(map(re.escape, x.split()))
                       for x in sorted(prefixes, key=len, reverse=True))
        return re.compile(
            rf"^\s*(?:(?:{pre})[\s,]+){{0,{n}}}(?:{self._alt(p)})\b[\s,:;.\-–—]*"
        )

    def _find_cue(self, sentence: str) -> tuple[Participant, str] | None:
        norm = _norm(sentence)
        for participant, pattern in self._matchers:
            m = pattern.match(norm)
            if not m:
                continue
            rest = sentence[m.end():]
            tail = self._tail[participant.full_name]
            while (t := tail.match(_norm(rest))):  # "Паша Зеленский, ..." -> убрать повтор
                rest = rest[t.end():]
            return participant, rest.strip()
        return None

    def _earliest_event(self, text: str) -> tuple[int, int, str | None] | None:
        """Ближайшая метка внутри фразы: «спасибо, имя» (имя=None) или оборот передачи слова."""
        norm = _norm(text)
        best: tuple[int, int, str | None] | None = None
        if self._closing and (m := self._closing.search(norm)):
            best = (m.start(), m.end(), None)
        for participant, pattern in self._handoffs:
            m = pattern.search(norm)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), m.end(), participant.full_name)
        return best

    def _walk(self, text: str):
        """Разбирает фразу на события: ("speaker", имя|None) и ("text", реплика)."""
        rest = text
        while rest.strip():
            if cue := self._find_cue(rest):
                yield "speaker", cue[0].full_name
                rest = cue[1]
                continue
            event = self._earliest_event(rest)
            if event is None:
                yield "text", rest.strip()
                return
            start, end, name = event
            if before := rest[:start].strip():
                yield "text", before
            yield "speaker", name
            rest = rest[end:]

    def attribute(self, transcript: Transcript) -> Transcript:
        segments, _ = self.run(transcript)
        return Transcript(segments, transcript.source, {"method": "rules"})

    def run(self, transcript: Transcript, initial_speaker: str | None = None
            ) -> tuple[list[Segment], str | None]:
        """Разбор с заданным говорящим на входе; возвращает сегменты и говорящего на выходе
        (нужно разметке по чанкам, где текущий говорящий переходит из чанка в чанк)."""
        current: str | None = initial_speaker
        result: list[Segment] = []
        for sentence, start, end in iter_sentences(transcript):
            for kind, value in self._walk(sentence):
                if kind == "speaker":
                    current = value
                else:
                    result.append(Segment(start, end, value, current))
        return result, current
