"""Фоновый планировщик (отдельный процесс/контейнер): периодические задачи и обмен с 1С.

    python manage.py scheduler

Ошибка одной задачи логируется и не останавливает планировщик.
"""
import logging
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections

log = logging.getLogger("core.scheduler")

JOBS = [
    # (имя, интервал в секундах, функция)
    ("run_periodic", 60 * 60, lambda: call_command("run_periodic")),
    ("onec_exchange", 10 * 60, lambda: call_command("onec_exchange")),
    ("clearsessions", 24 * 60 * 60, lambda: call_command("clearsessions")),
]


class Command(BaseCommand):
    help = "Периодические задачи: автозакрытие, SLA-напоминания, обмен с 1С"

    def handle(self, *args, **opts):
        last = {}
        log.info("Планировщик запущен")
        while True:
            now = time.monotonic()
            for name, every, job in JOBS:
                if name == "onec_exchange" and not settings.PROCUREMENT["ONEC_EXCHANGE_DIR"]:
                    continue
                if now - last.get(name, -every) >= every:
                    last[name] = now
                    close_old_connections()
                    try:
                        job()
                    except Exception:  # noqa: BLE001
                        log.exception("Задача %s завершилась с ошибкой", name)
            time.sleep(30)
