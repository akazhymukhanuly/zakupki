"""Загрузка справочников заказчика из Excel.

    python manage.py import_refs --template шаблон.xlsx      # получить шаблон для заказчика
    python manage.py import_refs данные.xlsx --dry-run        # проверить без записи
    python manage.py import_refs данные.xlsx                  # загрузить
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.refs_import import build_template, import_workbook


class Command(BaseCommand):
    help = "Загрузить справочники (подразделения, пользователи, номенклатура, поставщики …) из Excel"

    def add_arguments(self, parser):
        parser.add_argument("file", help="Путь к .xlsx")
        parser.add_argument("--template", action="store_true", help="Записать пустой шаблон в file")
        parser.add_argument("--dry-run", action="store_true", help="Только проверить, ничего не записывать")

    def handle(self, file, template=False, dry_run=False, **opts):
        path = Path(file)
        if template:
            path.write_bytes(build_template())
            self.stdout.write(self.style.SUCCESS(f"Шаблон записан: {path}"))
            return
        if not path.exists():
            raise CommandError(f"Файл не найден: {path}")
        report = import_workbook(path.read_bytes(), dry_run=dry_run)
        for sheet, n in report.created.items():
            self.stdout.write(f"{sheet}: добавлено {n}")
        for sheet, n in report.updated.items():
            self.stdout.write(f"{sheet}: обновлено {n}")
        for e in report.errors:
            self.stderr.write(self.style.ERROR(e))
        if report.errors:
            raise CommandError(f"Ошибок: {len(report.errors)}. Ничего не записано — исправьте файл и повторите.")
        if dry_run:
            self.stdout.write(self.style.SUCCESS("Проверка пройдена. Запустите без --dry-run для загрузки."))
            return
        if report.passwords:
            out = path.with_name(path.stem + "_пароли.csv")
            out.write_text("Логин;Временный пароль\n" + "\n".join(f"{u};{p}" for u, p in report.passwords), encoding="utf-8-sig")
            self.stdout.write(self.style.WARNING(f"Временные пароли новых пользователей: {out} — раздайте и удалите файл."))
        self.stdout.write(self.style.SUCCESS("Загружено."))
