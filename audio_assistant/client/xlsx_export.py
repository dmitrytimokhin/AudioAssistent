"""Выгрузка сводки в Excel: лист с данными и лист со списком встреч."""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_MAX_WIDTH, _MIN_WIDTH = 60, 10


def _fill_sheet(sheet, headers: list[str], rows: list[list]) -> None:
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for index, header in enumerate(headers, 1):
        longest = max([len(str(header))] + [len(str(r[index - 1]).split("\n")[0]) for r in rows if len(r) >= index])
        sheet.column_dimensions[get_column_letter(index)].width = max(_MIN_WIDTH, min(_MAX_WIDTH, longest + 2))
    sheet.freeze_panes = "A2"
    if rows:
        sheet.auto_filter.ref = sheet.dimensions


def write_digest_xlsx(path: Path, headers: list[str], rows: list[dict], meetings: list[dict]) -> Path:
    """rows -- словари {заголовок: значение}; meetings -- итог по каждой встрече (какая обработана, какая нет)."""
    workbook = Workbook()
    data = workbook.active
    data.title = "Данные"
    _fill_sheet(data, headers, [[row.get(h, "") for h in headers] for row in rows])

    meetings_sheet = workbook.create_sheet("Встречи")
    _fill_sheet(meetings_sheet, ["Запуск", "Дата", "Файл", "Тип встречи", "Результат", "Строк", "Комментарий"],
                [[m["run_id"], m["meeting_date"], m["source"], m.get("kind") or "", m["status"],
                  m.get("rows", 0), m.get("note", "")] for m in meetings])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path
