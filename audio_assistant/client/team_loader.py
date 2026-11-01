"""Загрузка состава дейли (config/team.yaml)."""
from __future__ import annotations

from pathlib import Path

import yaml

from .domain import Participant, Project, Team


def load_team(path: Path) -> Team:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    projects = []
    for proj in raw["projects"]:
        pid = str(proj["id"])
        people = tuple(
            Participant(p["name"], pid, tuple(p.get("aliases", [])))
            for p in proj["participants"]
        )
        projects.append(Project(pid, people, proj.get("title", "")))
    team = Team(tuple(projects))
    team.validate()
    return team
