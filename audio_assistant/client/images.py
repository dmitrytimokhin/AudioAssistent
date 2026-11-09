"""Независимый разбор картинок: фото + промпт -> текст модели (vision). Со встречами не связан."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from observability.logging.setup import get_logger

from .image_prep import IMAGE_EXTENSIONS, prepare_image
from .ports import LLMClient
from .prompt_runner import model_name_of
from .prompts import FilePromptRepository
from .run_context import llm_context

logger = get_logger(__name__)


def fill_image_prompt(template: str, **values: str) -> str:
    """Подстановка {file}, {count}, {date}, {tags} через replace (в промптах бывают JSON-примеры с { })."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


@dataclass
class ImageResult:
    files: list[str]
    text: str = ""
    error: str | None = None
    analysis_id: int | None = None
    model: str = ""
    duration_ms: float = 0.0
    images: list[dict] = field(default_factory=list)


class ImageAnalyzer:
    def __init__(self, llm: LLMClient, prompts: FilePromptRepository, images_dir: Path, store,
                 max_side: int, max_per_request: int):
        self._llm, self._prompts, self._dir, self._store = llm, prompts, Path(images_dir), store
        self._max_side, self._max_per_request = max_side, max_per_request

    @property
    def persists(self) -> bool:
        return self._store is not None

    # ── файлы ────────────────────────────────────────────────────────────
    def list_files(self) -> list[dict]:
        if not self._dir.is_dir():
            return []
        items = [{"file_name": p.name, "size_bytes": p.stat().st_size,
                  "modified_at": datetime.fromtimestamp(p.stat().st_mtime)}
                 for p in self._dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
        return sorted(items, key=lambda i: i["modified_at"], reverse=True)

    def resolve(self, file_name: str) -> Path:
        if not file_name or Path(file_name).name != file_name:
            raise ValueError(f"Недопустимое имя файла: {file_name!r}")
        path = self._dir / file_name
        if not path.is_file():
            raise FileNotFoundError(f"Нет файла {file_name} в {self._dir}")
        return path

    def save_file(self, name: str, data: bytes, overwrite: bool = False, unique: bool = False) -> str:
        """Кладёт фото в images_dir. Имя занято: overwrite заменяет, unique добавляет суффикс, иначе FileExistsError."""
        name = Path(name or "").name
        if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"Нужна картинка {sorted(IMAGE_EXTENSIONS)}, получено {name!r}")
        prepare_image(data, self._max_side, name)          # битый файл не сохраняем
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / name
        if path.exists() and not overwrite:
            if not unique:
                raise FileExistsError(f"{name} уже есть; overwrite=true заменит файл")
            n = 1
            while (path := self._dir / f"{Path(name).stem}_{n}{Path(name).suffix}").exists():
                n += 1
        path.write_bytes(data)
        return path.name

    # ── разбор ───────────────────────────────────────────────────────────
    def analyze(self, files: list[str], *, prompt_name: str | None = None, prompt: str | None = None,
                system: str | None = None, tags: list[str] | None = None, mode: str = "together",
                json_schema: dict | None = None, save: bool = True) -> list[ImageResult]:
        """mode=together: все картинки в одном запросе (одна страница, несколько фото одной доски);
        mode=each: отдельный запрос и отдельный разбор на каждую (пачка фото, один промпт)."""
        if not files:
            raise ValueError("Не указано ни одного файла")
        if mode not in ("together", "each"):
            raise ValueError(f"mode = together | each, получено {mode!r}")
        if mode == "together" and len(files) > self._max_per_request:
            raise ValueError(f"В одном запросе не больше {self._max_per_request} картинок "
                             f"(ASSISTANT_IMAGE_MAX_PER_REQUEST); для пачки используйте mode=each")
        if bool(prompt_name) == bool(prompt and prompt.strip()):
            raise ValueError("Укажите ровно одно: prompt_name (папка prompts/images/) или prompt (свой текст)")
        if prompt_name:
            profile = self._prompts.image_prompt(prompt_name)       # ошибка промпта -- до дорогих вызовов
            system_text, template = profile.system, profile.user_template
        else:
            system_text, template = system or "", prompt
        paths = [self.resolve(f) for f in files]
        tags = tags or []

        groups = [paths] if mode == "together" else [[p] for p in paths]
        results = []
        for group in groups:
            results.append(self._analyze_group(group, prompt_name, system_text, template, tags,
                                               json_schema, save and self.persists, mode == "each"))
        return results

    def _analyze_group(self, paths: list[Path], prompt_name: str | None, system: str, template: str,
                       tags: list[str], json_schema: dict | None, save: bool, isolate: bool) -> ImageResult:
        names = [p.name for p in paths]
        result = ImageResult(files=names)
        try:
            prepared = [prepare_image(p.read_bytes(), self._max_side, p.name) for p in paths]
            result.images = [{"file": n, "width": i.width, "height": i.height} for n, i in zip(names, prepared)]
            user = fill_image_prompt(template, file=", ".join(names), count=str(len(names)),
                                     date=date.today().isoformat(), tags=", ".join(tags))
            started = time.perf_counter()
            with llm_context(None, f"image:{prompt_name or 'custom'}"):
                result.text = (self._llm.generate(system, user, images=[i.data for i in prepared])
                               if json_schema is None else
                               self._llm.generate(system, user, json_schema=json_schema,
                                                  images=[i.data for i in prepared]))
            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            result.model = model_name_of(self._llm)
            if save:
                result.analysis_id = self._store.save(
                    images=result.images, prompt_name=prompt_name, system_prompt=system, prompt=user,
                    model=result.model, tags=tags, text=result.text, duration_ms=result.duration_ms)
        except Exception as exc:
            if not isolate:           # одна группа: ошибку видит вызывающий; в пачке -- не роняем остальные
                raise
            result.error = f"{type(exc).__name__}: {exc}"
            logger.warning("Image analysis failed", files=names, error=result.error)
        return result
