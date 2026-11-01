"""Статус на каждого сотрудника отдельным запросом к LLM.

Для модели 4B так надёжнее, чем отдавать весь дейли целиком: короткий контекст,
нет «перепутанных» людей, а структуру итогового текста собирает код (rendering.py).
"""
from __future__ import annotations

import re
from datetime import date
from string import Template

from observability.logging.setup import get_logger

from .domain import DailySummary, Participant, PersonStatus, Team, Transcript
from .ports import HistoryRepository, LLMClient
from .prompts import AudienceProfile
from .run_context import current_audience, current_person

logger = get_logger(__name__)

_PREFIX = re.compile(r"^\s*(?:\d+[.)]\s*)?(?:[А-ЯЁA-Z][\wё-]+\s+){1,2}[-–—:]\s+")


class DailySummarizer:
    def __init__(self, llm: LLMClient, history: HistoryRepository, team: Team):
        self._llm, self._history, self._team = llm, history, team

    def summarize(self, transcript: Transcript, profile: AudienceProfile, day: date) -> DailySummary:
        spoken = transcript.text_by_speaker()
        current_audience.set(profile.name)
        statuses = [self._status(p, spoken.get(p.full_name), profile, day)
                    for p in self._team.participants]
        return DailySummary(day, profile.name, statuses)

    def _status(self, p: Participant, utterance: str | None, profile: AudienceProfile,
                day: date) -> PersonStatus:
        if not utterance:
            return PersonStatus(p.full_name, p.project_id, profile.layout.empty_text, has_data=False)
        previous = self._history.previous_status(p.full_name, day, profile.name)
        user = Template(profile.person_template).safe_substitute(
            date=day.strftime(profile.layout.date_format),
            project_id=p.project_id,
            full_name=p.full_name,
            previous_status=previous or "нет данных",
            examples=profile.examples,
            utterance=utterance,
        )
        current_person.set(p.full_name)
        try:
            text = self._clean(self._llm.generate(profile.system, user), p.full_name)
        except Exception as exc:
            # Сбой на одном сотруднике не должен терять статусы остальных
            logger.error("Status generation failed", full_name=p.full_name, error=str(exc))
            return PersonStatus(p.full_name, p.project_id, profile.layout.error_text, has_data=False)
        finally:
            current_person.set(None)
        return PersonStatus(p.full_name, p.project_id, text)

    @staticmethod
    def _clean(text: str, full_name: str = "") -> str:
        text = " ".join(text.split())      # одна строка
        if full_name and text.startswith(full_name):
            text = text[len(full_name):].lstrip(" -–—:,")  # "Иванов Иван собрал..." -> "собрал..."
        text = _PREFIX.sub("", text)       # модель иногда повторяет "1. Иванов Иван - "
        return text.strip(" \"'«»")
