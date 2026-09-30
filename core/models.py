"""Модель данных (п. 3 ТЗ).

СЗ 1──< Позиция ──< Строка закупки >── Закупка ──< Запрос КП ──< КП ──< Строка КП
                         │                  Решение (позиция, поставщик, кол-во)
                         └──< Строка договора >── Договор ── Поставщик
                                   │
                          Оплата / Поступление (из 1С)
"""
import secrets
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

ZERO = Decimal("0")


# ---------------------------------------------------------------- Справочники


class Department(models.Model):
    name = models.CharField("Подразделение", max_length=200, unique=True)
    head = models.ForeignKey(
        User, verbose_name="Руководитель (согласующий СЗ)", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="headed_departments",
    )

    class Meta:
        verbose_name = "подразделение"
        verbose_name_plural = "подразделения"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    department = models.ForeignKey(
        Department, verbose_name="Подразделение", null=True, blank=True, on_delete=models.SET_NULL
    )
    position = models.CharField("Должность", max_length=200, blank=True)

    class Meta:
        verbose_name = "профиль"
        verbose_name_plural = "профили"

    def __str__(self):
        return str(self.user)


class BudgetItem(models.Model):
    code = models.CharField("Код", max_length=50, unique=True)
    name = models.CharField("Статья бюджета", max_length=200)

    class Meta:
        verbose_name = "статья бюджета"
        verbose_name_plural = "статьи бюджета"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class Category(models.Model):
    name = models.CharField("Категория", max_length=200, unique=True)

    class Meta:
        verbose_name = "категория"
        verbose_name_plural = "категории"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Nomenclature(models.Model):
    name = models.CharField("Наименование", max_length=300)
    unit = models.CharField("Ед. изм.", max_length=30, default="шт")
    category = models.ForeignKey(Category, verbose_name="Категория", null=True, blank=True, on_delete=models.SET_NULL)
    code_1c = models.CharField("Код в 1С", max_length=50, blank=True)

    class Meta:
        verbose_name = "номенклатура"
        verbose_name_plural = "номенклатура"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}, {self.unit}"


class Supplier(models.Model):
    name = models.CharField("Наименование", max_length=300)
    bin = models.CharField("БИН/ИИН", max_length=20, blank=True)
    email = models.EmailField("E-mail", blank=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    contact = models.CharField("Контактное лицо", max_length=200, blank=True)
    categories = models.ManyToManyField(Category, verbose_name="Закрываемые категории", blank=True)
    code_1c = models.CharField("Код в 1С", max_length=50, blank=True)

    class Meta:
        verbose_name = "поставщик"
        verbose_name_plural = "поставщики"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ApprovalRule(models.Model):
    """Порог согласования решения по закупке (шаг 7): сумма ≥ порога → нужна роль."""

    min_amount = models.DecimalField("Сумма от", max_digits=16, decimal_places=2)
    role = models.CharField("Роль согласующего", max_length=50)

    class Meta:
        verbose_name = "правило согласования решения"
        verbose_name_plural = "правила согласования решений"
        ordering = ["min_amount"]

    def __str__(self):
        return f"≥ {self.min_amount:,.0f} → {self.role}"


class MemoTemplate(models.Model):
    name = models.CharField("Название шаблона", max_length=200)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = "шаблон СЗ"
        verbose_name_plural = "шаблоны СЗ"

    def __str__(self):
        return self.name


class MemoTemplateLine(models.Model):
    template = models.ForeignKey(MemoTemplate, on_delete=models.CASCADE, related_name="lines")
    nomenclature = models.ForeignKey(Nomenclature, on_delete=models.CASCADE)
    quantity = models.DecimalField("Кол-во", max_digits=14, decimal_places=3)


# ---------------------------------------------------------------- СЗ и позиции


class Memo(models.Model):
    """Служебная записка (СЗ) — документ инициатора."""

    class State(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ON_APPROVAL = "on_approval", "На согласовании"
        APPROVED = "approved", "Утверждена"
        PARTIALLY_APPROVED = "partially_approved", "Утверждена частично"
        REJECTED = "rejected", "Отклонена"

    number = models.PositiveIntegerField("Номер", unique=True, editable=False)
    department = models.ForeignKey(Department, verbose_name="Подразделение", on_delete=models.PROTECT)
    initiator = models.ForeignKey(User, verbose_name="Инициатор", on_delete=models.PROTECT, related_name="memos")
    justification = models.TextField("Обоснование")
    required_date = models.DateField("Требуемый срок")
    state = models.CharField("Состояние документа", max_length=30, choices=State.choices, default=State.DRAFT)
    approver = models.ForeignKey(
        User, verbose_name="Согласующий", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approval_comment = models.TextField("Комментарий согласующего", blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "служебная записка"
        verbose_name_plural = "служебные записки"
        ordering = ["-number"]

    def __str__(self):
        return f"СЗ №{self.number}"

    def save(self, *args, **kwargs):
        if not self.number:
            last = Memo.objects.aggregate(m=models.Max("number"))["m"] or 0
            self.number = last + 1
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("memo_detail", args=[self.pk])

    @property
    def is_editable(self):
        return self.state == self.State.DRAFT

    @property
    def is_approved(self):
        return self.state in (self.State.APPROVED, self.State.PARTIALLY_APPROVED)

    # --- Расчётный статус (п. 4.2) ---
    SUMMARY_GROUPS = [
        ("pool", "в пуле"),
        ("procurement", "в закупке"),
        ("contract", "в договоре"),
        ("delivered", "поставлена"),
        ("closed", "закрыта"),
        ("rejected", "отклонена"),
        ("withdrawn", "отозвана"),
    ]

    def summary(self):
        counts = {}
        for item in self.items.all():
            g = item.summary_group
            counts[g] = counts.get(g, 0) + 1
        return counts

    def summary_text(self):
        items = list(self.items.all())
        counts = self.summary()
        parts = [f"{counts[g]} {label}" for g, label in self.SUMMARY_GROUPS if counts.get(g)]
        return f"{len(items)} поз.: " + ", ".join(parts) if parts else f"{len(items)} поз."

    @property
    def aggregate_status(self):
        """Укрупнённый статус для реестра."""
        if self.state == self.State.DRAFT:
            return ("draft", "Черновик")
        if self.state == self.State.ON_APPROVAL:
            return ("on_approval", "На согласовании")
        if self.state == self.State.REJECTED:
            return ("rejected", "Отклонена")
        items = [i for i in self.items.all() if i.status != MemoItem.Status.REJECTED]
        if not items:
            return ("rejected", "Отклонена")
        today = timezone.localdate()
        final = {MemoItem.Status.DELIVERED, MemoItem.Status.CLOSED, MemoItem.Status.WITHDRAWN}
        if any(i.required_date < today and i.status not in final for i in items):
            return ("problem", "Проблемная")
        if all(i.status in final for i in items):
            return ("done", "Исполнена")
        if all(i.status == MemoItem.Status.APPROVED for i in items):
            return ("new", "Новая")
        return ("in_work", "В работе")

    @property
    def progress(self):
        """Доля покрытия: поставленное количество / общее, по неотклонённым позициям (в %)."""
        items = [i for i in self.items.all() if i.status not in (MemoItem.Status.REJECTED, MemoItem.Status.WITHDRAWN)]
        if not items:
            return 0
        vals = [min(i.delivered_percent, 100) for i in items]
        return round(sum(vals) / len(vals))

    @property
    def coverage(self):
        items = [i for i in self.items.all() if i.status not in (MemoItem.Status.REJECTED, MemoItem.Status.WITHDRAWN)]
        if not items:
            return 0
        return round(sum(min(i.coverage_percent, 100) for i in items) / len(items))


class MemoItem(models.Model):
    """Позиция потребности — атомарная строка СЗ, живёт своей жизнью после утверждения."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ON_APPROVAL = "on_approval", "На согласовании"
        REJECTED = "rejected", "Отклонена"
        APPROVED = "approved", "Утверждена (в пуле)"
        IN_PROCUREMENT = "in_procurement", "В закупке"
        SUPPLIER_SELECTED = "supplier_selected", "Поставщик выбран"
        PARTIALLY_CONTRACTED = "partially_contracted", "Частично в договоре"
        CONTRACTED = "contracted", "В договоре"
        PARTIALLY_DELIVERED = "partially_delivered", "Частично поставлена"
        DELIVERED = "delivered", "Поставлена"
        CLOSED = "closed", "Закрыта"
        WITHDRAWN = "withdrawn", "Отозвана"

    memo = models.ForeignKey(Memo, verbose_name="СЗ", on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveIntegerField("№ п/п")
    nomenclature = models.ForeignKey(
        Nomenclature, verbose_name="Номенклатура", null=True, blank=True, on_delete=models.PROTECT
    )
    description = models.CharField("Описание", max_length=500)
    quantity = models.DecimalField("Кол-во", max_digits=14, decimal_places=3)
    unit = models.CharField("Ед. изм.", max_length=30, default="шт")
    required_date = models.DateField("Требуемый срок")
    budget_item = models.ForeignKey(
        BudgetItem, verbose_name="Статья бюджета", null=True, blank=True, on_delete=models.PROTECT
    )
    category = models.ForeignKey(Category, verbose_name="Категория", null=True, blank=True, on_delete=models.SET_NULL)
    estimated_price = models.DecimalField("Ориентир. цена", max_digits=14, decimal_places=2, null=True, blank=True)
    urgent = models.BooleanField("Срочно", default=False)

    status = models.CharField("Статус", max_length=30, choices=Status.choices, default=Status.DRAFT, db_index=True)
    status_changed_at = models.DateTimeField(default=timezone.now)
    rejected = models.BooleanField(default=False)
    reject_comment = models.CharField("Причина отклонения", max_length=500, blank=True)
    withdrawn = models.BooleanField(default=False)
    remainder_closed = models.BooleanField("Остаток закрыт", default=False)
    closed = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    pool_note = models.CharField("Пометка в пуле", max_length=200, blank=True)
    pool_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "позиция потребности"
        verbose_name_plural = "позиции потребности"
        ordering = ["memo__number", "line_no"]
        unique_together = [("memo", "line_no")]

    def __str__(self):
        return f"{self.code} {self.description}"

    @property
    def code(self):
        """Идентификатор позиции для 1С: «СЗ-12/3»."""
        return f"СЗ-{self.memo.number}/{self.line_no}"

    # --- Количественные показатели ---
    @property
    def contracted_qty(self):
        return ContractLine.objects.filter(item=self).exclude(
            contract__status=Contract.Status.CANCELLED
        ).aggregate(s=Sum("quantity"))["s"] or ZERO

    @property
    def delivered_qty(self):
        return ReceiptLine.objects.filter(contract_line__item=self, confirmed=True).exclude(
            contract_line__contract__status=Contract.Status.CANCELLED
        ).aggregate(s=Sum("quantity"))["s"] or ZERO

    @property
    def target_qty(self):
        """Сколько должно быть покрыто: всё количество, либо законтрактованное, если остаток закрыт."""
        if self.remainder_closed:
            return min(self.quantity, self.contracted_qty)
        return self.quantity

    @property
    def remaining_qty(self):
        """Непокрытый договорами остаток (для пула)."""
        if self.remainder_closed:
            return ZERO
        return max(self.quantity - self.contracted_qty, ZERO)

    @property
    def coverage_percent(self):
        if not self.quantity:
            return 0
        return round(float(self.contracted_qty / self.quantity * 100))

    @property
    def delivered_percent(self):
        if not self.quantity:
            return 0
        return round(float(self.delivered_qty / self.quantity * 100))

    @property
    def paid_amount(self):
        return sum((cl.paid_amount for cl in self.contract_lines.exclude(contract__status=Contract.Status.CANCELLED)), ZERO)

    @property
    def active_procurement_line(self):
        return self.procurement_lines.filter(state=ProcurementLine.State.ACTIVE).select_related("procurement").first()

    @property
    def summary_group(self):
        S = self.Status
        return {
            S.DRAFT: "draft", S.ON_APPROVAL: "draft",
            S.APPROVED: "pool", S.PARTIALLY_CONTRACTED: "pool",
            S.IN_PROCUREMENT: "procurement", S.SUPPLIER_SELECTED: "procurement",
            S.CONTRACTED: "contract", S.PARTIALLY_DELIVERED: "contract",
            S.DELIVERED: "delivered", S.CLOSED: "closed",
            S.REJECTED: "rejected", S.WITHDRAWN: "withdrawn",
        }[self.status]

    @property
    def in_pool(self):
        return self.pool_since is not None

    @property
    def is_overdue(self):
        return self.required_date < timezone.localdate() and self.status not in (
            self.Status.DELIVERED, self.Status.CLOSED, self.Status.WITHDRAWN, self.Status.REJECTED
        )


# ---------------------------------------------------------------- Закупка


class Procurement(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        RFQ = "rfq", "Запрос КП"
        COLLECTING = "collecting", "Сбор КП"
        ANALYSIS = "analysis", "Анализ"
        DECIDED = "decided", "Решение принято"
        CONTRACTS = "contracts", "Договоры оформлены"
        CLOSED = "closed", "Закрыта"
        CANCELLED = "cancelled", "Отменена"

    class Method(models.TextChoices):
        RFQ = "rfq", "Запрос КП"
        TENDER = "tender", "Тендер"
        SINGLE = "single", "У единственного поставщика"

    class DecisionState(models.TextChoices):
        NONE = "none", "Не сформировано"
        ON_APPROVAL = "on_approval", "На согласовании"
        APPROVED = "approved", "Утверждено"
        REJECTED = "rejected", "Отклонено"

    number = models.PositiveIntegerField("Номер", unique=True, editable=False)
    title = models.CharField("Наименование", max_length=300)
    buyer = models.ForeignKey(User, verbose_name="Ответственный закупщик", on_delete=models.PROTECT, related_name="procurements")
    method = models.CharField("Способ", max_length=20, choices=Method.choices, default=Method.RFQ)
    kp_deadline = models.DateField("Срок сбора КП", null=True, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.DRAFT)
    decision_state = models.CharField(
        "Решение", max_length=20, choices=DecisionState.choices, default=DecisionState.NONE
    )
    decision_comment = models.TextField("Комментарий к решению", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "закупка"
        verbose_name_plural = "закупки"
        ordering = ["-number"]

    def __str__(self):
        return f"Закупка №{self.number}"

    def save(self, *args, **kwargs):
        if not self.number:
            last = Procurement.objects.aggregate(m=models.Max("number"))["m"] or 0
            self.number = last + 1
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("procurement_detail", args=[self.pk])

    @property
    def active_lines(self):
        return self.lines.filter(state__in=[ProcurementLine.State.ACTIVE, ProcurementLine.State.DONE]).select_related(
            "item", "item__memo", "item__nomenclature"
        )

    @property
    def decision_total(self):
        return sum((d.amount for d in self.decision_lines.all()), ZERO)

    @property
    def is_open(self):
        return self.status not in (self.Status.CLOSED, self.Status.CANCELLED)

    @property
    def is_overdue_kp(self):
        return (
            self.kp_deadline
            and self.kp_deadline < timezone.localdate()
            and self.status in (self.Status.RFQ, self.Status.COLLECTING)
            and not Quote.objects.filter(rfq__procurement=self).exists()
        )


class ProcurementLine(models.Model):
    """Строка закупки: позиция в лоте. Одновременно позиция активна только в одной закупке."""

    class State(models.TextChoices):
        ACTIVE = "active", "В лоте"
        DONE = "done", "Отработана"
        RELEASED = "released", "Возвращена в пул"
        REMOVED = "removed", "Отозвана"

    procurement = models.ForeignKey(Procurement, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(MemoItem, on_delete=models.PROTECT, related_name="procurement_lines")
    quantity = models.DecimalField("Кол-во к закупке", max_digits=14, decimal_places=3)
    state = models.CharField(max_length=20, choices=State.choices, default=State.ACTIVE)

    class Meta:
        ordering = ["item__memo__number", "item__line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["item"], condition=models.Q(state="active"), name="one_active_procurement_per_item"
            )
        ]

    def __str__(self):
        return f"{self.procurement} / {self.item.code}"

    @property
    def consolidation_key(self):
        """Ключ сводной строки для запроса КП (п. 6.1): одинаковая номенклатура."""
        if self.item.nomenclature_id:
            return f"n{self.item.nomenclature_id}"
        return f"l{self.pk}"


class RFQ(models.Model):
    """Запрос КП конкретному поставщику по конкретной закупке."""

    procurement = models.ForeignKey(Procurement, on_delete=models.CASCADE, related_name="rfqs")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="rfqs")
    sent_at = models.DateTimeField("Отправлен", null=True, blank=True)
    needs_correction = models.BooleanField("Требует корректировки", default=False)
    token = models.CharField(max_length=64, unique=True, editable=False)

    class Meta:
        verbose_name = "запрос КП"
        verbose_name_plural = "запросы КП"
        unique_together = [("procurement", "supplier")]

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Запрос КП {self.supplier} по {self.procurement}"


class Quote(models.Model):
    """КП поставщика — ответ на запрос."""

    class Source(models.TextChoices):
        MANUAL = "manual", "Введено закупщиком"
        EXCEL = "excel", "Импорт из Excel"
        PORTAL = "portal", "Заполнено поставщиком"

    rfq = models.OneToOneField(RFQ, on_delete=models.CASCADE, related_name="quote")
    received_at = models.DateTimeField("Получено", default=timezone.now)
    valid_until = models.DateField("Действует до", null=True, blank=True)
    payment_terms = models.CharField("Условия оплаты", max_length=300, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    attachment = models.FileField("Файл КП", upload_to="quotes/", blank=True)

    class Meta:
        verbose_name = "КП"
        verbose_name_plural = "КП"

    def __str__(self):
        return f"КП {self.rfq.supplier}"

    @property
    def supplier(self):
        return self.rfq.supplier

    @property
    def total(self):
        return sum((ql.amount for ql in self.lines.all() if not ql.not_offered), ZERO)


class QuoteLine(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="lines")
    procurement_line = models.ForeignKey(ProcurementLine, on_delete=models.CASCADE, related_name="quote_lines")
    price = models.DecimalField("Цена за ед.", max_digits=14, decimal_places=2, null=True, blank=True)
    offered_qty = models.DecimalField(
        "Предлагаемое кол-во", max_digits=14, decimal_places=3, null=True, blank=True,
        help_text="Пусто — на всё количество",
    )
    lead_time_days = models.PositiveIntegerField("Срок поставки, дн.", null=True, blank=True)
    not_offered = models.BooleanField("Не предлагает", default=False)
    is_analog = models.BooleanField("Аналог", default=False)
    analog_description = models.CharField("Описание аналога", max_length=500, blank=True)
    comment = models.CharField("Комментарий", max_length=500, blank=True)

    class Meta:
        unique_together = [("quote", "procurement_line")]

    @property
    def qty(self):
        if self.offered_qty is not None:
            return self.offered_qty
        return self.procurement_line.quantity

    @property
    def amount(self):
        if self.not_offered or self.price is None:
            return ZERO
        return self.price * self.qty


class DecisionLine(models.Model):
    """Решение по закупке: для позиции — поставщик и количество."""

    class AnalogState(models.TextChoices):
        NA = "na", "—"
        PENDING = "pending", "Ждёт подтверждения инициатора"
        ACCEPTED = "accepted", "Инициатор согласен"
        DECLINED = "declined", "Инициатор не согласен"

    procurement = models.ForeignKey(Procurement, on_delete=models.CASCADE, related_name="decision_lines")
    procurement_line = models.ForeignKey(ProcurementLine, on_delete=models.CASCADE, related_name="decision_lines")
    quote_line = models.ForeignKey(QuoteLine, on_delete=models.PROTECT, related_name="decision_lines")
    quantity = models.DecimalField("Кол-во", max_digits=14, decimal_places=3)
    analog_state = models.CharField(max_length=20, choices=AnalogState.choices, default=AnalogState.NA)

    class Meta:
        ordering = ["procurement_line__item__memo__number", "procurement_line__item__line_no"]

    @property
    def supplier(self):
        return self.quote_line.quote.rfq.supplier

    @property
    def price(self):
        return self.quote_line.price or ZERO

    @property
    def amount(self):
        return self.price * self.quantity


class DecisionApproval(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Ожидает"
        APPROVED = "approved", "Согласовано"
        REJECTED = "rejected", "Отклонено"

    procurement = models.ForeignKey(Procurement, on_delete=models.CASCADE, related_name="approvals")
    role = models.CharField("Роль", max_length=50)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    comment = models.CharField(max_length=500, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)


# ---------------------------------------------------------------- Договор


class Contract(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        SIGNING = "signing", "На подписании"
        SIGNED = "signed", "Подписан"
        EXECUTION = "execution", "Исполняется"
        EXECUTED = "executed", "Исполнен"
        CLOSED = "closed", "Закрыт"
        CANCELLED = "cancelled", "Аннулирован"

    class Kind(models.TextChoices):
        REGULAR = "regular", "Договор"
        FRAMEWORK = "framework", "Рамочный договор"
        SPECIFICATION = "specification", "Спецификация / допсоглашение"

    number = models.CharField("Номер", max_length=50)
    date = models.DateField("Дата", default=timezone.localdate)
    kind = models.CharField("Вид", max_length=20, choices=Kind.choices, default=Kind.REGULAR)
    parent = models.ForeignKey(
        "self", verbose_name="Рамочный договор", null=True, blank=True, on_delete=models.PROTECT,
        related_name="specifications",
    )
    supplier = models.ForeignKey(Supplier, verbose_name="Поставщик", on_delete=models.PROTECT, related_name="contracts")
    procurement = models.ForeignKey(
        Procurement, verbose_name="Закупка", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts"
    )
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.DRAFT)
    valid_until = models.DateField("Действует до", null=True, blank=True)
    responsible = models.ForeignKey(User, verbose_name="Ответственный", null=True, blank=True, on_delete=models.SET_NULL)
    exported_1c_at = models.DateTimeField("Выгружен в 1С", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "договор"
        verbose_name_plural = "договоры"
        ordering = ["-created_at"]

    def __str__(self):
        prefix = "Спецификация" if self.kind == self.Kind.SPECIFICATION else "Договор"
        return f"{prefix} №{self.number}"

    def get_absolute_url(self):
        return reverse("contract_detail", args=[self.pk])

    @property
    def amount(self):
        return sum((l.amount for l in self.lines.all()), ZERO)

    @property
    def paid_total(self):
        return self.payments.aggregate(s=Sum("amount"))["s"] or ZERO

    @property
    def delivered_percent(self):
        total = sum((l.quantity for l in self.lines.all()), ZERO)
        if not total:
            return 0
        done = sum((min(l.delivered_qty, l.quantity) for l in self.lines.all()), ZERO)
        return round(float(done / total * 100))

    @property
    def memos(self):
        return Memo.objects.filter(items__contract_lines__contract=self).distinct()


class ContractLine(models.Model):
    """Строка спецификации договора — ссылается ровно на одну позицию потребности."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(MemoItem, on_delete=models.PROTECT, related_name="contract_lines")
    decision_line = models.ForeignKey(DecisionLine, null=True, blank=True, on_delete=models.SET_NULL)
    description = models.CharField("Наименование", max_length=500)
    quantity = models.DecimalField("Кол-во", max_digits=14, decimal_places=3)
    unit = models.CharField("Ед. изм.", max_length=30)
    price = models.DecimalField("Цена", max_digits=14, decimal_places=2)
    over_quantity_confirmed = models.BooleanField("Превышение подтверждено", default=False)

    class Meta:
        ordering = ["item__memo__number", "item__line_no"]

    def __str__(self):
        return f"{self.contract} / {self.item.code}"

    @property
    def amount(self):
        return self.price * self.quantity

    @property
    def delivered_qty(self):
        return self.receipt_lines.filter(confirmed=True).aggregate(s=Sum("quantity"))["s"] or ZERO

    @property
    def paid_amount(self):
        """Оплата по договору распределяется на строки пропорционально сумме (справочно, п. 8)."""
        contract_amount = self.contract.amount
        if not contract_amount:
            return ZERO
        return (self.contract.paid_total * self.amount / contract_amount).quantize(Decimal("0.01"))


class Payment(models.Model):
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="payments")
    date = models.DateField("Дата")
    amount = models.DecimalField("Сумма", max_digits=16, decimal_places=2)
    doc_number = models.CharField("Документ 1С", max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "оплата"
        verbose_name_plural = "оплаты"
        ordering = ["date"]


class Receipt(models.Model):
    """Поступление ТМЦ / акт — приходит из 1С по договору."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="receipts")
    date = models.DateField("Дата")
    doc_number = models.CharField("Документ 1С", max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "поступление"
        verbose_name_plural = "поступления"
        ordering = ["date"]


class ReceiptLine(models.Model):
    class Match(models.TextChoices):
        ID = "id", "По ID позиции"
        NOMENCLATURE = "nomenclature", "По номенклатуре и договору"
        MANUAL = "manual", "Вручную"

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    contract_line = models.ForeignKey(ContractLine, null=True, blank=True, on_delete=models.CASCADE, related_name="receipt_lines")
    raw_name = models.CharField("Наименование из 1С", max_length=500, blank=True)
    raw_ref = models.CharField("Ссылка из 1С", max_length=100, blank=True)
    quantity = models.DecimalField("Кол-во", max_digits=14, decimal_places=3)
    matched_by = models.CharField(max_length=20, choices=Match.choices, default=Match.ID)
    confirmed = models.BooleanField("Подтверждено", default=True)


# ---------------------------------------------------------------- Процессы и служебное


class WithdrawalRequest(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Ожидает закупщика"
        APPROVED = "approved", "Подтверждён"
        REJECTED = "rejected", "Отклонён"

    item = models.ForeignKey(MemoItem, on_delete=models.CASCADE, related_name="withdrawals")
    procurement = models.ForeignKey(Procurement, null=True, blank=True, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField("Причина", max_length=500)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    resolved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    text = models.TextField()
    url = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class HistoryEntry(models.Model):
    """Журнал действий по объектам (вкладка «История»)."""

    memo = models.ForeignKey(Memo, null=True, blank=True, on_delete=models.CASCADE, related_name="history")
    procurement = models.ForeignKey(Procurement, null=True, blank=True, on_delete=models.CASCADE, related_name="history")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="history")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ReminderLog(models.Model):
    """Чтобы SLA-напоминания не дублировались."""

    key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


def currency():
    return settings.PROCUREMENT["CURRENCY"]
