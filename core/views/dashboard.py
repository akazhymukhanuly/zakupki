"""Главная, дэшборд закупщика, отчёты (п. 9), уведомления, права, портал поставщика."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import mean

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import excel, roles, services
from ..models import (
    RFQ, Contract, ContractLine, DecisionLine, Department, Memo, MemoItem, Notification, Procurement, ProcurementLine,
    Quote, QuoteLine, Supplier,
)
from .common import attempt
from .procurements import _quote_rows, quote_groups

S = MemoItem.Status


@login_required
def home(request):
    u = request.user
    if roles.can(u, "dashboard.buyer"):
        return redirect("dashboard")
    if roles.has_role(u, roles.APPROVER, roles.DIRECTOR, roles.CFO):
        return redirect("approvals")
    if roles.has_role(u, roles.ACCOUNTANT):
        return redirect("contract_list")
    return redirect("memo_list")


@roles.require("dashboard.buyer")
def dashboard(request):
    today = timezone.localdate()
    now = timezone.now()
    stale_days = services.cfg("POOL_STALE_DAYS")
    warn_days = services.cfg("DEADLINE_WARN_DAYS")
    pool = services.pool_items()
    stale = [i for i in pool if i.pool_since and (now - i.pool_since).days >= stale_days]
    for i in stale:
        i.days_in_pool = (now - i.pool_since).days
    urgent = [i for i in pool if i.urgent]
    kp_overdue = [p for p in Procurement.objects.filter(status__in=[Procurement.Status.RFQ, Procurement.Status.COLLECTING],
                                                         kp_deadline__lt=today) if p.is_overdue_kp]
    near = MemoItem.objects.filter(
        required_date__lte=today + timedelta(days=warn_days),
        status__in=[S.APPROVED, S.IN_PROCUREMENT, S.SUPPLIER_SELECTED, S.PARTIALLY_CONTRACTED],
    ).select_related("memo", "memo__department").order_by("required_date")
    my_procs = Procurement.objects.filter(buyer=request.user).exclude(
        status__in=[Procurement.Status.CLOSED, Procurement.Status.CANCELLED]).prefetch_related("lines", "rfqs")
    withdrawals = request.user.procurements.filter(withdrawalrequest__state="pending").distinct()
    counters = {
        "pool": len(pool),
        "urgent": len(urgent),
        "in_procurement": MemoItem.objects.filter(status__in=[S.IN_PROCUREMENT, S.SUPPLIER_SELECTED]).count(),
        "contracted": MemoItem.objects.filter(status__in=[S.CONTRACTED, S.PARTIALLY_DELIVERED]).count(),
        "overdue": sum(1 for i in MemoItem.objects.exclude(status__in=[S.DELIVERED, S.CLOSED, S.WITHDRAWN, S.REJECTED, S.DRAFT])
                       .filter(required_date__lt=today)),
    }
    return render(request, "core/dashboard.html", {
        "stale": stale, "urgent": urgent, "kp_overdue": kp_overdue, "near": near, "my_procs": my_procs,
        "counters": counters, "stale_days": stale_days, "warn_days": warn_days, "withdrawals": withdrawals,
    })


# ---------------------------------------------------------------- отчёты


def _period(request):
    today = timezone.localdate()
    try:
        d_from = date.fromisoformat(request.GET.get("from")) if request.GET.get("from") else date(today.year, 1, 1)
        d_to = date.fromisoformat(request.GET.get("to")) if request.GET.get("to") else today
    except ValueError:
        d_from, d_to = date(today.year, 1, 1), today
    return d_from, d_to


def report_coverage(d_from, d_to):
    items = MemoItem.objects.filter(memo__submitted_at__date__gte=d_from, memo__submitted_at__date__lte=d_to) \
        .exclude(status__in=[S.DRAFT]).select_related("memo__department")
    rows = defaultdict(lambda: {"submitted": 0, "approved": 0, "contracted": 0, "delivered": 0, "overdue": 0})
    for i in items:
        r = rows[i.memo.department.name]
        r["submitted"] += 1
        if i.status not in (S.REJECTED, S.ON_APPROVAL):
            r["approved"] += 1
        if i.status in (S.CONTRACTED, S.PARTIALLY_DELIVERED, S.DELIVERED, S.CLOSED) or \
                (i.status == S.PARTIALLY_CONTRACTED):
            r["contracted"] += 1
        if i.status in (S.DELIVERED, S.CLOSED):
            r["delivered"] += 1
        if i.is_overdue:
            r["overdue"] += 1
    out = []
    for dept, r in sorted(rows.items()):
        r["dept"] = dept
        r["pct"] = round(r["delivered"] / r["approved"] * 100) if r["approved"] else 0
        out.append(r)
    return out


def report_cycle(d_from, d_to):
    lines = ContractLine.objects.filter(contract__date__gte=d_from, contract__date__lte=d_to) \
        .exclude(contract__status=Contract.Status.CANCELLED) \
        .select_related("item__memo", "item__category", "contract__procurement__buyer", "contract")
    by_cat = defaultdict(lambda: {"to_contract": [], "to_delivery": []})
    by_buyer = defaultdict(lambda: {"to_contract": [], "to_delivery": []})
    for l in lines:
        memo = l.item.memo
        if not memo.approved_at:
            continue
        start = timezone.localtime(memo.approved_at).date()
        to_c = (l.contract.date - start).days
        cat = str(l.item.category or "Без категории")
        buyer = l.contract.procurement.buyer.get_full_name() if l.contract.procurement else "—"
        by_cat[cat]["to_contract"].append(to_c)
        by_buyer[buyer]["to_contract"].append(to_c)
        if l.item.delivered_at:
            to_d = (timezone.localtime(l.item.delivered_at).date() - start).days
            by_cat[cat]["to_delivery"].append(to_d)
            by_buyer[buyer]["to_delivery"].append(to_d)

    def summarize(d):
        return [{"name": k, "count": len(v["to_contract"]),
                 "to_contract": round(mean(v["to_contract"]), 1) if v["to_contract"] else None,
                 "to_delivery": round(mean(v["to_delivery"]), 1) if v["to_delivery"] else None}
                for k, v in sorted(d.items())]
    return summarize(by_cat), summarize(by_buyer)


def report_savings(d_from, d_to):
    procs = Procurement.objects.filter(decision_state=Procurement.DecisionState.APPROVED,
                                       created_at__date__gte=d_from, created_at__date__lte=d_to)
    out = []
    for p in procs:
        chosen = Decimal(0)
        avg_total = Decimal(0)
        max_total = Decimal(0)
        last_year = Decimal(0)
        last_year_base = Decimal(0)
        for d in p.decision_lines.select_related("procurement_line__item__nomenclature", "quote_line"):
            line = d.procurement_line
            prices = [ql.price for ql in QuoteLine.objects.filter(procurement_line=line, not_offered=False, price__isnull=False)]
            chosen += d.amount
            if prices:
                avg_total += Decimal(mean(prices)) * d.quantity
                max_total += max(prices) * d.quantity
            nom = line.item.nomenclature_id
            if nom:
                year = p.created_at.year - 1
                prev = [cl.price for cl in ContractLine.objects.filter(
                    item__nomenclature_id=nom, contract__date__year=year).exclude(contract__status=Contract.Status.CANCELLED)]
                if prev:
                    last_year += Decimal(mean(prev)) * d.quantity
                    last_year_base += d.amount
        out.append({
            "proc": p, "chosen": chosen, "avg": avg_total, "max": max_total,
            "vs_avg": avg_total - chosen, "vs_max": max_total - chosen,
            "vs_avg_pct": round(float((avg_total - chosen) / avg_total * 100), 1) if avg_total else None,
            "last_year": last_year, "vs_last_year": (last_year - last_year_base) if last_year else None,
        })
    return out


def report_suppliers(d_from, d_to):
    out = []
    for s in Supplier.objects.all():
        rfqs = RFQ.objects.filter(supplier=s, procurement__created_at__date__gte=d_from, procurement__created_at__date__lte=d_to)
        sent = rfqs.filter(sent_at__isnull=False).count() or rfqs.count()
        answered = Quote.objects.filter(rfq__in=rfqs).count()
        won = DecisionLine.objects.filter(quote_line__quote__rfq__in=rfqs,
                                          procurement__decision_state=Procurement.DecisionState.APPROVED) \
            .values("procurement").distinct().count()
        amount = sum((c.amount for c in Contract.objects.filter(supplier=s, date__gte=d_from, date__lte=d_to)
                      .exclude(status=Contract.Status.CANCELLED)), Decimal(0))
        if sent or answered or amount:
            out.append({"supplier": s, "sent": sent, "answered": answered, "won": won, "amount": amount,
                        "response_rate": round(answered / sent * 100) if sent else 0})
    return sorted(out, key=lambda r: -r["amount"])


@roles.require("reports.view")
def reports(request):
    d_from, d_to = _period(request)
    report = request.GET.get("r", "coverage")
    ctx = {"d_from": d_from, "d_to": d_to, "r": report}
    if report == "coverage":
        ctx["rows"] = report_coverage(d_from, d_to)
    elif report == "cycle":
        ctx["by_cat"], ctx["by_buyer"] = report_cycle(d_from, d_to)
    elif report == "savings":
        ctx["rows"] = report_savings(d_from, d_to)
    elif report == "suppliers":
        ctx["rows"] = report_suppliers(d_from, d_to)
    if request.GET.get("export"):
        return _report_excel(report, ctx)
    return render(request, "core/reports.html", ctx)


def _report_excel(report, ctx):
    if report == "coverage":
        rows = [["Подразделение", "Подано позиций", "Утверждено", "В договоре", "Поставлено", "Просрочено", "% поставки"]]
        rows += [[r["dept"], r["submitted"], r["approved"], r["contracted"], r["delivered"], r["overdue"], r["pct"]] for r in ctx["rows"]]
        title = "Покрытие потребностей"
    elif report == "cycle":
        rows = [["Разрез", "Наименование", "Строк договоров", "Дней до договора", "Дней до поставки"]]
        rows += [["Категория", r["name"], r["count"], r["to_contract"], r["to_delivery"]] for r in ctx["by_cat"]]
        rows += [["Закупщик", r["name"], r["count"], r["to_contract"], r["to_delivery"]] for r in ctx["by_buyer"]]
        title = "Срок цикла"
    elif report == "savings":
        rows = [["Закупка", "Выбрано", "Среднее по КП", "Максимум по КП", "Экономия к среднему", "Экономия к максимуму",
                 "Цена прошлого года", "Экономия к прошлому году"]]
        rows += [[f"№{r['proc'].number} {r['proc'].title}", r["chosen"], r["avg"], r["max"], r["vs_avg"], r["vs_max"],
                  r["last_year"] or None, r["vs_last_year"]] for r in ctx["rows"]]
        title = "Экономия"
    else:
        rows = [["Поставщик", "Запросов получил", "Ответил", "Выиграл закупок", "% ответов", "Сумма договоров"]]
        rows += [[r["supplier"].name, r["sent"], r["answered"], r["won"], r["response_rate"], r["amount"]] for r in ctx["rows"]]
        title = "Активность поставщиков"
    return excel.rows_response(rows, f"{title}.xlsx", title=f"{title} за {ctx['d_from']:%d.%m.%Y}–{ctx['d_to']:%d.%m.%Y}")


# ---------------------------------------------------------------- уведомления, права


@login_required
def notifications(request):
    qs = request.user.notifications.all()[:200]
    return render(request, "core/notifications.html", {"items": qs})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    n.is_read = True
    n.save(update_fields=["is_read"])
    return redirect(n.url or "notifications")


@login_required
@require_POST
def notifications_read_all(request):
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return redirect("notifications")


@login_required
def rights(request):
    matrix = [(roles.PERMISSION_LABELS[p], [r in allowed for r in roles.ALL_ROLES]) for p, allowed in roles.PERMISSIONS.items()]
    extra = [
        ("Инициатор", "видит свои СЗ и СЗ своего подразделения; редактирует только черновик; отзывает свои позиции; "
                      "подтверждает аналоги и получение"),
        ("Согласующий", "согласует СЗ, где он назначен, или СЗ своего подразделения (руководитель); может отклонить отдельные позиции"),
        ("Закупщик", "работает с пулом, закупками, КП, решениями и договорами; обрабатывает запросы на отзыв"),
        ("Директор / Финдиректор", "согласуют решения по закупкам выше порогов (настраиваются в админке), видят всё"),
        ("Бухгалтер", "договоры, оплаты, поступления, обмен с 1С"),
        ("Администратор", "всё + справочники и пользователи в /admin/"),
    ]
    return render(request, "core/rights.html", {"roles": roles.ALL_ROLES, "matrix": matrix, "extra": extra})


@roles.require("dashboard.buyer")
@require_POST
def run_periodic(request):
    res = services.run_periodic()
    messages.success(request, f"Периодические задачи: автозакрыто позиций {res['auto_closed']}, напоминаний {res['reminders']}")
    return redirect("dashboard")


# ---------------------------------------------------------------- портал поставщика


def portal_quote(request, token):
    """Ссылка для поставщика: заполнить КП самому (без входа в систему)."""
    rfq = get_object_or_404(RFQ.objects.select_related("procurement", "supplier"), token=token)
    proc = rfq.procurement
    closed = proc.status not in (Procurement.Status.RFQ, Procurement.Status.COLLECTING, Procurement.Status.ANALYSIS) or \
        proc.decision_state in (Procurement.DecisionState.ON_APPROVAL, Procurement.DecisionState.APPROVED)
    quote = Quote.objects.filter(rfq=rfq).first()
    if request.method == "POST" and not closed:
        from .procurements import _quote_header
        ok, _ = attempt(request, lambda: services.save_quote(
            rfq, _quote_header(request.POST), _quote_rows(proc, request.POST), None, Quote.Source.PORTAL),
            "Спасибо! Ваше предложение получено.")
        return redirect("portal_quote", token=token)
    return render(request, "core/portal_quote.html", {
        "rfq": rfq, "proc": proc, "groups": quote_groups(proc, quote), "quote": quote, "closed": closed,
    })
