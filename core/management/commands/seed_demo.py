"""Синтетические демо-данные ГРЭС: python manage.py seed_demo [--reset]

Сценарии повторяют макет ГРЭС (коридоры, вопросы, эскалация, транши) и добавляют то, что требует ТЗ
(пул и консолидация, сравнение КП, договор по рамочному). Всё проходит через бизнес-логику (services).
Пароль у всех демо-пользователей: demo12345.
"""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import Group, User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core import roles, services
from core.models import (
    Attachment, BudgetItem, Category, Contract, DecisionApproval, Department, HistoryEntry, Memo, MemoItem,
    MemoTemplate, MemoTemplateLine, Nomenclature, Procurement, ProcurementLine, Profile, Question, ReceiptLine, RFQ,
    Station, Supplier,
)

PASSWORD = "demo12345"


class Command(BaseCommand):
    help = "Заполнить базу синтетическими демо-данными ГРЭС"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Очистить базу перед заполнением")

    def handle(self, *args, **opts):
        from django.conf import settings
        if not settings.DEMO_MODE:
            raise CommandError("Демо-данные заливаются только при DEMO_MODE=1 (не на боевом сервере).")
        if opts["reset"]:
            call_command("flush", "--noinput")
            call_command("migrate", "--run-syncdb", verbosity=0)
            self._restore_corridors()
        if User.objects.filter(username="ospanova").exists():
            self.stdout.write(self.style.WARNING("Демо-данные уже есть. Используйте --reset для пересоздания."))
            return
        with transaction.atomic():
            self.seed()
        self.stdout.write(self.style.SUCCESS(
            "Готово. Пользователи: serik, beketov, ospanova, ahmetov, nurlanova, musin, saparov, ermekov, kasymov, "
            f"temirov, zhumabaeva, akhmetova, admin. Пароль: {PASSWORD}"))

    def _restore_corridors(self):
        """flush очищает и данные миграций — возвращаем коридоры по умолчанию."""
        import importlib
        from django.apps import apps
        mig = importlib.import_module("core.migrations.0003_grees_corridors_stations")
        mig.forwards(apps, None)

    # ------------------------------------------------------------------ helpers
    def user(self, username, last, first, role_names, dept=None, station=None, position="", staff=False, superuser=False):
        u = User.objects.create_user(username, f"{username}@gres.example.kz", PASSWORD, first_name=first, last_name=last,
                                     is_staff=staff or superuser, is_superuser=superuser)
        for r in role_names:
            u.groups.add(Group.objects.get_or_create(name=r)[0])
        Profile.objects.create(user=u, department=dept, station=station, position=position)
        return u

    def memo(self, initiator, title, why, items, budget, cat, required_days=21, submit=True, urgent=False):
        prof = initiator.profile
        required = timezone.localdate() + timedelta(days=required_days)
        m = Memo.objects.create(department=prof.department, station=prof.station, initiator=initiator, title=title,
                                justification=why, required_date=required, budget_item=budget, category=cat)
        services.log(f"создал потребность ({len(items)} поз.). Цены не указывались", initiator, memo=m)
        for n, (nom, qty, spec) in enumerate(items, 1):
            MemoItem.objects.create(memo=m, line_no=n, nomenclature=nom, description=nom.name, spec=spec,
                                    quantity=D(qty), unit=nom.unit, required_date=required, budget_item=budget,
                                    category=cat, urgent=urgent)
        if submit:
            services.submit_memo(m, initiator)
        else:
            services.recalc_memo(m)
        return m

    def file(self, name, text, user, kind, **target):
        a = Attachment(kind=kind, name=name, uploaded_by=user, **target)
        a.file.save(name.replace(" ", "_"), ContentFile(text.encode("utf-8")), save=False)
        a.save()
        return a

    def quote(self, proc, supplier, prices, user, offered=None, analog=None):
        rfq = RFQ.objects.get(procurement=proc, supplier=supplier)
        rows = {}
        for line in proc.lines.filter(state="active").select_related("item"):
            nom = line.item.nomenclature
            if nom in prices:
                p = prices[nom]
                rows[line.pk] = {"price": None if p is None else D(p), "not_offered": p is None,
                                 "offered_qty": (offered or {}).get(nom),
                                 "is_analog": bool((analog or {}).get(nom)), "analog_description": (analog or {}).get(nom, "")}
        return services.save_quote(rfq, {"payment_terms": "по договору", "valid_until": timezone.localdate() + timedelta(days=30)},
                                   rows, user)

    def shift(self, days, memos=(), procs=(), contracts=()):
        """Сдвинуть «историю» сценария в прошлое, чтобы «Ждёт N дн.» и лента выглядели правдоподобно."""
        delta = timedelta(days=days)
        for m in memos:
            m.refresh_from_db()
            Memo.objects.filter(pk=m.pk).update(
                created_at=m.created_at - delta, submitted_at=(m.submitted_at - delta) if m.submitted_at else None,
                approved_at=(m.approved_at - delta) if m.approved_at else None)
            for i in m.items.all():
                MemoItem.objects.filter(pk=i.pk).update(
                    status_changed_at=i.status_changed_at - delta,
                    pool_since=(i.pool_since - delta) if i.pool_since else None)
            for h in HistoryEntry.objects.filter(memo=m):
                HistoryEntry.objects.filter(pk=h.pk).update(created_at=h.created_at - delta)
        for p in procs:
            p.refresh_from_db()
            Procurement.objects.filter(pk=p.pk).update(
                created_at=p.created_at - delta,
                decision_submitted_at=(p.decision_submitted_at - delta) if p.decision_submitted_at else None)
            for a in p.approvals.all():
                DecisionApproval.objects.filter(pk=a.pk).update(
                    due_at=(a.due_at - delta) if a.due_at else None,
                    decided_at=(a.decided_at - delta) if a.decided_at else None)
            for h in HistoryEntry.objects.filter(procurement=p):
                HistoryEntry.objects.filter(pk=h.pk).update(created_at=h.created_at - delta)
            for q in Question.objects.filter(procurement=p):
                Question.objects.filter(pk=q.pk).update(created_at=q.created_at - delta,
                                                        answered_at=(q.answered_at - delta) if q.answered_at else None)
        for c in contracts:
            c.refresh_from_db()
            Contract.objects.filter(pk=c.pk).update(date=c.date - delta, created_at=c.created_at - delta)
            for h in HistoryEntry.objects.filter(contract=c):
                HistoryEntry.objects.filter(pk=h.pk).update(created_at=h.created_at - delta)

    def approve_all(self, proc, users_by_role, except_roles=(), comments=None):
        for a in proc.approvals.filter(state="pending"):
            if a.role in except_roles:
                continue
            services.resolve_decision_approval(a, users_by_role[a.role], True, (comments or {}).get(a.role, ""))

    def sign(self, contract, buyer):
        services.set_contract_status(contract, Contract.Status.SIGNING, buyer)
        services.set_contract_status(contract, Contract.Status.SIGNED, buyer)
        self.file(f"Договор {contract.number}.txt", f"Скан договора {contract.number} (демо)", buyer,
                  Attachment.Kind.CONTRACT, contract=contract)

    # ------------------------------------------------------------------ data
    def seed(self):
        today = timezone.localdate()
        for r in roles.ALL_ROLES:
            Group.objects.get_or_create(name=r)

        g1 = Station.objects.create(name="ГРЭС-1")
        g2 = Station.objects.create(name="ГРЭС-2")
        g3 = Station.objects.create(name="ГРЭС-3")
        dep = {n: Department.objects.create(name=n) for n in
               ["Дирекция", "КТЦ", "Химцех", "Электроцех", "ПТО", "ОМТС", "ФЭО", "Юридический отдел", "ОТиТБ", "Бухгалтерия"]}

        ahmetov = self.user("ahmetov", "Ахметов", "Даурен", [roles.CHIEF, roles.APPROVER], dep["Дирекция"], g1, "Главный инженер")
        serik = self.user("serik", "Нурлыбеков", "Серик", [roles.INITIATOR], dep["КТЦ"], g1, "Инженер КТЦ · инициатор")
        beketov = self.user("beketov", "Бекетов", "Аскар", [roles.APPROVER, roles.INITIATOR], dep["КТЦ"], g1, "Начальник КТЦ")
        ospanova = self.user("ospanova", "Оспанова", "Айгерим", [roles.BUYER], dep["ОМТС"], g1, "Отдел закупок (ОМТС)", staff=True)
        nurlanova = self.user("nurlanova", "Нурланова", "Салтанат", [roles.CFO], dep["ФЭО"], g1, "Финансовый отдел")
        musin = self.user("musin", "Мусин", "Тимур", [roles.LAWYER], dep["Юридический отдел"], g1, "Юридический отдел")
        saparov = self.user("saparov", "Сапаров", "Марат", [roles.DIRECTOR], dep["Дирекция"], g1, "Генеральный директор")
        ermekov = self.user("ermekov", "Ермеков", "Болат", [roles.PTO, roles.INITIATOR], dep["ПТО"], g1, "ПТО")
        kasymov = self.user("kasymov", "Касымов", "Ерлан", [roles.INITIATOR], dep["Химцех"], g2, "Инженер химцеха")
        zhakupov = self.user("zhakupov", "Жакупов", "Руслан", [roles.APPROVER], dep["Химцех"], g2, "Начальник химцеха")
        temirov = self.user("temirov", "Темиров", "Канат", [roles.INITIATOR], dep["Электроцех"], g3, "Инженер электроцеха")
        abenov = self.user("abenov", "Абенов", "Санжар", [roles.APPROVER], dep["Электроцех"], g3, "Начальник электроцеха")
        zhumabaeva = self.user("zhumabaeva", "Жумабаева", "Гульмира", [roles.INITIATOR], dep["ОТиТБ"], g1, "Инженер по ОТиТБ")
        self.user("akhmetova", "Ахметова", "Гульнара", [roles.ACCOUNTANT], dep["Бухгалтерия"], g1, "Бухгалтер")
        self.user("admin", "Системы", "Админ", [roles.ADMIN], superuser=True)
        for d, head in [("КТЦ", beketov), ("Химцех", zhakupov), ("Электроцех", abenov), ("ОТиТБ", ahmetov),
                        ("ПТО", ahmetov), ("Дирекция", ahmetov)]:
            dep[d].head = head
            dep[d].save()
        by_role = {roles.CHIEF: ahmetov, roles.CFO: nurlanova, roles.BUYER: ospanova, roles.LAWYER: musin,
                   roles.DIRECTOR: saparov, roles.PTO: ermekov}

        B = lambda code, name, limit: BudgetItem.objects.create(code=code, name=name, limit=D(limit))
        b_rep = B("01.01", "Ремонт основного оборудования", 250_000_000)
        b_cap = B("01.02", "Капремонт", 2_500_000_000)
        b_exp = B("02.01", "Эксплуатационные материалы", 80_000_000)
        b_chem = B("02.02", "Химреагенты", 60_000_000)
        b_fuel = B("03.01", "Топливо", 12_000_000_000)
        b_safe = B("04.01", "Охрана труда", 15_000_000)

        C = lambda n: Category.objects.create(name=n)
        c_parts, c_mat, c_chem, c_oil, c_rep, c_fuel, c_safe = (
            C("Запчасти КТЦ"), C("Материалы"), C("Химреагенты"), C("Масла и смазки"), C("Ремонтные работы"),
            C("Топливо"), C("Охрана труда"))

        def N(name, unit, cat, code):
            return Nomenclature.objects.create(name=name, unit=unit, category=cat, code_1c=code)
        seal = N("Торцевое уплотнение вала ПЭН", "компл.", c_parts, "10-0001")
        sleeve = N("Втулка защитная вала", "шт", c_parts, "10-0002")
        rings = N("Кольца уплотнительные резиновые (набор)", "упак.", c_parts, "10-0003")
        el4 = N("Электроды сварочные УОНИ 13/55 Ø4", "кг", c_mat, "20-0001")
        el3 = N("Электроды сварочные УОНИ 13/55 Ø3", "кг", c_mat, "20-0002")
        bearing = N("Подшипник роликовый сферический 3626", "шт", c_parts, "10-0101")
        bseal = N("Комплект уплотнений подшипникового узла", "компл.", c_parts, "10-0102")
        suit = N("Костюм утеплённый зимний", "компл.", c_safe, "40-0001")
        boots = N("Ботинки защитные утеплённые", "шт", c_safe, "40-0002")
        helmet = N("Каска защитная", "шт", c_safe, "40-0003")
        acid = N("Кислота серная техническая 92%", "т", c_chem, "30-0001")
        rail = N("Услуги ж/д перевозки (опасный груз)", "усл.", c_chem, "30-0002")
        turbine = N("Работы по капитальному ремонту турбины К-500-240", "усл.", c_rep, "50-0001")
        flow = N("Комплект запасных частей проточной части турбины", "компл.", c_rep, "50-0002")
        coal = N("Уголь экибастузский СС-1", "т", c_fuel, "60-0001")
        trans_oil = N("Масло трансформаторное ГК", "т", c_oil, "70-0001")
        turb_oil = N("Масло турбинное Тп-22С", "т", c_oil, "70-0002")
        valve = N("Задвижка стальная Ду100 Ру40", "шт", c_parts, "10-0201")

        def S(name, bin_, cats, email):
            s = Supplier.objects.create(name=name, bin=bin_, email=email, phone="+7 7182 00 00 00")
            s.categories.set(cats)
            return s
        energo = S("ТОО «Энергозапчасть»", "050340001234", [c_parts], "sales@energozap.example.kz")
        kazturbo = S("ТОО «Казтурбо»", "060440002345", [c_parts, c_rep], "info@kazturbo.example.kz")
        spec = S("ТОО «Спецзащита-KZ»", "120540009876", [c_safe], "b2b@speczashita.example.kz")
        kazhim = S("ТОО «КазХимПром»", "000940005555", [c_chem], "order@kazhim.example.kz")
        temirhim = S("ТОО «Темир-Хим»", "110240007777", [c_chem], "sales@temirhim.example.kz")
        ker = S("АО «Казэнергоремонт»", "990140001111", [c_rep], "tender@ker.example.kz")
        ers = S("ТОО «Энергоремонт-Сервис»", "100340008888", [c_rep], "info@ers.example.kz")
        bogatyr = S("ТОО «Богатырь Комир»", "970140002222", [c_fuel], "sales@bogatyr.example.kz")
        neftehim = S("ТОО «Нефтехим-KZ»", "080240003333", [c_oil], "sales@neftehim.example.kz")
        lukoil = S("ТОО «Смазочные материалы»", "090840004444", [c_oil], "b2b@oil.example.kz")
        S("ТОО «Сварка-Сервис»", "130640005555", [c_mat], "info@svarka.example.kz")

        Contract.objects.create(number="РД-14/2026", kind=Contract.Kind.FRAMEWORK, supplier=spec, status=Contract.Status.SIGNED,
                                date=date(today.year, 1, 20), valid_until=date(today.year, 12, 31), responsible=ospanova)
        Contract.objects.create(number="17-У/2026", kind=Contract.Kind.FRAMEWORK, supplier=bogatyr, status=Contract.Status.SIGNED,
                                date=date(today.year, 1, 10), valid_until=date(today.year, 12, 31), responsible=ospanova)
        tpl = MemoTemplate.objects.create(name="СИЗ на квартал", department=dep["ОТиТБ"])
        MemoTemplateLine.objects.create(template=tpl, nomenclature=suit, quantity=40)
        MemoTemplateLine.objects.create(template=tpl, nomenclature=boots, quantity=40)

        # ---- прошлый год: подшипники дороже (для отчёта «Экономия»)
        old = self.memo(serik, "Подшипники ДН-26 (прошлый год)", "Плановая замена", [(bearing, 4, "")], b_rep, c_parts)
        services.approve_memo(old, beketov)
        p_old = services.create_procurement(ospanova, list(old.items.all()), "Подшипники ДН-26", Procurement.Method.SINGLE, buyer=ospanova)
        services.quick_price(p_old, energo, {l.pk: D(650000) for l in p_old.lines.all()}, ospanova, "по факту")
        self.approve_all(p_old, by_role)
        (c_old,) = services.create_contracts(p_old, ospanova, {})
        self.sign(c_old, ospanova)
        services.register_receipt(c_old, today - timedelta(days=300), "ПТУ-0912",
                                  [(c_old.lines.first(), D(4), ReceiptLine.Match.ID, True, "", "")], ospanova)
        services.register_payment(c_old, today - timedelta(days=295), c_old.amount, "ПП-1101", nurlanova)
        services.close_item(old.items.first(), serik)
        self.shift(370, [old], [p_old], [c_old])
        Contract.objects.filter(pk=c_old.pk).update(number=f"Д-{today.year - 1}-0042")

        # ---- 1. Электроды: черновик у инициатора (задача «Отправить»)
        self.memo(serik, "Электроды сварочные УОНИ 13/55 (Ø3, Ø4)", "Текущий ремонт трубопроводов котла ст.№2",
                  [(el4, 500, "ГОСТ 9466"), (el3, 200, "ГОСТ 9466")], b_rep, c_mat, required_days=14, submit=False)

        # ---- 2. Задвижки: СЗ на согласовании у руководителя КТЦ
        m_valve = self.memo(serik, "Задвижки Ду100 для замены на линии питательной воды", "Пропуск арматуры, акт осмотра №57",
                            [(valve, 6, "Ру40, фланцевая")], b_rep, c_parts, required_days=30)
        self.shift(1, [m_valve])

        # ---- 3. ПЭН-2: утверждена, у закупщика на расценке (из одного источника)
        m_pen = self.memo(serik, "Комплектующие для ремонта питательного насоса ПЭН-2", "Течь по валу ПЭН-2, плановый ремонт при остановке",
                          [(seal, 2, "по чертежу завода-изготовителя"), (sleeve, 4, ""), (rings, 10, "")], b_rep, c_parts, 24)
        services.approve_memo(m_pen, beketov)
        self.file("СЗ-скан ПЭН-2.txt", "Скан служебной записки (демо)", serik, Attachment.Kind.MEMO, memo=m_pen)
        p_pen = services.create_procurement(ospanova, list(m_pen.items.all()), "Комплектующие для ремонта ПЭН-2",
                                            Procurement.Method.SINGLE, buyer=ospanova)
        self.shift(1, [m_pen], [p_pen])

        # ---- 4. Подшипники ДН-26: 🟢 расценено, ждёт главного инженера
        m_brg = self.memo(serik, "Подшипники для дымососа ДН-26 (компл.)", "Вибрация подшипникового узла ДН-26Б выше нормы, плановая замена",
                          [(bearing, 4, "для ДН-26Б"), (bseal, 4, "")], b_rep, c_parts, 18)
        services.approve_memo(m_brg, beketov)
        p_brg = services.create_procurement(ospanova, list(m_brg.items.all()), "Подшипники для дымососа ДН-26",
                                            Procurement.Method.SINGLE, buyer=ospanova)
        lines = {l.item.nomenclature_id: l.pk for l in p_brg.lines.all()}
        services.quick_price(p_brg, energo, {lines[bearing.pk]: D(600000), lines[bseal.pk]: D(250000)}, ospanova,
                             "100% в течение 10 р.д. после поставки")
        self.file("КП Энергозапчасть.txt", "КП: подшипник 3626 — 600 000 ₸/шт; уплотнения — 250 000 ₸/компл.", ospanova,
                  Attachment.Kind.QUOTE, procurement=p_brg)
        self.file("КП Казтурбо.txt", "КП: подшипник 3626 — 640 000 ₸/шт", ospanova, Attachment.Kind.QUOTE, procurement=p_brg)
        self.shift(1, [m_brg], [p_brg])

        # ---- 5. СИЗ: 🟢 по рамочному договору, ждёт главного инженера (вторая «зелёная»)
        m_siz = self.memo(zhumabaeva, "Спецодежда и СИЗ для КТЦ, 40 комплектов", "Плановая выдача СИЗ по нормам, IV квартал",
                          [(suit, 40, ""), (boots, 40, ""), (helmet, 40, "")], b_safe, c_safe, 10)
        services.approve_memo(m_siz, ahmetov)
        p_siz = services.create_procurement(ospanova, list(m_siz.items.all()), "Спецодежда и СИЗ, IV квартал",
                                            Procurement.Method.FRAMEWORK, buyer=ospanova)
        lines = {l.item.nomenclature_id: l.pk for l in p_siz.lines.all()}
        services.quick_price(p_siz, spec, {lines[suit.pk]: D(45000), lines[boots.pk]: D(20000), lines[helmet.pk]: D(6500)},
                             ospanova, "Предоплата 100%")
        self.file("Счёт №445.txt", "Счёт по рамочному договору РД-14/2026 (демо)", ospanova, Attachment.Kind.QUOTE, procurement=p_siz)
        self.shift(2, [m_siz], [p_siz])

        # ---- 6. Серная кислота: 🟡 сравнение 2 КП, финансы согласовали, вопрос ГИ, срок истёк
        m_acid = self.memo(kasymov, "Реагенты ХВО: серная кислота 92%, 60 т", "Регенерация катионитовых фильтров, запас на складе — на 18 дней",
                           [(acid, 60, ""), (rail, 1, "")], b_chem, c_chem, 26)
        services.approve_memo(m_acid, zhakupov)
        p_acid = services.create_procurement(ospanova, list(m_acid.items.all()), "Серная кислота 92%, 60 т",
                                             Procurement.Method.RFQ, timezone.localdate(), buyer=ospanova)
        services.add_rfqs(p_acid, [kazhim, temirhim], ospanova)
        services.mark_rfqs_sent(p_acid, ospanova)
        q1 = self.quote(p_acid, kazhim, {acid: 450000, rail: 500000}, ospanova)
        self.quote(p_acid, temirhim, {acid: 432000, rail: None}, ospanova)
        services.set_decision(p_acid, [(ql.procurement_line, ql, ql.procurement_line.quantity) for ql in q1.lines.all()], ospanova)
        Procurement.objects.filter(pk=p_acid.pk).update(payment_terms="Аванс 30%, 70% по факту поставки")
        p_acid.refresh_from_db()
        services.submit_decision(p_acid, ospanova)
        for name, text in [("КП КазХимПром.txt", "450 000 ₸/т + ж/д 500 000 ₸"), ("КП Темир-Хим.txt", "432 000 ₸/т, без доставки"),
                           ("Протокол выбора.txt", "Протокол выбора поставщика (демо)")]:
            self.file(name, text, ospanova, Attachment.Kind.QUOTE, procurement=p_acid)
        services.resolve_decision_approval(p_acid.approvals.get(role=roles.CFO), nurlanova, True)
        q = services.ask_question(ahmetov, "Почему не выбран ТОО «Темир-Хим»? Их КП на 4% ниже", procurement=p_acid)
        services.answer_question(q, ospanova, "У Темир-Хим нет лицензии на перевозку опасных грузов и нет доставки, приложила протокол")
        self.shift(4, [m_acid], [p_acid])

        # ---- 7. Капремонт турбины: 🔴 тендер, 3 из 5 согласовали, ГИ просрочил → эскалация гендиректору
        m_tur = self.memo(beketov, "Капремонт турбины К-500-240 ст.№3 (подряд)", "Наработка 210 тыс. ч, по графику ППР капремонт в 2027 г.",
                          [(turbine, 1, "по ТЗ и дефектной ведомости"), (flow, 1, "")], b_cap, c_rep, 150)
        services.approve_memo(m_tur, ahmetov)
        self.file("ТЗ на ремонт.txt", "Техническое задание на капремонт (демо)", beketov, Attachment.Kind.MEMO, memo=m_tur)
        p_tur = services.create_procurement(ospanova, list(m_tur.items.all()), "Капремонт турбины К-500-240 ст.№3",
                                            Procurement.Method.TENDER, buyer=ospanova)
        services.add_rfqs(p_tur, [ker, ers], ospanova)
        services.mark_rfqs_sent(p_tur, ospanova)
        self.quote(p_tur, ker, {turbine: 1_800_000_000, flow: 50_000_000}, ospanova)
        self.quote(p_tur, ers, {turbine: 1_960_000_000, flow: 48_000_000}, ospanova)
        services.decision_preset(p_tur, "single", ospanova, RFQ.objects.get(procurement=p_tur, supplier=ker).quote)
        Procurement.objects.filter(pk=p_tur.pk).update(payment_terms="Аванс 20%, далее по актам КС-2 ежемесячно")
        p_tur.refresh_from_db()
        services.submit_decision(p_tur, ospanova)
        self.file("Протокол тендера.txt", "Протокол тендера (демо)", ospanova, Attachment.Kind.QUOTE, procurement=p_tur)
        self.approve_all(p_tur, by_role, except_roles=(roles.CHIEF, roles.DIRECTOR),
                         comments={roles.LAWYER: "с замечанием: добавить штраф за срыв сроков 0,1%/день"})
        self.shift(8, [m_tur], [p_tur])

        # ---- 8. Уголь: 🔵 по действующему договору, упрощённый круг ПТО + Финансы, оплачен 1-й транш
        m_coal = self.memo(ermekov, "Уголь экибастузский, 120 тыс. т (октябрь)", "Месячная спецификация по договору №17-У/2026",
                           [(coal, 120000, "по спецификации №10 к договору")], b_fuel, c_fuel, 30)
        services.approve_memo(m_coal, ahmetov)
        p_coal = services.create_procurement(ospanova, list(m_coal.items.all()), "Уголь экибастузский, октябрь",
                                             Procurement.Method.CONTRACT, buyer=ospanova)
        services.quick_price(p_coal, bogatyr, {p_coal.lines.first().pk: D(8000)}, ospanova, "Предоплата по графику, еженедельно")
        self.approve_all(p_coal, by_role)
        (c_coal,) = services.create_contracts(p_coal, ospanova, {bogatyr.pk: {"mode": "spec"}})
        self.sign(c_coal, ospanova)
        services.register_payment(c_coal, today - timedelta(days=6), D(240_000_000), "ПП-2210 (1-й транш)", None)
        services.register_payment(c_coal, today - timedelta(days=1), D(240_000_000), "ПП-2265 (2-й транш)", None)
        self.shift(20, [m_coal], [p_coal], [c_coal])

        # ---- 9. Трансформаторное масло: 🟡 3 КП, всё согласовано, поставлено и оплачено → закрыто
        m_oil = self.memo(temirov, "Трансформаторное масло ГК, 40 т", "Доливка в трансформаторы Т-1, Т-2 после ремонта",
                          [(trans_oil, 40, "")], b_exp, c_oil, 5)
        services.approve_memo(m_oil, abenov)
        p_oil = services.create_procurement(ospanova, list(m_oil.items.all()), "Масло трансформаторное ГК, 40 т",
                                            Procurement.Method.RFQ, buyer=ospanova)
        services.add_rfqs(p_oil, [neftehim, lukoil], ospanova)
        services.mark_rfqs_sent(p_oil, ospanova)
        self.quote(p_oil, neftehim, {trans_oil: 455000}, ospanova)
        self.quote(p_oil, lukoil, {trans_oil: 471000}, ospanova)
        services.decision_preset(p_oil, "best", ospanova)
        services.submit_decision(p_oil, ospanova)
        self.approve_all(p_oil, by_role)
        (ct_oil,) = services.create_contracts(p_oil, ospanova, {neftehim.pk: {"number": f"88/{today.year}"}})
        self.sign(ct_oil, ospanova)
        services.register_receipt(ct_oil, today - timedelta(days=10), "ПТУ-1877",
                                  [(ct_oil.lines.first(), D(40), ReceiptLine.Match.ID, True, "", "")], None)
        services.close_item(m_oil.items.first(), temirov)
        services.register_payment(ct_oil, today - timedelta(days=7), ct_oil.amount, "ПП-2190", None)
        self.shift(35, [m_oil], [p_oil], [ct_oil])

        # ---- 10. ТЗ: одинаковые позиции из разных СЗ и станций лежат в пуле — кандидаты на консолидацию
        m_t1 = self.memo(serik, "Масло турбинное для маслосистемы ТГ-2", "Плановая доливка", [(turb_oil, 2, "")], b_exp, c_oil, 20)
        services.approve_memo(m_t1, beketov)
        m_t2 = self.memo(temirov, "Масло турбинное, резерв электроцеха", "Резерв на складе", [(turb_oil, 1, "")], b_exp, c_oil, 25,
                         urgent=True)
        services.approve_memo(m_t2, abenov)
        self.shift(6, [m_t1, m_t2])

        services.run_periodic()
