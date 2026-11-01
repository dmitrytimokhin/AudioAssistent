"""Профили аудиторий из папки prompts/<audience>/.

Новая аудитория = новая папка, код не меняется (OCP):
  system.md    — роль и правила стиля
  person.md    — шаблон запроса на одного сотрудника (string.Template, ${var})
  examples.md  — примеры желаемого стиля (подставляются в ${examples})
  audience.yaml — раскладка итогового текста (заголовок, проект, строка сотрудника)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Layout:
    header: str = "Дейли {date}"
    project_header: str = "Проект {project}:"
    item: str = "{index}. {full_name} - {text}"
    date_format: str = "%d.%m.%Y"
    empty_text: str = "нет обновлений"
    error_text: str = "ошибка обработки, статус не сформирован"


@dataclass(frozen=True)
class AudienceProfile:
    name: str
    system: str
    person_template: str
    examples: str
    layout: Layout


@dataclass(frozen=True)
class TaskPrompt:
    """Промпт служебной задачи (не аудитория): prompts/_tasks/<имя>/{system,user}.md."""

    system: str
    user_template: str


class FilePromptRepository:
    def __init__(self, root: Path):
        self._root = Path(root)

    def available(self) -> list[str]:
        return sorted(p.name for p in self._root.iterdir() if (p / "system.md").exists())

    def get(self, audience: str) -> AudienceProfile:
        folder = self._root / audience
        if not folder.is_dir():
            raise KeyError(f"Аудитория '{audience}' не найдена. Доступно: {self.available()}")
        read = lambda name: (folder / name).read_text(encoding="utf-8").strip()  # noqa: E731
        layout_file = folder / "audience.yaml"
        layout = Layout(**yaml.safe_load(layout_file.read_text(encoding="utf-8"))) \
            if layout_file.exists() else Layout()
        return AudienceProfile(audience, read("system.md"), read("person.md"),
                               read("examples.md") if (folder / "examples.md").exists() else "",
                               layout)

    def task(self, name: str) -> TaskPrompt:
        folder = self._root / "_tasks" / name
        if not folder.is_dir():
            raise KeyError(f"Промпт задачи '{name}' не найден: {folder}")
        read = lambda f: (folder / f).read_text(encoding="utf-8").strip()  # noqa: E731
        return TaskPrompt(read("system.md"), read("user.md"))
