"""Разбор одной встречи промптом своего типа (prompts/meetings/<тип>/)."""
from __future__ import annotations

from dataclasses import dataclass

from .domain import Team, Transcript
from .meeting_kinds import MeetingKinds
from .meeting_text import fill, render_transcript, split_text
from .ports import LLMClient
from .prompt_runner import model_name_of
from .prompts import FilePromptRepository


@dataclass
class AnalysisResult:
    prompt_name: str
    source: str
    model: str
    chunks: int
    text: str


def meeting_values(run: dict, kinds: MeetingKinds | None, team: Team) -> dict[str, str]:
    """Поля встречи для подстановки в промпт (всё, кроме {transcript})."""
    kind = run.get("kind") or ""
    title = kinds.get(kind).title if kinds and kind in kinds.names else kind
    return {
        "date": str(run.get("meeting_date", "")), "kind": kind, "kind_title": title,
        "file": str(run.get("source", "")), "tags": ", ".join(run.get("tags") or []),
        "participants": ", ".join(p.full_name for p in team.participants),
    }


class MeetingAnalyzer:
    def __init__(self, llm: LLMClient, prompts: FilePromptRepository, team: Team,
                 kinds: MeetingKinds, chunk_chars: int):
        self._llm, self._prompts, self._team, self._kinds = llm, prompts, team, kinds
        self._chunk_chars = chunk_chars

    def prompt_for(self, run: dict, prompt: str | None) -> str:
        if prompt:
            return prompt
        if not run.get("kind"):
            raise ValueError("У встречи не указан тип: задайте kind (PATCH /assistant/runs/{id}) "
                             "или передайте prompt — имя папки в prompts/meetings/")
        return self._kinds.get(run["kind"]).prompt

    def analyze(self, run: dict, transcript: Transcript, source: str, prompt: str | None = None
                ) -> AnalysisResult:
        name = self.prompt_for(run, prompt)
        profile = self._prompts.meeting_prompt(name)
        values = meeting_values(run, self._kinds, self._team)
        parts = split_text(render_transcript(transcript, source), self._chunk_chars)
        answers = [self._llm.generate(profile.system, fill(profile.user_template, transcript=part, **values))
                   for part in parts]
        text = answers[0] if len(answers) == 1 else "\n\n".join(
            f"[Часть {i} из {len(answers)}]\n{a}" for i, a in enumerate(answers, 1))
        return AnalysisResult(name, source, model_name_of(self._llm), len(answers), text.strip())
