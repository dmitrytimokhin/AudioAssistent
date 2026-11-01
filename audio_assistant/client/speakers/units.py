"""Нарезка транскрипта на пронумерованные фразы для разметки говорящих моделью.

Разметка идёт по фразам, а не по словам, и текст при этом никогда не переписывается моделью:
она только указывает, с какой фразы заговорил другой участник. Границы фраз:
  * конец предложения (с учётом того, что Whisper может разорвать фразу границей сегмента);
  * позиция сразу ПОСЛЕ любого упоминания имени участника: имя часто и есть место смены говорящего
    ("...идентификацию спасибо катя | следующий паша | я делаю ...");
  * слишком длинный кусок без пунктуации режется на равные части.
"""
from __future__ import annotations

import re

from ..domain import Segment, Team, Transcript
from .name_cue import _alias_pattern, _norm, iter_sentences

_AFTER_NAME = r"[\s,:;.!?\-–—]*"


def name_mention_pattern(team: Team) -> re.Pattern[str]:
    """Любое упоминание участника (имя, уменьшительное, фамилия) в любом падеже."""
    alts = "|".join(
        _alias_pattern(a)
        for p in team.participants
        for a in dict.fromkeys((*p.aliases, p.full_name, *p.full_name.split()))
    )
    return re.compile(rf"(?<!\w)(?:{alts})\b{_AFTER_NAME}")


class NameIndex:
    """Распознавание упоминаний участников в тексте (имя, уменьшительное, фамилия, падежи).
    Это делает код, а не модель: сопоставить «паша» со строкой из списка в 9 имён 4B-модель ошибается."""

    def __init__(self, team: Team):
        self._patterns = [
            (p.full_name, re.compile(rf"(?<!\w)(?:{'|'.join(_alias_pattern(a) for a in dict.fromkeys((*p.aliases, p.full_name, *p.full_name.split())))})\b"))
            for p in team.participants
        ]

    def find(self, text: str) -> list[str]:
        """Названные участники в порядке появления, без повторов."""
        norm = _norm(text)
        hits = [(m.start(), name) for name, pat in self._patterns if (m := pat.search(norm))]
        return [name for _, name in sorted(hits)]


def _wrap(piece: str, max_words: int) -> list[str]:
    words = piece.split()
    if len(words) <= max_words:
        return [piece]
    parts = -(-len(words) // max_words)          # потолок
    size = -(-len(words) // parts)               # равные части
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def build_units(transcript: Transcript, team: Team, max_words: int = 30) -> list[Segment]:
    """Фразы транскрипта (без говорящего); порядковый номер фразы = индекс + 1."""
    mention = name_mention_pattern(team)
    units: list[Segment] = []
    for sentence, start, end in iter_sentences(transcript):
        norm, cuts, prev = _norm(sentence), [], 0
        for m in mention.finditer(norm):
            cuts.append(sentence[prev:m.end()].strip())
            prev = m.end()
        cuts.append(sentence[prev:].strip())
        for piece in filter(None, cuts):
            units.extend(Segment(start, end, part) for part in _wrap(piece, max_words))
    return units
