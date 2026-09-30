"""Полный пересчёт количеств и статусов всех позиций и СЗ.

Нужен после ручных правок в админке или после обновления со старой версии:
    python manage.py recalc_all
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Memo, MemoItem
from core.services import recalc_item, recalc_memo


class Command(BaseCommand):
    help = "Пересчитать количества и статусы позиций и СЗ"

    def handle(self, *args, **opts):
        n = 0
        for item in MemoItem.objects.select_related("memo").iterator(chunk_size=500):
            with transaction.atomic():
                recalc_item(item, refresh_memo=False)
            n += 1
        for memo in Memo.objects.iterator(chunk_size=500):
            recalc_memo(memo)
        self.stdout.write(self.style.SUCCESS(f"Пересчитано позиций: {n}"))
