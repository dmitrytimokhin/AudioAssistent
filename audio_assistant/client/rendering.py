"""Сборка итогового текста по раскладке аудитории (без LLM)."""
from __future__ import annotations

from .domain import DailySummary
from .prompts import Layout


class StatusRenderer:
    def render(self, summary: DailySummary, layout: Layout) -> str:
        lines = [layout.header.format(date=summary.day.strftime(layout.date_format))]
        for project_id, statuses in summary.by_project().items():
            lines.append(layout.project_header.format(project=project_id))
            for i, st in enumerate(statuses, 1):
                lines.append(layout.item.format(index=i, full_name=st.full_name, text=st.text))
        return "\n".join(lines)
