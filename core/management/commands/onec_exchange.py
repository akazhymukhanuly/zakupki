"""Автоматический обмен с 1С через папку (запускать по расписанию, напр. каждые 10 минут).

Структура папки ONEC_EXCHANGE_DIR:
  out/      — система выгружает подписанные, ещё не выгруженные договоры (contracts_ГГГГММДД_ЧЧММ.xml)
  in/       — 1С кладёт файлы: receipts_*.xml|csv|xlsx|json (поступления), payments_*.* (оплаты)
  archive/  — успешно обработанные входящие файлы
  error/    — файлы с ошибками + .log с описанием

    python manage.py onec_exchange [--dir /path]
"""
import io
import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core import onec
from core.models import Contract

log = logging.getLogger("core.onec")


class Command(BaseCommand):
    help = "Обмен с 1С через папку: выгрузка договоров и загрузка поступлений/оплат"

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=None)

    def handle(self, *args, dir=None, **opts):
        base = Path(dir or settings.PROCUREMENT["ONEC_EXCHANGE_DIR"] or "")
        if not str(base):
            raise CommandError("Не задана папка обмена: ONEC_EXCHANGE_DIR или --dir")
        for sub in ("in", "out", "archive", "error"):
            (base / sub).mkdir(parents=True, exist_ok=True)

        # 1. Выгрузка договоров
        contracts = list(Contract.objects.filter(
            exported_1c_at__isnull=True,
            status__in=[Contract.Status.SIGNED, Contract.Status.EXECUTION],
        ).exclude(kind=Contract.Kind.FRAMEWORK))
        if contracts:
            name = f"contracts_{timezone.localtime():%Y%m%d_%H%M%S}.xml"
            tmp = base / "out" / (name + ".tmp")
            tmp.write_bytes(onec.export_xml(contracts))
            tmp.rename(base / "out" / name)  # атомарно: 1С не увидит недописанный файл
            onec.mark_exported(contracts)
            self.stdout.write(f"Выгружено договоров: {len(contracts)} → {name}")

        # 2. Загрузка входящих
        for path in sorted((base / "in").iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() == ".tmp":
                continue
            low = path.name.lower()
            kind = "receipts" if low.startswith(("receipt", "поступ")) else "payments" if low.startswith(("payment", "оплат")) else None
            if not kind:
                continue
            upload = SimpleUploadedFile(path.name, path.read_bytes())
            try:
                with transaction.atomic():
                    rep = (onec.import_receipts if kind == "receipts" else onec.import_payments)(upload, None)
            except Exception as e:  # noqa: BLE001 — любой сбой файла не должен останавливать обмен
                rep = {"errors": [f"{type(e).__name__}: {e}"]}
                log.exception("1С: ошибка файла %s", path.name)
            stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
            if rep.get("errors"):
                dest = base / "error" / f"{stamp}_{path.name}"
                shutil.move(str(path), dest)
                dest.with_suffix(dest.suffix + ".log").write_text("\n".join(rep["errors"]), encoding="utf-8")
                self.stderr.write(f"{path.name}: ошибок {len(rep['errors'])} → error/")
            else:
                shutil.move(str(path), base / "archive" / f"{stamp}_{path.name}")
                self.stdout.write(f"{path.name}: обработан → archive/ {rep}")
