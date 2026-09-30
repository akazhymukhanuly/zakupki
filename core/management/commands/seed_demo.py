"""Демо-данные для показа MVP: python manage.py seed_demo [--reset]

Все сценарии прогоняются через бизнес-логику (services), поэтому статусы согласованы.
Пароль у всех демо-пользователей: demo12345.
"""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core import roles, services
from core.models import (
    ApprovalRule, BudgetItem, Category, Contract, ContractLine, Department, Memo, MemoItem, MemoTemplate,
    MemoTemplateLine, Nomenclature, Procurement, Profile, ReceiptLine, Supplier,
)

PASSWORD = "demo12345"


class Command(BaseCommand):
    help = "Заполнить базу демо-данными"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Очистить базу перед заполнением")

    def handle(self, *args, **opts):
        from django.conf import settings
        if not settings.DEMO_MODE:
            raise CommandError("Демо-данные заливаются только при DEMO_MODE=1 (не на боевом сервере).")
        if opts["reset"]:
            call_command("flush", "--noinput")
        if User.objects.filter(username="buyer").exists():
            self.stdout.write(self.style.WARNING("Демо-данные уже есть. Используйте --reset для пересоздания."))
            return
        with transaction.atomic():
            self.seed()
        self.stdout.write(self.style.SUCCESS("Готово. Логины: ivanov, petrova, sidorov, head, buyer, director, cfo, "
                                             f"accountant, admin. Пароль: {PASSWORD}"))

    # ------------------------------------------------------------------
    def user(self, username, first, last, role_names, dept=None, staff=False, superuser=False, position=""):
        u = User.objects.create_user(username, f"{username}@example.kz", PASSWORD, first_name=first, last_name=last,
                                     is_staff=staff or superuser, is_superuser=superuser)
        for r in role_names:
            u.groups.add(Group.objects.get(name=r))
        Profile.objects.create(user=u, department=dept, position=position)
        return u

    def memo(self, initiator, dept, justification, required, items, submit=True):
        m = Memo.objects.create(department=dept, initiator=initiator, justification=justification, required_date=required)
        services.log("СЗ создана", initiator, memo=m)
        for n, (nom, qty, extra) in enumerate(items, 1):
            MemoItem.objects.create(
                memo=m, line_no=n, nomenclature=nom, description=extra.get("description", nom.name if nom else ""),
                quantity=D(qty), unit=nom.unit if nom else extra.get("unit", "шт"),
                required_date=extra.get("required", required), category=nom.category if nom else extra.get("category"),
                budget_item=extra.get("budget"), urgent=extra.get("urgent", False),
                estimated_price=extra.get("price"),
            )
        if submit:
            services.submit_memo(m, initiator)
        return m

    def quote(self, proc, supplier, buyer, prices):
        """prices: {nomenclature: (price|None, lead, extra)}; None — не предлагает."""
        from core.models import RFQ
        rfq = RFQ.objects.get(procurement=proc, supplier=supplier)
        rows = {}
        for line in proc.lines.filter(state="active").select_related("item"):
            if line.item.nomenclature not in prices:
                continue
            price, lead, extra = prices[line.item.nomenclature]
            rows[line.pk] = {"price": None if price is None else D(price), "lead_time_days": lead,
                             "not_offered": price is None, "offered_qty": extra.get("offered"),
                             "is_analog": bool(extra.get("analog")), "analog_description": extra.get("analog", ""),
                             "comment": extra.get("comment", "")}
        return services.save_quote(rfq, {"payment_terms": "100% по факту поставки", "valid_until": timezone.localdate() + timedelta(days=30)},
                                   rows, buyer)

    # ------------------------------------------------------------------
    def seed(self):
        today = timezone.localdate()
        for r in roles.ALL_ROLES:
            Group.objects.get_or_create(name=r)

        it = Department.objects.create(name="IT-отдел")
        acc = Department.objects.create(name="Бухгалтерия")
        wh = Department.objects.create(name="Склад и АХО")

        head = self.user("head", "Айгуль", "Сапарова", [roles.APPROVER], it, position="Руководитель направления")
        for d in (it, acc, wh):
            d.head = head
            d.save()
        ivanov = self.user("ivanov", "Иван", "Иванов", [roles.INITIATOR], it, position="Системный администратор")
        petrova = self.user("petrova", "Мария", "Петрова", [roles.INITIATOR], acc, position="Главный бухгалтер")
        sidorov = self.user("sidorov", "Ерлан", "Сидоров", [roles.INITIATOR], wh, position="Завхоз")
        buyer = self.user("buyer", "Данияр", "Касымов", [roles.BUYER, roles.INITIATOR], staff=True, position="Менеджер по закупкам")
        director = self.user("director", "Алия", "Нурланова", [roles.DIRECTOR], position="Генеральный директор")
        self.user("cfo", "Сергей", "Ким", [roles.CFO], position="Финансовый директор")
        self.user("accountant", "Гульнара", "Ахметова", [roles.ACCOUNTANT], acc, position="Бухгалтер")
        self.user("admin", "Админ", "Системы", [roles.ADMIN], superuser=True)

        ApprovalRule.objects.create(min_amount=D("1000000"), role=roles.DIRECTOR)
        ApprovalRule.objects.create(min_amount=D("5000000"), role=roles.CFO)

        b_office = BudgetItem.objects.create(code="01.01", name="Канцелярские расходы")
        b_it = BudgetItem.objects.create(code="02.03", name="IT-оборудование")
        b_house = BudgetItem.objects.create(code="03.02", name="Хозяйственные нужды")
        b_furn = BudgetItem.objects.create(code="04.01", name="Мебель")

        c_office = Category.objects.create(name="Канцтовары")
        c_print = Category.objects.create(name="Оргтехника и расходники")
        c_it = Category.objects.create(name="IT-оборудование")
        c_house = Category.objects.create(name="Хозтовары")
        c_furn = Category.objects.create(name="Мебель")

        def nom(name, unit, cat, code):
            return Nomenclature.objects.create(name=name, unit=unit, category=cat, code_1c=code)
        paper = nom("Бумага А4 SvetoCopy, 500 л.", "пач", c_office, "00-0001")
        pens = nom("Ручка шариковая синяя (уп. 50 шт)", "уп", c_office, "00-0002")
        folders = nom("Папка-регистратор 75 мм", "шт", c_office, "00-0003")
        cartridge = nom("Картридж HP 85A (CE285A)", "шт", c_print, "00-0101")
        laptop = nom("Ноутбук Lenovo ThinkPad E14 (i5/16/512)", "шт", c_it, "00-0201")
        monitor = nom("Монитор 24\" Dell P2422H", "шт", c_it, "00-0202")
        mouse = nom("Мышь беспроводная Logitech M185", "шт", c_it, "00-0203")
        keyboard = nom("Клавиатура Logitech K120", "шт", c_it, "00-0204")
        detergent = nom("Средство моющее универсальное 5 л", "шт", c_house, "00-0301")
        chair = nom("Кресло офисное", "шт", c_furn, "00-0401")
        calc = nom("Калькулятор настольный Citizen", "шт", c_office, "00-0004")

        def sup(name, bin_, email, cats, contact):
            s = Supplier.objects.create(name=name, bin=bin_, email=email, phone="+7 727 000 00 00", contact=contact)
            s.categories.set(cats)
            return s
        kanc = sup("ТОО «Канцлер»", "180540012345", "sales@kancler.example.kz", [c_office], "Асель")
        officemag = sup("ТОО «OfficeMag KZ»", "150840098765", "b2b@officemag.example.kz", [c_office, c_print, c_house], "Руслан")
        techno = sup("ТОО «TechnoDom B2B»", "090340011122", "corp@technodom.example.kz", [c_it, c_print], "Олжас")
        sup("ТОО «Мебель Плюс»", "120240033344", "info@mebelplus.example.kz", [c_furn], "Марат")
        chisto = sup("ТОО «ЧистоДом»", "170640055566", "order@chistodom.example.kz", [c_house], "Дина")

        tpl = MemoTemplate.objects.create(name="Канцелярия на квартал", department=acc)
        MemoTemplateLine.objects.create(template=tpl, nomenclature=paper, quantity=20)
        MemoTemplateLine.objects.create(template=tpl, nomenclature=pens, quantity=2)
        MemoTemplateLine.objects.create(template=tpl, nomenclature=folders, quantity=10)

        # Рамочный договор с OfficeMag — к нему будет предложена спецификация.
        Contract.objects.create(number="РД-2026-001", kind=Contract.Kind.FRAMEWORK, supplier=officemag,
                                status=Contract.Status.SIGNED, date=date(today.year, 1, 15),
                                valid_until=date(today.year, 12, 31), responsible=buyer)

        # ---------- История прошлого года (для отчёта «Экономия») ----------
        old = self.memo(petrova, acc, "Канцелярия на IV квартал прошлого года", date(today.year - 1, 11, 1),
                        [(paper, 80, {"budget": b_office})])
        services.approve_memo(old, head)
        p_old = services.create_procurement(buyer, list(old.items.all()), "Бумага (прошлый год)", buyer=buyer)
        services.add_rfqs(p_old, [kanc], buyer)
        services.mark_rfqs_sent(p_old, buyer)
        self.quote(p_old, kanc, buyer, {paper: (2650, 3, {})})
        services.decision_preset(p_old, "best", buyer)
        services.submit_decision(p_old, buyer)
        (c_old,) = services.create_contracts(p_old, buyer, {})
        services.set_contract_status(c_old, Contract.Status.SIGNING, buyer)
        services.set_contract_status(c_old, Contract.Status.SIGNED, buyer)
        services.register_receipt(c_old, date(today.year - 1, 10, 20), "ПТУ-0931",
                                  [(c_old.lines.first(), D(80), ReceiptLine.Match.ID, True, "", "")], buyer)
        services.register_payment(c_old, date(today.year - 1, 10, 25), c_old.amount, "ПП-1204", buyer)
        services.close_item(old.items.first(), petrova)
        services.close_procurement(p_old, buyer)
        back = timezone.now().replace(year=today.year - 1, month=10, day=1)
        Memo.objects.filter(pk=old.pk).update(created_at=back, submitted_at=back, approved_at=back)
        Contract.objects.filter(pk=c_old.pk).update(date=date(today.year - 1, 10, 10))
        Procurement.objects.filter(pk=p_old.pk).update(created_at=back)

        # ---------- Текущие СЗ ----------
        m1 = self.memo(ivanov, it, "Обновление рабочих мест новых сотрудников IT-отдела и расходники для принтеров",
                       today + timedelta(days=21), [
                           (paper, 50, {"budget": b_office}),
                           (cartridge, 5, {"budget": b_office, "urgent": True, "required": today + timedelta(days=7)}),
                           (laptop, 3, {"budget": b_it, "price": D("500000")}),
                           (monitor, 3, {"budget": b_it}),
                       ])
        services.approve_memo(m1, head, comment="Согласовано в рамках бюджета IT")

        m2 = self.memo(petrova, acc, "Канцелярия и хозтовары для бухгалтерии на IV квартал", today + timedelta(days=14), [
            (paper, 50, {"budget": b_office}),
            (pens, 10, {"budget": b_office}),
            (folders, 20, {"budget": b_office}),
            (detergent, 10, {"budget": b_house}),
        ])
        services.approve_memo(m2, head)

        m3 = self.memo(sidorov, wh, "Хозяйственные нужды склада и бумага для накладных", today + timedelta(days=10), [
            (detergent, 30, {"budget": b_house}),
            (chair, 4, {"budget": b_furn}),
            (paper, 20, {"budget": b_office, "urgent": True, "required": today - timedelta(days=2)}),
        ])
        services.approve_memo(m3, head, rejected_items={m3.items.get(line_no=2).pk: "Нет лимита по статье «Мебель» в этом квартале"})

        m7 = self.memo(ivanov, it, "Бумага и картриджи для принтеров 2-го этажа", today + timedelta(days=18), [
            (paper, 30, {"budget": b_office}), (cartridge, 2, {"budget": b_office}),
        ])
        services.approve_memo(m7, head)
        self.memo(ivanov, it, "Периферия для переговорной", today + timedelta(days=30), [
            (mouse, 4, {"budget": b_it}), (keyboard, 4, {"budget": b_it}),
        ], submit=False)
        self.memo(petrova, acc, "Калькуляторы для новых сотрудников", today + timedelta(days=20), [
            (calc, 3, {"budget": b_office}),
        ])

        # ---------- Закупка A: канцелярия и расходники (полный цикл) ----------
        office_items = [i for i in MemoItem.objects.filter(memo__in=[m1, m2], nomenclature__in=[paper, pens, folders, cartridge])]
        pa = services.create_procurement(buyer, office_items, "Канцтовары и картриджи, IV кв.", kp_deadline=today - timedelta(days=3), buyer=buyer)
        services.add_rfqs(pa, [kanc, officemag, techno], buyer)
        services.mark_rfqs_sent(pa, buyer)
        self.quote(pa, kanc, buyer, {paper: (2400, 2, {}), pens: (1500, 2, {}), folders: (900, 2, {}), cartridge: (None, None, {})})
        self.quote(pa, officemag, buyer, {
            paper: (2300, 3, {"comment": "цена на 100 пачек"}), pens: (1600, 3, {}),
            folders: (850, 3, {"analog": "Папка-регистратор Esselte 75 мм (аналог)"}),
            cartridge: (38000, 5, {}),
        })
        self.quote(pa, techno, buyer, {paper: (None, None, {}), cartridge: (36500, 1, {"offered": D(3), "comment": "в наличии 3 шт"})})
        services.decision_preset(pa, "best", buyer)
        for d in pa.decision_lines.filter(analog_state="pending"):
            services.confirm_analog(d, petrova, True)
        services.submit_decision(pa, buyer)
        contracts = services.create_contracts(pa, buyer, {officemag.pk: {"mode": "spec"}})
        by_sup = {c.supplier_id: c for c in contracts}
        # Канцлер — подписан и поставил ручки полностью.
        ck = by_sup[kanc.pk]
        for st in (Contract.Status.SIGNING, Contract.Status.SIGNED):
            services.set_contract_status(ck, st, buyer)
        services.register_receipt(ck, today - timedelta(days=1), "ПТУ-0154",
                                  [(l, l.quantity, ReceiptLine.Match.ID, True, l.description, l.item.code) for l in ck.lines.all()], buyer)
        services.register_payment(ck, today, ck.amount, "ПП-0877", buyer)
        # OfficeMag (спецификация к рамочному) — подписана, бумага поставлена частично, аванс 50%.
        co = by_sup[officemag.pk]
        for st in (Contract.Status.SIGNING, Contract.Status.SIGNED):
            services.set_contract_status(co, st, buyer)
        paper_line = co.lines.filter(item__memo=m1, item__nomenclature=paper).first()
        services.register_receipt(co, today, "ПТУ-0160", [(paper_line, D(50), ReceiptLine.Match.ID, True, paper.name, paper_line.item.code)], buyer)
        services.register_payment(co, today - timedelta(days=2), (co.amount / 2).quantize(D("0.01")), "ПП-0870", buyer)
        # TechnoDom — на подписании.
        services.set_contract_status(by_sup[techno.pk], Contract.Status.SIGNING, buyer)

        # ---------- Закупка B: ноутбуки и мониторы — решение на согласовании у директора ----------
        it_items = list(MemoItem.objects.filter(memo=m1, nomenclature__in=[laptop, monitor]))
        pb = services.create_procurement(buyer, it_items, "Ноутбуки и мониторы для IT", kp_deadline=today + timedelta(days=2), buyer=buyer)
        services.add_rfqs(pb, [techno, officemag], buyer)
        services.mark_rfqs_sent(pb, buyer)
        self.quote(pb, techno, buyer, {laptop: (489000, 5, {}), monitor: (98000, 5, {})})
        self.quote(pb, officemag, buyer, {laptop: (515000, 10, {}), monitor: (94500, 7, {})})
        services.decision_preset(pb, "best", buyer)
        services.submit_decision(pb, buyer)

        # ---------- Закупка C: хозтовары — идёт сбор КП ----------
        house = list(MemoItem.objects.filter(memo=m3, nomenclature=detergent))
        pc = services.create_procurement(buyer, house, "Хозтовары для склада", kp_deadline=today + timedelta(days=5), buyer=buyer)
        services.add_rfqs(pc, [chisto, officemag], buyer)
        services.mark_rfqs_sent(pc, buyer)
        self.quote(pc, chisto, buyer, {detergent: (4200, 2, {})})

        # Состарим пребывание в пуле, чтобы дэшборд было что показать.
        MemoItem.objects.filter(memo=m3, nomenclature=paper).update(pool_since=timezone.now() - timedelta(days=8),
                                                                    status_changed_at=timezone.now() - timedelta(days=8))
        MemoItem.objects.filter(memo=m2, nomenclature=detergent).update(pool_since=timezone.now() - timedelta(days=6))
        services.run_periodic()
