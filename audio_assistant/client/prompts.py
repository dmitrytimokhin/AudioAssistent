"""Профили аудиторий из папки prompts/<audience>/.

Новая аудитория = новая папка, код не меняется (OCP):
  system.md    — роль и правила стиля
  person.md    — шаблон запроса на одного сотрудника (string.Template, ${var})
  examples.md  — примеры желаемого стиля (подставляются в ${examples})
  audience.yaml — раскладка итогового текста (заголовок, проект, строка сотрудника)

Остальные промпты:
  prompts/meetings/<тип встречи>/{system,user}.md  — разбор одной встречи своего типа
  prompts/digests/<имя>/{digest.yaml,system,user}.md — сводка по многим встречам в таблицу (Excel)
  prompts/_tasks/<имя>/{system,user}.md              — служебные задачи (разметка говорящих)
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


@dataclass(frozen=True)
class MeetingPrompt:
    """Разбор одной встречи: prompts/meetings/<имя>/{system,user}.md."""

    name: str
    system: str
    user_template: str


@dataclass(frozen=True)
class ImagePrompt:
    """Разбор картинок: prompts/images/<имя>/{system,user}.md."""

    name: str
    system: str
    user_template: str


META_COLUMNS = {"date": "Дата", "file": "Файл", "kind": "Тип встречи", "run_id": "Запуск", "tags": "Теги"}


@dataclass(frozen=True)
class DigestProfile:
    """Сводка по встречам за период: prompts/digests/<имя>/{digest.yaml,system.md,user.md}."""

    name: str
    title: str
    system: str
    user_template: str
    columns: tuple[str, ...]               # колонки, которые заполняет модель
    meta_columns: tuple[str, ...] = ("date", "file")   # колонки, которые заполняет код (META_COLUMNS)
    kinds: tuple[str, ...] = ()            # какие типы встреч брать по умолчанию (пусто = любые)
    source: str = "dialogue"               # dialogue | raw

    @property
    def headers(self) -> list[str]:
        return [META_COLUMNS[m] for m in self.meta_columns] + list(self.columns)


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

    # ── разбор встреч по типам ───────────────────────────────────────────
    def meeting_prompts(self) -> list[str]:
        base = self._root / "meetings"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir()
                      if (p / "system.md").exists() and (p / "user.md").exists())

    def meeting_prompt(self, name: str) -> MeetingPrompt:
        folder = self._root / "meetings" / name
        if not ((folder / "system.md").exists() and (folder / "user.md").exists()):
            raise KeyError(f"Нет промпта встречи '{name}'. Создайте prompts/meetings/{name}/system.md и user.md. "
                           f"Есть: {self.meeting_prompts()}")
        read = lambda f: (folder / f).read_text(encoding="utf-8").strip()  # noqa: E731
        return MeetingPrompt(name, read("system.md"), read("user.md"))

    # ── сводки по периоду ────────────────────────────────────────────────
    def digests(self) -> list[str]:
        base = self._root / "digests"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if (p / "digest.yaml").exists())

    def digest(self, name: str) -> DigestProfile:
        folder = self._root / "digests" / name
        if not (folder / "digest.yaml").exists():
            raise KeyError(f"Нет сводки '{name}'. Создайте prompts/digests/{name}/digest.yaml, system.md, user.md. "
                           f"Есть: {self.digests()}")
        cfg = yaml.safe_load((folder / "digest.yaml").read_text(encoding="utf-8")) or {}
        columns = tuple(str(c).strip() for c in cfg.get("columns") or ())
        meta = tuple(cfg.get("meta_columns", ("date", "file")))
        source = cfg.get("source", "dialogue")
        if not columns or len(set(columns)) != len(columns):
            raise ValueError(f"digest.yaml '{name}': columns должен быть непустым списком без повторов")
        if bad := [m for m in meta if m not in META_COLUMNS]:
            raise ValueError(f"digest.yaml '{name}': неизвестные meta_columns {bad}; можно {list(META_COLUMNS)}")
        if clash := [c for c in columns if c in META_COLUMNS.values()]:
            raise ValueError(f"digest.yaml '{name}': колонки {clash} занимает код (meta_columns), переименуйте")
        if source not in ("dialogue", "raw"):
            raise ValueError(f"digest.yaml '{name}': source = dialogue | raw")
        read = lambda f: (folder / f).read_text(encoding="utf-8").strip()  # noqa: E731
        return DigestProfile(name, cfg.get("title", name), read("system.md"), read("user.md"),
                             columns, meta, tuple(cfg.get("kinds") or ()), source)

    # ── разбор картинок ──────────────────────────────────────────────────
    def image_prompts(self) -> list[str]:
        base = self._root / "images"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir()
                      if (p / "system.md").exists() and (p / "user.md").exists())

    def image_prompt(self, name: str) -> ImagePrompt:
        folder = self._root / "images" / name
        if not ((folder / "system.md").exists() and (folder / "user.md").exists()):
            raise KeyError(f"Нет промпта картинок '{name}'. Создайте prompts/images/{name}/system.md и user.md. "
                           f"Есть: {self.image_prompts()}")
        read = lambda f: (folder / f).read_text(encoding="utf-8").strip()  # noqa: E731
        return ImagePrompt(name, read("system.md"), read("user.md"))
