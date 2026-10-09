"""Главный экран в формате макета ГРЭС: реестр (СЗ, закупки, договоры) + карточка.

Здесь — только «как показать»: этап, кого ждём, задача текущего пользователя, KPI.
Правила и переходы остаются в services.py.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from . import roles, services
from .models import (
    AppSetting, Contract, Corridor, DecisionApproval, DecisionLine, Memo, MemoItem, Payment, Procurement,
    ProcurementLine, Question, Station, WithdrawalRequest,
)

STEPS = ["Потребность", "Согласование СЗ", "Расценка", "Согласование", "Договор", "Исполнение", "Закрыто"]
ZERO = Decimal(0)
S = MemoItem.Status
P = Procurement.Status
C = Contract.Status

# ---------------------------------------------------------------- KPI и доступ к ним

KPIS = [
    ("work", "В работе"),
    ("active", "Сумма активных закупок"),
    ("paid", "Оплачено в этом году"),
    ("rest", "Остаток к оплате"),
    ("late", "Просрочено"),
]
DEFAULT_KPI_ACCESS = {
    "work": roles.ALL_ROLES,
    "active": [r for r in roles.ALL_ROLES if r != roles.INITIATOR],
    "paid": [roles.CHIEF, roles.CFO, roles.DIRECTOR, roles.ACCOUNTANT, roles.BUYER, roles.ADMIN],
    "rest": [roles.CHIEF, roles.CFO, roles.DIRECTOR, roles.ACCOUNTANT, roles.BUYER, roles.ADMIN],
    "late": [r for r in roles.ALL_ROLES if r != roles.INITIATOR],
}


def kpi_access():
    stored = AppSetting.get("kpi_access") or {}
    return {k: stored.get(k, DEFAULT_KPI_ACCESS[k]) for k, _ in KPIS}


def visible_kpis(user):
    access = kpi_access()
    mine = roles.user_roles(user)
    return [k for k, _ in KPIS if user.is_superuser or set(access[k]) & mine]


# ---------------------------------------------------------------- строка реестра


@dataclass
class Row:
    key: str
    kind: str                 # memo / proc / contract
    obj: object
    number: str
    title: str
    sub: str = ""
    who: str = ""
    who_sub: str = ""
    amount: Decimal = None
    supplier: str = "—"
    stage: int = 0
    stage_label: str = ""
    state: str = ""           # rejected / cancelled / ""
    wait_days: int = None
    wait_who: str = ""
    dot: str = "n"
    task: str = ""
    task_code: str = ""
    late: bool = False
    station: str = "—"
    paid: Decimal = None
    question: bool = False
    sort_date: object = None
    extra: dict = field(default_factory=dict)

    @property
    def done(self):
        return self.stage == 6 or self.state in ("rejected", "cancelled")

    @property
    def paid_pct(self):
        if not self.amount or self.paid is None:
            return None
        return min(100, round(float(self.paid / self.amount * 100)))


def _days(dt):
    if not dt:
        return None
    if hasattr(dt, "date"):
        dt = timezone.localtime(dt).date()
    return max(0, (timezone.localdate() - dt).days)


def corridor_color(amount, by_contract=False):
    if amount is None:
        return "n"
    c = Corridor.for_amount(amount, by_contract)
    return c.color if c else "n"


# ---------------------------------------------------------------- этапы


def item_stage(item):
    st = item.status
    if st in (S.DRAFT,):
        return 0
    if st == S.ON_APPROVAL:
        return 1
    if st in (S.APPROVED, S.IN_PROCUREMENT, S.PARTIALLY_CONTRACTED):
        line = next((l for l in item.procurement_lines.all() if l.state == ProcurementLine.State.ACTIVE), None)
        if line and line.procurement.decision_state == Procurement.DecisionState.ON_APPROVAL:
            return 3
        return 2
    if st == S.SUPPLIER_SELECTED:
        return 4
    if st == S.CONTRACTED:
        unsigned = any(cl.contract.status in (C.DRAFT, C.SIGNING) for cl in item.contract_lines.all())
        return 4 if unsigned else 5
    if st == S.PARTIALLY_DELIVERED:
        return 5
    return 6  # delivered / closed


def memo_stage(memo):
    if memo.state == Memo.State.DRAFT:
        return 0, ""
    if memo.state == Memo.State.ON_APPROVAL:
        return 1, ""
    if memo.state == Memo.State.REJECTED:
        return 1, "rejected"
    items = [i for i in memo.items.all() if i.status not in (S.REJECTED, S.WITHDRAWN)]
    if not items:
        return 6, ""
    return min(item_stage(i) for i in items), ""


def proc_stage(proc):
    if proc.status == P.CANCELLED:
        return 2, "cancelled"
    if proc.status == P.CLOSED:
        return 6, ""
    if proc.status in (P.DRAFT, P.RFQ, P.COLLECTING, P.ANALYSIS):
        return (3 if proc.decision_state == Procurement.DecisionState.ON_APPROVAL else 2), ""
    if proc.status == P.DECIDED:
        return 4, ""
    contracts = [c for c in proc.contracts.all() if c.status != C.CANCELLED]
    if any(c.status in (C.DRAFT, C.SIGNING) for c in contracts):
        return 4, ""
    return 5, ""


def contract_stage(c):
    if c.status == C.CANCELLED:
        return 4, "cancelled"
    if c.status in (C.DRAFT, C.SIGNING):
        return 4, ""
    if c.status == C.CLOSED:
        return 6, ""
    return 5, ""


def stage_label(stage, state):
    if state == "rejected":
        return "Отклонено"
    if state == "cancelled":
        return "Отменено"
    return STEPS[stage]


# ---------------------------------------------------------------- построение строк


class Builder:
    def __init__(self, user):
        self.user = user
        self.prices = roles.sees_prices(user)
        self.my_approvals = {a.procurement_id: a for a in services.approvals_for(user)}
        self.is_buyer = roles.has_role(user, roles.BUYER, roles.ADMIN)
        self.is_finance = roles.has_role(user, roles.CFO, roles.ACCOUNTANT, roles.ADMIN)

    # --- СЗ
    def memo(self, m):
        from .views.memos import can_approve
        stage, state = memo_stage(m)
        items = list(m.items.all())
        active_items = [i for i in items if i.status not in (S.REJECTED, S.WITHDRAWN)]
        contracts = {}
        procs = {}
        amount = ZERO
        priced = False
        for i in active_items:
            for cl in i.contract_lines.all():
                if cl.contract.status != C.CANCELLED:
                    contracts[cl.contract_id] = cl.contract
                    amount += cl.amount
                    priced = True
            for pl in i.procurement_lines.all():
                if pl.state in (ProcurementLine.State.ACTIVE, ProcurementLine.State.DONE):
                    procs[pl.procurement_id] = pl.procurement
        if not priced:
            dl = DecisionLine.objects.filter(procurement_line__item__memo=m,
                                             procurement_line__state=ProcurementLine.State.ACTIVE)
            for d in dl.select_related("quote_line"):
                amount += d.amount
                priced = True
        r = Row(key=f"m{m.pk}", kind="memo", obj=m, number=f"СЗ-{m.number}", title=m.display_title,
                sub=f"{m.category or (active_items[0].category if active_items and active_items[0].category else 'Без категории')} · {len(items)} поз.",
                who=m.initiator.get_full_name() or m.initiator.username,
                who_sub=f"{m.department}", stage=stage, state=state,
                station=str(m.station) if m.station else "—", sort_date=m.created_at)
        r.amount = amount if priced else None
        suppliers = sorted({c.supplier.name for c in contracts.values()})
        r.supplier = ", ".join(suppliers) if suppliers else "не определён"
        colors = [p.corridor.color for p in procs.values() if p.corridor]
        r.dot = max(colors, key="gyr".find) if colors and any(c in "gyr" for c in colors) else (
            corridor_color(r.amount) if r.amount else "n")
        r.extra = {"procs": list(procs.values()), "contracts": list(contracts.values()), "items": items}
        # кого ждём
        if stage == 1 and not state:
            r.wait_days = _days(m.submitted_at)
            r.wait_who = f"Согласование СЗ: {m.approver.get_full_name() if m.approver else 'руководитель'}"
        elif stage == 2:
            r.wait_days = _days(min((i.status_changed_at for i in active_items if item_stage(i) == 2), default=None))
            r.wait_who = "Закупки: расценка"
        elif stage == 3:
            p = next((p for p in procs.values() if p.decision_state == Procurement.DecisionState.ON_APPROVAL), None)
            if p:
                r.wait_days = _days(p.decision_submitted_at)
                r.wait_who = ", ".join(roles.SHORT.get(a.role, a.role) for a in p.approvals.all()
                                       if a.state == DecisionApproval.State.PENDING)
        elif stage == 4:
            r.wait_who = "Закупки: договор"
        elif stage == 5:
            r.wait_who = "Поставка / Финансы"
        r.late = bool(r.wait_days is not None and r.wait_days > 2 and stage in (1, 2, 3)) or m.status_code == "problem"
        r.question = any(q.is_open for q in m.questions.all())
        # задача текущего пользователя
        u = self.user
        owner = m.initiator_id == u.pk
        if owner and m.state == Memo.State.DRAFT:
            r.task, r.task_code = "Отправить", "send"
        elif owner and r.question:
            r.task, r.task_code = "Ответить на вопрос", "answer"
        elif owner and DecisionLine.objects.filter(procurement_line__item__memo=m,
                                                   analog_state=DecisionLine.AnalogState.PENDING).exists():
            r.task, r.task_code = "Подтвердить аналог", "analog"
        elif owner and any(i.status == S.DELIVERED for i in items):
            r.task, r.task_code = "Подтвердить получение", "receive"
        elif can_approve(u, m):
            r.task, r.task_code = "Согласовать СЗ", "approve_memo"
        if not self.prices:
            r.amount = None
            r.dot = "n"
        return r

    # --- закупка
    def proc(self, p):
        stage, state = proc_stage(p)
        lines = [l for l in p.lines.all() if l.state in (ProcurementLine.State.ACTIVE, ProcurementLine.State.DONE)]
        memos = sorted({l.item.memo.number for l in lines})
        dls = list(p.decision_lines.all())
        amount = sum((d.amount for d in dls), ZERO) if dls else None
        r = Row(key=f"p{p.pk}", kind="proc", obj=p, number=f"З-{p.number}", title=p.title,
                sub=f"{p.get_method_display()} · {len(lines)} поз.",
                who=p.buyer.get_full_name(), who_sub=("СЗ " + ", ".join(f"№{n}" for n in memos[:4])
                                                      + ("…" if len(memos) > 4 else "")) if memos else "",
                stage=stage, state=state, amount=amount, station=str(p.station) if p.station else "несколько",
                sort_date=p.created_at)
        sup = sorted({d.quote_line.quote.rfq.supplier.name for d in dls})
        r.supplier = ", ".join(sup) if sup else "не определён"
        r.dot = p.corridor.color if p.corridor else corridor_color(amount, p.method == Procurement.Method.CONTRACT)
        pending = [a for a in p.approvals.all() if a.state == DecisionApproval.State.PENDING]
        if stage == 2 and not state:
            r.wait_days = _days(p.created_at)
            r.wait_who = "Закупки: расценка"
        elif stage == 3:
            r.wait_days = _days(p.decision_submitted_at)
            r.wait_who = ", ".join(roles.SHORT.get(a.role, a.role) for a in pending)
            r.late = any(a.is_overdue for a in pending)
        elif stage == 4:
            r.wait_who = "Закупки: договор"
        elif stage == 5:
            r.wait_who = "Поставка / Финансы"
        if stage == 2 and r.wait_days and r.wait_days > 5:
            r.late = True
        r.question = any(q.is_open for q in p.questions.all())
        mine = p.buyer_id == self.user.pk or roles.has_role(self.user, roles.ADMIN)
        if p.pk in self.my_approvals:
            r.task, r.task_code = "Согласовать", "approve"
        elif mine and r.question:
            r.task, r.task_code = "Ответить на вопрос", "answer"
        elif mine and WithdrawalRequest.objects.filter(procurement=p, state=WithdrawalRequest.State.PENDING).exists():
            r.task, r.task_code = "Запрос на отзыв", "withdrawal"
        elif mine and stage == 2 and not state and p.decision_state != Procurement.DecisionState.ON_APPROVAL:
            r.task, r.task_code = "Расценка", "price"
        elif mine and stage == 4:
            r.task, r.task_code = "Договор", "contract"
        r.extra = {"approvals": list(p.approvals.all()), "pending": pending}
        return r

    # --- договор
    def contract(self, c):
        stage, state = contract_stage(c)
        amount = c.amount if c.kind != Contract.Kind.FRAMEWORK else None
        r = Row(key=f"c{c.pk}", kind="contract", obj=c, number=c.number, title=f"{c.get_kind_display()} · {c.supplier.name}",
                sub=(f"закупка №{c.procurement.number}" if c.procurement else "без закупки"),
                who=c.responsible.get_full_name() if c.responsible else "—",
                who_sub=", ".join(f"СЗ-{m.number}" for m in c.memos[:3]),
                stage=stage, state=state, amount=amount, supplier=c.supplier.name, sort_date=c.created_at,
                station=str(c.procurement.station) if c.procurement and c.procurement.station else "—")
        r.paid = c.paid_total if amount else None
        r.dot = corridor_color(amount) if amount else "b"
        if stage == 4 and not state:
            r.wait_who = "Подписание"
            r.wait_days = _days(c.created_at)
        elif stage == 5:
            r.wait_who = "Поставка / оплата"
        if self.is_finance and c.status in (C.SIGNED, C.EXECUTION, C.EXECUTED) and amount and c.paid_total < amount:
            r.task, r.task_code = "Оплата", "pay"
        elif self.is_buyer and c.status in (C.DRAFT, C.SIGNING):
            r.task, r.task_code = "Подписание", "sign"
        return r


# ---------------------------------------------------------------- выборка реестра


def visible_querysets(user):
    from .views.memos import visible_memos
    memos = visible_memos(user).prefetch_related(
        "items__procurement_lines__procurement", "items__contract_lines__contract__supplier", "questions",
        "items__category",
    ).select_related("station", "department", "initiator", "approver", "category")
    procs = Procurement.objects.none()
    contracts = Contract.objects.none()
    if roles.can(user, "procurement.view"):
        procs = Procurement.objects.select_related("buyer", "corridor", "station").prefetch_related(
            "lines__item__memo", "approvals", "questions", "contracts",
            "decision_lines__quote_line__quote__rfq__supplier")
    if roles.can(user, "contract.view"):
        contracts = Contract.objects.exclude(kind=Contract.Kind.FRAMEWORK).select_related(
            "supplier", "procurement__station", "responsible").prefetch_related("lines", "payments")
    return memos, procs, contracts


def search_filter(qs, kind, q):
    if not q:
        return qs
    num = int(q) if q.isdigit() else None
    if kind == "memo":
        cond = (Q(title__icontains=q) | Q(justification__icontains=q) | Q(items__description__icontains=q)
                | Q(initiator__last_name__icontains=q) | Q(department__name__icontains=q)
                | Q(items__contract_lines__contract__supplier__name__icontains=q))
        if num:
            cond |= Q(number=num)
        if q.upper().startswith("СЗ-") and q[3:].isdigit():
            cond |= Q(number=int(q[3:]))
    elif kind == "proc":
        cond = (Q(title__icontains=q) | Q(lines__item__description__icontains=q)
                | Q(rfqs__supplier__name__icontains=q) | Q(buyer__last_name__icontains=q))
        if num:
            cond |= Q(number=num) | Q(lines__item__memo__number=num)
        if q.upper().startswith("З-") and q[2:].isdigit():
            cond |= Q(number=int(q[2:]))
    else:
        cond = Q(number__icontains=q) | Q(supplier__name__icontains=q) | Q(lines__item__description__icontains=q)
        if num:
            cond |= Q(lines__item__memo__number=num)
    return qs.filter(cond).distinct()


def build_rows(user, f="my", q="", station=None, limit=300):
    memos, procs, contracts = visible_querysets(user)
    q = (q or "").strip()
    if station:
        memos = memos.filter(station_id=station)
        procs = procs.filter(Q(station_id=station) | Q(lines__item__memo__station_id=station)).distinct()
        contracts = contracts.filter(lines__item__memo__station_id=station).distinct()
    memos, procs, contracts = (search_filter(memos, "memo", q), search_filter(procs, "proc", q),
                               search_filter(contracts, "contract", q))
    # Свежие и незакрытые — первыми; закрытые подгружаем только когда просят.
    if f not in ("done", "all"):
        memos = memos.exclude(status_code__in=["done", "rejected"])
        procs = procs.exclude(status__in=[P.CLOSED, P.CANCELLED])
        contracts = contracts.exclude(status__in=[C.CLOSED, C.CANCELLED])
    if f == "memos":
        procs, contracts = procs.none(), contracts.none()
    elif f == "procs":
        memos, contracts = memos.none(), contracts.none()
    elif f == "contracts":
        memos, procs = memos.none(), procs.none()
    elif f == "red":
        memos, contracts = memos.none(), contracts.none()
        procs = procs.filter(corridor__color="r")
    elif f in ("myappr", "overdue"):
        memos, contracts = memos.none(), contracts.none()
    b = Builder(user)
    rows = [b.memo(m) for m in memos.order_by("-created_at")[:limit]]
    rows += [b.proc(p) for p in procs.order_by("-created_at")[:limit]]
    rows += [b.contract(c) for c in contracts.order_by("-created_at")[:limit]]
    if f == "my":
        rows = [r for r in rows if r.task]
    elif f == "late":
        rows = [r for r in rows if r.late and not r.done]
    elif f == "done":
        rows = [r for r in rows if r.done]
    elif f == "myappr":
        rows = [r for r in rows if r.task_code == "approve"]
    elif f == "overdue":
        rows = [r for r in rows if r.kind == "proc" and r.stage == 3 and r.late]
    rows.sort(key=lambda r: (not bool(r.task), r.done, -(r.sort_date.timestamp() if r.sort_date else 0)))
    return rows


def my_task_count(user):
    return len(build_rows(user, "my"))


# ---------------------------------------------------------------- KPI, обзор руководителя, сводка по станциям


def kpi_values(user, station=None):
    year = timezone.localdate().year
    memos, procs, contracts = visible_querysets(user)
    if station:
        memos = memos.filter(station_id=station)
        procs = procs.filter(station_id=station)
        contracts = contracts.filter(procurement__station_id=station)
    active_procs = procs.filter(decision_state__in=[Procurement.DecisionState.ON_APPROVAL,
                                                    Procurement.DecisionState.APPROVED]).exclude(
        status__in=[P.CLOSED, P.CANCELLED])
    active_sum = sum((d.amount for p in active_procs for d in p.decision_lines.all()), ZERO)
    signed = contracts.filter(status__in=[C.SIGNED, C.EXECUTION, C.EXECUTED])
    signed_sum = sum((c.amount for c in signed), ZERO)
    paid_signed = sum((c.paid_total for c in signed), ZERO)
    paid_year = Payment.objects.filter(contract__in=contracts, date__year=year).aggregate(s=Sum("amount"))["s"] or ZERO
    work_memos = memos.exclude(status_code__in=["done", "rejected", "draft"]).count()
    work_procs = procs.exclude(status__in=[P.CLOSED, P.CANCELLED]).count()
    late_appr = DecisionApproval.objects.filter(state=DecisionApproval.State.PENDING, due_at__lt=timezone.now(),
                                                procurement__in=procs).values("procurement").distinct().count()
    late_items = memos.filter(status_code="problem").count()
    return {
        "work": (f"{work_memos + work_procs}", f"СЗ: {work_memos} · закупок: {work_procs}"),
        "active": (_money_short(active_sum), f"{active_procs.count()} закупок на согласовании и в договоре"),
        "paid": (_money_short(paid_year), f"{year} год"),
        "rest": (_money_short(max(signed_sum - paid_signed, ZERO)),
                 f"из {_money_short(signed_sum)} по подписанным договорам"),
        "late": (f"{late_appr + late_items}", f"согласований: {late_appr} · СЗ с просрочкой: {late_items}"),
    }


def _money_short(v):
    v = Decimal(v or 0)
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f} млрд ₸".replace(".", ",")
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн ₸".replace(".", ",")
    return f"{v:,.0f} ₸".replace(",", " ")


def director_tiles(user):
    my = services.approvals_for(user).count()
    red = Procurement.objects.filter(corridor__color="r").exclude(status__in=[P.CLOSED, P.CANCELLED]).count()
    overdue = DecisionApproval.objects.filter(state=DecisionApproval.State.PENDING, due_at__lt=timezone.now(),
                                              procurement__decision_state=Procurement.DecisionState.ON_APPROVAL
                                              ).values("procurement").distinct().count()
    return [
        ("myappr", "На моём согласовании", my, "закупки ждут вашего решения"),
        ("red", "Красный коридор", red, "крупные закупки в работе (> 50 млн ₸)"),
        ("overdue", "Просроченные согласования", overdue, "срок по коридору истёк"),
    ]


def station_analytics(user):
    out = []
    for st in Station.objects.all():
        contracts = Contract.objects.exclude(status=C.CANCELLED).exclude(kind=Contract.Kind.FRAMEWORK).filter(
            procurement__station=st)
        total = sum((c.amount for c in contracts), ZERO)
        paid = sum((c.paid_total for c in contracts), ZERO)
        active = Memo.objects.filter(station=st).exclude(status_code__in=["done", "rejected", "draft"]).count()
        out.append({"station": st, "total": _money_short(total), "paid_pct": round(float(paid / total * 100)) if total else 0,
                    "active": active, "paid": _money_short(paid)})
    return out


def stage_counts(rows):
    counts = [0] * len(STEPS)
    for r in rows:
        if not r.state:
            counts[r.stage] += 1
    return list(zip(STEPS, counts))
