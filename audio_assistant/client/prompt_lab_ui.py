"""Интерактивная форма промпт-лаборатории для Jupyter (ipywidgets): создать, править, проверить, сравнить, удалить.

    ui = PromptStudioUI(lab, trial=trial, sample=lambda: SAMPLE, run=lambda: SAMPLE_RUN)
    ui.show()

Вся логика в PromptLab/PromptTrial; здесь только кнопки. Методы on_* можно вызвать и из кода.
"""
from __future__ import annotations

import html
from typing import Callable

import ipywidgets as w
from IPython.display import HTML, display

from .prompt_lab import KINDS, PromptLab, PromptTrial

_KIND_TITLES = {"audience": "audience - статус для аудитории", "meeting": "meeting - разбор встречи",
                "digest": "digest - сводка в Excel", "image": "image - разбор фото"}
_TEMPLATE = "(шаблон)"
_MONO = "font-family:monospace;font-size:13px"


class PromptStudioUI:
    def __init__(self, lab: PromptLab, trial: PromptTrial | None = None,
                 sample: Callable | None = None, run: Callable | None = None, day: Callable | None = None):
        self.lab, self.trial, self._sample, self._run, self._day = lab, trial, sample, run, day
        self.log = w.Output(layout=w.Layout(border="1px solid #8884", padding="6px", min_height="40px"))
        self._editors: dict[str, w.Textarea] = {}

        self.kind = w.Dropdown(options=[(_KIND_TITLES[k], k) for k in KINDS], description="Вид:")
        self.name = w.Dropdown(description="Промпт:", layout=w.Layout(width="360px"))
        self.tabs = w.Tab()
        self.versions = w.Dropdown(description="Версия:", layout=w.Layout(width="360px"))
        self.trial_names = w.Text(description="Сравнить:", placeholder="имена через запятую; пусто = выбранный",
                                  layout=w.Layout(width="520px"))

        self.save_btn = self._button("Сохранить", "success", self.on_save)
        self.check_btn = self._button("Проверить", "", self.on_check)
        self.preview_btn = self._button("Итоговый текст", "", self.on_preview)
        self.trial_btn = self._button("Пробный запуск", "info", self.on_trial)
        self.restore_btn = self._button("Откатить файл к версии", "warning", self.on_restore)

        self.del_confirm = w.Checkbox(description="да, удалить (можно вернуть из корзины)", indent=False)
        self.del_kind = w.Checkbox(description="и убрать тип встречи из config", indent=False)
        self.del_btn = self._button("Удалить", "danger", self.on_delete)

        self.new_name = w.Text(description="Имя:", placeholder="латиницей: weekly_review")
        self.new_title = w.Text(description="Название:", placeholder="для типа встречи, по-русски")
        self.new_from = w.Dropdown(description="Основа:")
        self.create_btn = self._button("Создать", "primary", self.on_create)

        self.trash = w.Dropdown(description="Корзина:", layout=w.Layout(width="520px"))
        self.undelete_btn = self._button("Вернуть", "", self.on_undelete)

        self.kind.observe(lambda _: self._refresh_names(), "value")
        self.name.observe(lambda _: self._load(), "value")
        self.tabs.observe(lambda _: self._refresh_versions(), "selected_index")
        self._refresh_names()
        self._refresh_trash()

    # ── вёрстка ─────────────────────────────────────────────────────────
    @staticmethod
    def _button(text: str, style: str, handler) -> w.Button:
        btn = w.Button(description=text, button_style=style, layout=w.Layout(width="auto"))
        btn.on_click(lambda _: handler())
        return btn

    def show(self) -> None:
        head = w.HBox([self.kind, self.name])
        actions = w.HBox([self.save_btn, self.check_btn, self.preview_btn])
        history = w.HBox([self.versions, self.restore_btn])
        trial = w.HBox([self.trial_names, self.trial_btn])
        delete = w.HBox([self.del_confirm, self.del_kind, self.del_btn])
        create = w.VBox([w.HTML("<b>Создать новый</b> (вид берётся сверху)"),
                         w.HBox([self.new_name, self.new_title]), w.HBox([self.new_from, self.create_btn])])
        trash = w.HBox([self.trash, self.undelete_btn])
        display(w.VBox([head, self.tabs, actions, history, trial,
                        w.HTML("<b>Удаление</b>"), delete, trash, create, self.log]))

    # ── данные ──────────────────────────────────────────────────────────
    def _refresh_names(self, select: str | None = None) -> None:
        names = self.lab.names(self.kind.value)
        self.name.options = names
        self.name.value = select if select in names else (names[0] if names else None)
        self.new_from.options = [_TEMPLATE, *names]
        self.new_title.disabled = self.kind.value != "meeting"
        self.del_kind.disabled = self.kind.value != "meeting"
        self._load()

    def _load(self) -> None:
        kind, name = self.kind.value, self.name.value
        self._editors = {}
        if not name:
            self.tabs.children, self.versions.options = [], []
            return
        files = self.lab.read(kind, name)
        for file_name, text in files.items():
            self._editors[file_name] = w.Textarea(
                value=text, layout=w.Layout(width="98%", height="320px"), continuous_update=False)
            self._editors[file_name].add_class("prompt-editor")
        self.tabs.children = list(self._editors.values())
        for i, file_name in enumerate(self._editors):
            self.tabs.set_title(i, file_name)
        self._refresh_versions()

    def _current_file(self) -> str:
        return list(self._editors)[self.tabs.selected_index or 0]

    def _refresh_versions(self) -> None:
        if self._editors:
            options = self.lab.history(self.kind.value, self.name.value, self._current_file())
            self.versions.options = options
            self.versions.value = options[0] if options else None

    def _refresh_trash(self) -> None:
        self.trash.options = self.lab.trash() or ["(пусто)"]

    def _say(self, text: str = "", *, error: bool = False, raw_html: str | None = None) -> None:
        self.log.clear_output()
        with self.log:
            if raw_html is not None:
                display(HTML(raw_html))
            else:
                color = "#c0392b" if error else "inherit"
                display(HTML(f"<pre style='{_MONO};color:{color};white-space:pre-wrap'>{html.escape(text)}</pre>"))

    # ── действия ────────────────────────────────────────────────────────
    def on_save(self) -> None:
        """Сохраняет все вкладки; изменившиеся файлы получают копию прежней версии в истории."""
        changed = []
        for file_name, editor in self._editors.items():
            current = self.lab.read(self.kind.value, self.name.value)[file_name]
            if editor.value != current:
                self.lab.save(self.kind.value, self.name.value, file_name, editor.value)
                changed.append(file_name)
        self._refresh_versions()
        if not changed:
            self._say("Изменений нет")
            return
        self.on_check(saved=changed)

    def on_check(self, saved: list[str] | None = None) -> None:
        lines = [str(i) for i in self.lab.check(self.kind.value, self.name.value)]
        head = f"Сохранено: {', '.join(saved)}\n\n" if saved else ""
        self._say(head + "Проверка:\n" + "\n".join(lines), error=any(l.startswith("[error]") for l in lines))

    def on_preview(self) -> None:
        self._say(self.lab.preview(self.kind.value, self.name.value))

    def on_trial(self) -> None:
        if self.trial is None or self._sample is None:
            self._say("Пробный запуск не подключён: передайте trial= и sample= в PromptStudioUI", error=True)
            return
        names = [n.strip() for n in self.trial_names.value.split(",") if n.strip()] or [self.name.value]
        kind, sample = self.kind.value, self._sample()
        self._say("Идёт запуск модели, подождите...")
        try:
            if kind == "meeting":
                results = self.trial.meeting(sample, names, run=self._run() if self._run else None)
            elif kind == "audience":
                results = self.trial.audience(sample, names, day=self._day() if self._day else None)
            else:
                self._say("Пробный запуск есть для meeting и audience. Для image: trial.image([...], [...]) "
                          "в ячейке C4; для digest - через ручку сводки.", error=True)
                return
        except Exception as exc:
            self._say(f"{type(exc).__name__}: {exc}", error=True)
            return
        cells = "".join(
            "<td style='vertical-align:top;padding:8px;border:1px solid #8884'>"
            f"<b>{html.escape(r.name)}</b> <small>{r.seconds:.1f} с</small><hr>"
            f"<pre style='white-space:pre-wrap'>{html.escape('ОШИБКА: ' + r.error if r.error else r.text)}</pre></td>"
            for r in results)
        self._say(raw_html=f"<table style='width:100%'><tr>{cells}</tr></table>")

    def on_restore(self) -> None:
        if not self.versions.value:
            self._say("У этого файла нет сохранённых версий", error=True)
            return
        file_name = self._current_file()
        self.lab.restore(self.kind.value, self.name.value, file_name, self.versions.value)
        self._load()
        self._say(f"{file_name} возвращён к версии {self.versions.value} (текущий текст тоже сохранён в историю)")

    def on_delete(self) -> None:
        kind, name = self.kind.value, self.name.value
        try:
            plan = self.lab.delete(kind, name, confirm=self.del_confirm.value, unregister_kind=self.del_kind.value)
        except (ValueError, KeyError) as exc:
            self._say(str(exc.args[0] if exc.args else exc), error=True)
            return
        if not plan["выполнено"]:
            note = plan.get("предупреждение") or "Отметьте «да, удалить» и нажмите ещё раз."
            self._say(f"План удаления {kind}/{name}: файлы {plan['файлы']}, типы встреч {plan['типы встреч']}\n{note}")
            return
        self.del_confirm.value = self.del_kind.value = False
        self._refresh_names()
        self._refresh_trash()
        self._say(f"Удалено в корзину: {plan['корзина']}")

    def on_undelete(self) -> None:
        if not self.trash.value or self.trash.value == "(пусто)":
            self._say("Корзина пуста", error=True)
            return
        try:
            path = self.lab.undelete(self.trash.value)
        except (KeyError, FileExistsError) as exc:
            self._say(str(exc.args[0] if exc.args else exc), error=True)
            return
        kind = self.trash.value.split("__", 1)[0]
        self.kind.value = kind
        self._refresh_names(select=path.name)
        self._refresh_trash()
        self._say(f"Возвращено: {path}")

    def on_create(self) -> None:
        kind, name = self.kind.value, self.new_name.value.strip()
        copy_from = None if self.new_from.value in (None, _TEMPLATE) else self.new_from.value
        try:
            self.lab.new(kind, name, title=self.new_title.value.strip() or None, copy_from=copy_from)
        except (ValueError, KeyError, FileExistsError) as exc:
            self._say(str(exc.args[0] if exc.args else exc), error=True)
            return
        self._refresh_names(select=name)
        self.new_name.value = self.new_title.value = ""
        self._say(f"Создано {kind}/{name}. Замените заготовки <...> и нажмите «Сохранить».")
