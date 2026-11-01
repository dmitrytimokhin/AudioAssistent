"""История прошлых дейликов: data/history/<date>_<audience>.json."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .domain import DailySummary


class FileHistoryRepository:
    def __init__(self, root: Path):
        self._root = Path(root)

    def save(self, summary: DailySummary) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{summary.day.isoformat()}_{summary.audience}.json"
        data = {
            "day": summary.day.isoformat(),
            "audience": summary.audience,
            "statuses": [vars(s) for s in summary.statuses],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_summary(self, run_id: int, audience: str) -> DailySummary | None:
        return None  # файловая история не знает про run_id

    def previous_status(self, full_name: str, before: date, audience: str) -> str | None:
        """Последний статус сотрудника до даты; приоритет — та же аудитория."""
        candidates = []
        for path in self._root.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            day = date.fromisoformat(data["day"])
            if day >= before:
                continue
            for st in data["statuses"]:
                if st["full_name"] == full_name and st.get("has_data", True):
                    candidates.append((data["audience"] == audience, day, st["text"]))
        return max(candidates)[2] if candidates else None
