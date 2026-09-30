"""Периодические задачи: автозакрытие позиций и SLA-напоминания.

Запускать по расписанию (cron, раз в час/сутки):
    python manage.py run_periodic
"""
from django.core.management.base import BaseCommand

from core.services import run_periodic


class Command(BaseCommand):
    help = "Автозакрытие поставленных позиций и SLA-напоминания по срочным"

    def handle(self, *args, **opts):
        res = run_periodic()
        self.stdout.write(f"Автозакрыто: {res['auto_closed']}, напоминаний: {res['reminders']}")
