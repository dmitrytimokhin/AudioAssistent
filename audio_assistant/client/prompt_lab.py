"""Промпт-лаборатория: создание, проверка и сравнение промптов без правки кода.

Четыре вида промптов, у каждого своя папка в prompts/:
  audience  prompts/<имя>/                  статус сотрудника для аудитории (руководство, РП, команда)
  meeting   prompts/meetings/<имя>/         разбор одной встречи (+ тип встречи в config/meeting_kinds.yaml)
  digest    prompts/digests/<имя>/          сводка по встречам за период в Excel
  image     prompts/images/<имя>/           разбор фото

PromptLab работает только с файлами (создать из шаблона prompts/_templates/, проверить, показать итоговый текст).
PromptTrial гоняет черновики на реальной встрече и ничего не сохраняет в Postgres: чтобы сравнивать версии промпта.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from string import Template

import yaml

from .domain import Segment, Transcript
from .meeting_text import fill
from .prompts import FilePromptRepository, Layout

KINDS = ("audience", "meeting", "digest", "image")

# что понимает код в каждом виде промпта (всё остальное в фигурных скобках -- вероятная опечатка)
_FILES = {
    "audience": ("system.md", "person.md", "examples.md", "audience.yaml"),
    "meeting": ("system.md", "user.md"),
    "digest": ("digest.yaml", "system.md", "user.md"),
    "image": ("system.md", "user.md"),
}
_REQUIRED = {
    "audience": ("system.md", "person.md"),
    "meeting": ("system.md", "user.md"),
    "digest": ("digest.yaml", "system.md", "user.md"),
    "image": ("system.md", "user.md"),
}
_LABELS = {
    "meeting": {"transcript", "date", "kind", "kind_title", "file", "tags", "participants"},
    "digest": {"transcript", "date", "kind", "kind_title", "file", "tags", "participants"},
    "image": {"file", "count", "date", "tags"},
}
_AUDIENCE_VARS = {"date", "project_id", "full_name", "previous_status", "examples", "utterance"}
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_BRACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DOLLAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TEMPLATE_HOLE = re.compile(r"<[^<>\n]{3,}>")      # заготовки шаблона: <КОГО: ...>
_SYSTEM_TOO_LONG = 6000                            # символов; для 4B длинные инструкции размываются


@dataclass(frozen=True)
class Issue:
    level: str      # error | warn | info
    text: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.text}"


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1e6) % 1_000_000:06d}"


def _suggest(label: str, known: set[str]) -> str:
    close = difflib.get_close_matches(label, sorted(known), n=1)
    return f" Возможно, {close[0]}?" if close else ""


class PromptLab:
    def __init__(self, prompts_dir: Path, kinds_file: Path, protected: tuple[tuple[str, str], ...] = ()):
        """protected - пары (вид, имя), которые нельзя удалить (например аудитория по умолчанию)."""
        self.root = Path(prompts_dir)
        self.kinds_file = Path(kinds_file)
        self.protected = set(protected)
        self._repo = FilePromptRepository(self.root)

    # ── где что лежит ───────────────────────────────────────────────────
    def folder(self, kind: str, name: str) -> Path:
        self._check_kind(kind)
        if Path(name).name != name or name.startswith("."):
            raise ValueError(f"Недопустимое имя промпта: {name!r}")
        return {"audience": self.root, "meeting": self.root / "meetings", "digest": self.root / "digests",
                "image": self.root / "images"}[kind] / name

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"Вид промпта: {' | '.join(KINDS)}; получено {kind!r}")

    def names(self, kind: str) -> list[str]:
        self._check_kind(kind)
        if kind == "audience":
            return self._repo.available()
        return {"meeting": self._repo.meeting_prompts, "digest": self._repo.digests,
                "image": self._repo.image_prompts}[kind]()

    def _registered_kinds(self) -> dict[str, str]:
        """ключ типа встречи -> папка промпта (из config/meeting_kinds.yaml)."""
        raw = yaml.safe_load(self.kinds_file.read_text(encoding="utf-8")) or {}
        return {k: (cfg or {}).get("prompt", k) for k, cfg in (raw.get("kinds") or {}).items()}

    # ── каталог ─────────────────────────────────────────────────────────
    def catalog(self) -> list[dict]:
        """Все промпты одной таблицей: pd.DataFrame(lab.catalog())."""
        registered = self._registered_kinds()
        rows = []
        for kind in KINDS:
            for name in self.names(kind):
                folder = self.folder(kind, name)
                files = [f for f in _FILES[kind] if (folder / f).exists()]
                size = sum((folder / f).stat().st_size for f in files)
                changed = max((folder / f).stat().st_mtime for f in files) if files else 0
                used = ", ".join(k for k, p in registered.items() if p == name) if kind == "meeting" else ""
                rows.append({"вид": kind, "имя": name, "тип встречи": used, "файлы": ", ".join(files),
                             "символов": size, "изменён": time.strftime("%Y-%m-%d %H:%M", time.localtime(changed))})
        return rows

    # ── создание ────────────────────────────────────────────────────────
    def new(self, kind: str, name: str, title: str | None = None, copy_from: str | None = None) -> list[Path]:
        """Создаёт промпт из шаблона (или копирует существующий: copy_from="daily").

        Для meeting ещё и регистрирует тип встречи в config/meeting_kinds.yaml (title - название для людей).
        Существующее не перезаписывается."""
        self._check_kind(kind)
        if not _NAME.match(name):
            raise ValueError(f"Имя {name!r}: латиница, цифры и _, начинается с буквы (например weekly_review)")
        target = self.folder(kind, name)
        if target.exists():
            raise FileExistsError(f"{target} уже есть. Правьте файлы там или выберите другое имя")
        if copy_from:
            source = self.folder(kind, copy_from)
            if not source.is_dir():
                raise KeyError(f"Нет промпта {copy_from!r} для копирования. Есть: {self.names(kind)}")
        else:
            source = self.root / "_templates" / kind
        target.mkdir(parents=True)
        created = []
        for file_name in _FILES[kind]:
            if (source / file_name).exists():
                shutil.copy2(source / file_name, target / file_name)
                created.append(target / file_name)
        if kind == "meeting":
            self._register_meeting_kind(name, title or name)
        return created

    def _register_meeting_kind(self, name: str, title: str) -> None:
        if name in self._registered_kinds():
            return
        text = self.kinds_file.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        self.kinds_file.write_text(f"{text}  {name}:\n    title: {json.dumps(title, ensure_ascii=False)}\n",
                                   encoding="utf-8")

    # ── чтение и правка ─────────────────────────────────────────────────
    def files(self, kind: str) -> tuple[str, ...]:
        self._check_kind(kind)
        return _FILES[kind]

    def read(self, kind: str, name: str) -> dict[str, str]:
        """Все файлы промпта: {имя файла: текст}; отсутствующие - пустой строкой."""
        folder = self._existing(kind, name)
        return {f: (folder / f).read_text(encoding="utf-8") if (folder / f).exists() else ""
                for f in _FILES[kind]}

    def save(self, kind: str, name: str, file_name: str, text: str) -> Path | None:
        """Записывает файл. Прежняя версия уходит в prompts/.history/ (откат: restore). Возвращает путь копии."""
        folder = self._existing(kind, name)
        if file_name not in _FILES[kind]:
            raise ValueError(f"{file_name!r}: для вида {kind} доступны {list(_FILES[kind])}")
        target = folder / file_name
        backup = None
        if target.exists():
            if target.read_text(encoding="utf-8") == text:
                return None
            backup = self._history_dir(kind, name, file_name) / _stamp()
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.write_text(text, encoding="utf-8")
        return backup

    def history(self, kind: str, name: str, file_name: str) -> list[str]:
        """Сохранённые прежние версии файла, новые первыми (метки для restore)."""
        directory = self._history_dir(kind, name, file_name)
        return sorted((p.name for p in directory.iterdir()), reverse=True) if directory.is_dir() else []

    def restore(self, kind: str, name: str, file_name: str, version: str) -> None:
        """Возвращает файл к сохранённой версии (текущая при этом тоже сохраняется в историю)."""
        source = self._history_dir(kind, name, file_name) / version
        if not source.is_file():
            raise KeyError(f"Нет версии {version!r}. Есть: {self.history(kind, name, file_name)}")
        self.save(kind, name, file_name, source.read_text(encoding="utf-8"))

    def _history_dir(self, kind: str, name: str, file_name: str) -> Path:
        return self.root / ".history" / kind / name / file_name

    def _existing(self, kind: str, name: str) -> Path:
        folder = self.folder(kind, name)
        if not folder.is_dir():
            raise KeyError(f"Нет промпта {kind}/{name}. Есть: {self.names(kind)}")
        return folder

    # ── удаление (в корзину, с возвратом) ───────────────────────────────
    def delete(self, kind: str, name: str, confirm: bool = False, unregister_kind: bool = False) -> dict:
        """Без confirm - только план (ничего не меняется). С confirm папка уходит в prompts/.trash/ (вернуть: undelete).

        Для meeting, привязанного к типу встречи, нужен unregister_kind=True: тип тоже убирается из
        config/meeting_kinds.yaml. Уже сохранённые встречи этого типа остаются в базе, но получить для них
        разбор по типу будет нельзя, пока тип не вернётся."""
        folder = self._existing(kind, name)
        if (kind, name) in self.protected:
            raise ValueError(f"{kind}/{name} защищён от удаления (используется по умолчанию)")
        bound = [k for k, p in self._registered_kinds().items() if p == name] if kind == "meeting" else []
        plan = {"папка": str(folder), "файлы": [f for f in _FILES[kind] if (folder / f).exists()],
                "типы встреч": bound, "выполнено": False}
        if bound and not unregister_kind:
            plan["предупреждение"] = (f"промпт привязан к типам {bound}; удаление с unregister_kind=True уберёт и "
                                      f"их из {self.kinds_file.name}")
            if confirm:
                raise ValueError(plan["предупреждение"])
        if not confirm:
            return plan
        trash = self.root / ".trash" / f"{kind}__{name}__{_stamp()}"
        trash.parent.mkdir(exist_ok=True)
        shutil.move(str(folder), str(trash))
        for key in bound:
            self._unregister_meeting_kind(key)
        plan.update({"выполнено": True, "корзина": trash.name})
        return plan

    def trash(self) -> list[str]:
        directory = self.root / ".trash"
        return sorted((p.name for p in directory.iterdir()), reverse=True) if directory.is_dir() else []

    def undelete(self, trash_name: str) -> Path:
        """Возвращает промпт из корзины на место (тип встречи, если был, нужно создать заново: new/регистрация)."""
        source = self.root / ".trash" / trash_name
        if not source.is_dir():
            raise KeyError(f"В корзине нет {trash_name!r}. Есть: {self.trash()}")
        kind, name, _ = trash_name.split("__", 2)
        target = self.folder(kind, name)
        if target.exists():
            raise FileExistsError(f"{target} уже есть: удалите или переименуйте его перед возвратом")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        if kind == "meeting":
            self._register_meeting_kind(name, name)      # название можно поправить в config/meeting_kinds.yaml
        return target

    def _unregister_meeting_kind(self, key: str) -> None:
        lines = self.kinds_file.read_text(encoding="utf-8").splitlines(keepends=True)
        out, skipping = [], False
        for line in lines:
            if line.startswith(f"  {key}:"):
                skipping = True
                continue
            if skipping and (line.startswith("    ") or not line.strip()):
                continue
            skipping = False
            out.append(line)
        self.kinds_file.write_text("".join(out), encoding="utf-8")

    # ── проверка ────────────────────────────────────────────────────────
    def check(self, kind: str, name: str) -> list[Issue]:
        """Находит типичные ошибки до запуска: пустые файлы, опечатки в метках, забытые заготовки."""
        self._check_kind(kind)
        folder = self.folder(kind, name)
        if not folder.is_dir():
            return [Issue("error", f"нет папки {folder}. Создайте: lab.new('{kind}', '{name}')")]
        issues: list[Issue] = []
        texts: dict[str, str] = {}
        for file_name in _FILES[kind]:
            path = folder / file_name
            if not path.exists():
                if file_name in _REQUIRED[kind]:
                    issues.append(Issue("error", f"нет файла {file_name}"))
                continue
            texts[file_name] = path.read_text(encoding="utf-8")
            if not texts[file_name].strip() and file_name in _REQUIRED[kind]:
                issues.append(Issue("error", f"{file_name} пустой"))
            if hole := _TEMPLATE_HOLE.search(texts[file_name]):
                issues.append(Issue("warn", f"{file_name}: осталась заготовка шаблона {hole.group(0)!r}, замените её"))

        if kind == "audience":
            issues += self._check_audience(texts)
        else:
            known = _LABELS[kind]
            for file_name in ("user.md",):
                for label in dict.fromkeys(_BRACE.findall(texts.get(file_name, ""))):
                    if label not in known:
                        issues.append(Issue("warn", f"{file_name}: метка {{{label}}} неизвестна и останется как есть."
                                                    f"{_suggest(label, known)}"))
            if kind in ("meeting", "digest") and "{transcript}" not in texts.get("user.md", "x{transcript}"):
                issues.append(Issue("warn", "в user.md нет {transcript}: текст встречи допишется в конец запроса"))
            if kind == "digest":
                try:
                    self._repo.digest(name)
                except (KeyError, ValueError) as exc:
                    issues.append(Issue("error", str(exc)))
            if kind == "meeting" and name not in self._registered_kinds().values():
                issues.append(Issue("info", f"промпт не привязан к типу встречи в {self.kinds_file.name}: "
                                            f"вызывать его нужно явно (prompt='{name}')"))
        if len(texts.get("system.md", "")) > _SYSTEM_TOO_LONG:
            issues.append(Issue("warn", f"system.md длиннее {_SYSTEM_TOO_LONG} символов: маленькая модель теряет "
                                        "правила в длинной инструкции, оставьте главное"))
        return issues or [Issue("info", "замечаний нет")]

    @staticmethod
    def _check_audience(texts: dict[str, str]) -> list[Issue]:
        issues = []
        person = texts.get("person.md", "")
        for var in dict.fromkeys(_DOLLAR.findall(person)):
            if var not in _AUDIENCE_VARS:
                issues.append(Issue("warn", f"person.md: переменная ${{{var}}} неизвестна и останется как есть."
                                            f"{_suggest(var, _AUDIENCE_VARS)}"))
        if person and "${utterance}" not in person:
            issues.append(Issue("error", "в person.md нет ${utterance}: модель не увидит речь сотрудника"))
        if "${examples}" in person and not texts.get("examples.md", "").strip():
            issues.append(Issue("warn", "person.md использует ${examples}, а examples.md пуст или отсутствует"))
        if "audience.yaml" in texts:
            try:
                cfg = yaml.safe_load(texts["audience.yaml"]) or {}
                Layout(**cfg)
            except (yaml.YAMLError, TypeError) as exc:
                issues.append(Issue("error", f"audience.yaml: {exc}"))
            else:
                item = cfg.get("item", "")
                if item and ("{full_name}" not in item or "{text}" not in item):
                    issues.append(Issue("warn", "audience.yaml: в item нет {full_name} или {text}"))
        return issues

    def check_all(self) -> list[dict]:
        """Проверка всех промптов: pd.DataFrame(lab.check_all())."""
        return [{"вид": kind, "имя": name, "уровень": issue.level, "замечание": issue.text}
                for kind in KINDS for name in self.names(kind) for issue in self.check(kind, name)]

    # ── что реально уйдёт в модель ──────────────────────────────────────
    def preview(self, kind: str, name: str, **values: str) -> str:
        """Итоговый текст system и user с подставленными значениями (по умолчанию - образцы)."""
        self._check_kind(kind)
        sample = {"transcript": "<здесь будет расшифровка>", "date": date.today().isoformat(), "kind": "daily",
                  "kind_title": "Дейли", "file": "20261102103215.WAV", "tags": "тест", "participants": "Иван, Катя",
                  "count": "1", "project_id": "1234", "full_name": "Иван Иванов",
                  "previous_status": "нет данных", "utterance": "<здесь будет речь сотрудника>", **values}
        folder = self.folder(kind, name)
        read = lambda f: (folder / f).read_text(encoding="utf-8").strip() if (folder / f).exists() else ""  # noqa: E731
        if kind == "audience":
            sample.setdefault("examples", read("examples.md"))
            user = Template(read("person.md")).safe_substitute(sample)
        else:
            user = fill(read("user.md"), **sample)
        return f"=== SYSTEM ===\n{read('system.md')}\n\n=== USER ===\n{user}"


def transcript_from_run(detail: dict, source: str = "dialogue") -> Transcript:
    """Ответ GET /assistant/runs/{id} -> Transcript (dialogue = с говорящими, raw = сырой)."""
    if source == "dialogue":
        rows = detail.get("dialogue") or []
        return Transcript([Segment(r.get("start") or 0.0, r.get("end") or 0.0, r["text"], r.get("speaker"))
                           for r in rows], str(detail.get("run", {}).get("source", "")))
    return Transcript([Segment(r.get("start", 0.0), r.get("end", 0.0), r["text"], r.get("speaker"))
                       for r in detail.get("raw_transcript") or []], str(detail.get("run", {}).get("source", "")))


@dataclass
class TrialResult:
    name: str
    text: str
    seconds: float
    error: str | None = None


class PromptTrial:
    """Прогон черновиков промптов на реальной встрече без записи в Postgres.

    Промпты читаются с диска на каждый прогон: поправили файл - запустили снова."""

    def __init__(self, pipeline):
        self._p = pipeline

    def meeting(self, transcript: Transcript, names: list[str], *, run: dict | None = None,
                source: str = "dialogue") -> list[TrialResult]:
        run = run or {"meeting_date": date.today().isoformat(), "kind": "", "source": transcript.source, "tags": []}
        return [self._timed(n, lambda n=n: self._p.analyzer.analyze(run, transcript, source, prompt=n).text)
                for n in names]

    def audience(self, transcript: Transcript, names: list[str], day: date | None = None) -> list[TrialResult]:
        day = day or date.today()
        marked = self._p.attribute(transcript)

        def one(name: str) -> str:
            return self._p.render(self._p.summarize(marked, name, day), name)
        return [self._timed(n, lambda n=n: one(n)) for n in names]

    def image(self, files: list[str], names: list[str], mode: str = "together") -> list[TrialResult]:
        def one(name: str) -> str:
            results = self._p.image_analyzer.analyze(files, prompt_name=name, mode=mode, save=False)
            return "\n\n".join(r.error and f"ОШИБКА: {r.error}" or r.text for r in results)
        return [self._timed(n, lambda n=n: one(n)) for n in names]

    @staticmethod
    def _timed(name: str, call) -> TrialResult:
        started = time.time()
        try:
            return TrialResult(name, call(), time.time() - started)
        except Exception as exc:     # одна версия промпта с ошибкой не должна ронять сравнение остальных
            return TrialResult(name, "", time.time() - started, f"{type(exc).__name__}: {exc}")

