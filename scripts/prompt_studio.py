"""Работа с промптами из терминала (то же самое доступно в ноутбуке, раздел «Prompt Studio»).

    python scripts/prompt_studio.py list
    python scripts/prompt_studio.py new meeting retro --title "Ретроспектива"
    python scripts/prompt_studio.py new audience investors --from management
    python scripts/prompt_studio.py check                # все промпты
    python scripts/prompt_studio.py check meeting retro
    python scripts/prompt_studio.py preview meeting retro
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)          # пути из настроек (prompts, config) относительные

from audio_assistant.client.prompt_lab import KINDS, PromptLab  # noqa: E402
from audio_assistant.endpoint.assistant_settings import get_assistant_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prompt_studio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="все промпты")
    n = sub.add_parser("new", help="создать промпт из шаблона")
    n.add_argument("kind", choices=KINDS)
    n.add_argument("name")
    n.add_argument("--title", help="для meeting: название типа встречи для людей")
    n.add_argument("--from", dest="copy_from", help="скопировать существующий промпт вместо шаблона")
    c = sub.add_parser("check", help="проверить промпты (без аргументов - все)")
    c.add_argument("kind", nargs="?", choices=KINDS)
    c.add_argument("name", nargs="?")
    p = sub.add_parser("preview", help="показать итоговый текст запроса")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("name")
    args = ap.parse_args(argv)

    settings = get_assistant_settings()
    lab = PromptLab(settings.prompts_dir, settings.kinds_file)
    if args.cmd == "list":
        for row in lab.catalog():
            print(f"{row['вид']:9} {row['имя']:22} {row['изменён']}  {row['символов']:>6} симв.  {row['тип встречи']}")
    elif args.cmd == "new":
        try:
            files = lab.new(args.kind, args.name, args.title, args.copy_from)
        except (ValueError, KeyError, FileExistsError) as exc:
            print("Ошибка:", exc.args[0] if exc.args else exc)
            return 1
        print("Создано:")
        for f in files:
            print("  ", f.relative_to(ROOT))
        print("Отредактируйте файлы (заготовки <...> замените), затем: check", args.kind, args.name)
    elif args.cmd == "check":
        targets = [(args.kind, args.name)] if args.kind and args.name else \
            [(k, nm) for k in KINDS for nm in lab.names(k)]
        bad = False
        for kind, name in targets:
            for issue in lab.check(kind, name):
                bad |= issue.level == "error"
                print(f"{kind}/{name}: {issue}")
        return 1 if bad else 0
    elif args.cmd == "preview":
        print(lab.preview(args.kind, args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
