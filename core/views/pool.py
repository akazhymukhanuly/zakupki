"""Пул потребностей (шаг 2) и формирование закупки (шаг 3)."""
from collections import Counter, OrderedDict
from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import roles, services
from ..forms import ProcurementForm
from ..models import BudgetItem, Category, Department, MemoItem, Procurement
from .common import attempt


@roles.require("pool.view")
def pool(request):
    items = services.pool_items()
    f = request.GET
    if f.get("category"):
        items = [i for i in items if str(i.category_id) == f["category"]]
    if f.get("department"):
        items = [i for i in items if str(i.memo.department_id) == f["department"]]
    if f.get("budget"):
        items = [i for i in items if str(i.budget_item_id) == f["budget"]]
    if f.get("urgent"):
        items = [i for i in items if i.urgent]
    if f.get("due"):
        limit = timezone.localdate() + timedelta(days=int(f["due"]))
        items = [i for i in items if i.required_date <= limit]
    if f.get("q"):
        q = f["q"].lower()
        items = [i for i in items if q in i.description.lower() or q in i.code.lower()]

    # Подсветка одинаковой номенклатуры из разных СЗ — кандидаты на консолидацию.
    memos_by_nom = {}
    for i in items:
        if i.nomenclature_id:
            memos_by_nom.setdefault(i.nomenclature_id, set()).add(i.memo_id)
    dup_noms = {n for n, ms in memos_by_nom.items() if len(ms) > 1}

    stale_days = services.cfg("POOL_STALE_DAYS")
    now = timezone.now()
    for i in items:
        i.is_dup = i.nomenclature_id in dup_noms
        i.days_in_pool = (now - i.pool_since).days if i.pool_since else 0
        i.is_stale = i.days_in_pool >= stale_days

    items.sort(key=lambda i: (not i.urgent, i.required_date, i.memo.number, i.line_no))
    group = f.get("group", "category")
    groups = OrderedDict()
    for i in sorted(items, key=lambda i: (str(i.category or "яяя") if group == "category" else "")):
        key = (str(i.category) if i.category else "Без категории") if group == "category" else "Все позиции"
        groups.setdefault(key, []).append(i)

    open_procs = Procurement.objects.filter(status__in=[Procurement.Status.DRAFT, Procurement.Status.RFQ,
                                                        Procurement.Status.COLLECTING])
    return render(request, "core/pool.html", {
        "groups": groups, "count": len(items), "dup_count": sum(1 for i in items if i.is_dup),
        "categories": Category.objects.all(), "departments": Department.objects.all(),
        "budgets": BudgetItem.objects.all(), "f": f, "group": group,
        "form": ProcurementForm(initial={"buyer": request.user, "kp_deadline": timezone.localdate() + timedelta(days=7)}),
        "open_procs": open_procs,
    })


@roles.require("procurement.manage")
@require_POST
def pool_create_procurement(request):
    ids = [int(x) for x in request.POST.getlist("items")]
    items = list(MemoItem.objects.filter(pk__in=ids))
    if not items:
        messages.error(request, "Отметьте позиции в пуле.")
        return redirect("pool")
    target = request.POST.get("target")
    if target and target != "new":
        proc = get_object_or_404(Procurement, pk=target)
        ok, _ = attempt(request, lambda: services.add_items_to_procurement(proc, items, request.user),
                        f"Добавлено позиций в закупку №{proc.number}: {len(items)}")
        return redirect(proc) if ok else redirect("pool")
    form = ProcurementForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте параметры закупки: " + "; ".join(e for errs in form.errors.values() for e in errs))
        return redirect("pool")
    d = form.cleaned_data
    title = d["title"] or _auto_title(items)
    ok, proc = attempt(request, lambda: services.create_procurement(
        request.user, items, title, d["method"], d["kp_deadline"], d["buyer"]), "Закупка создана")
    if ok:
        return redirect(proc)
    return redirect("pool")


def _auto_title(items):
    cats = Counter(str(i.category) for i in items if i.category)
    if cats:
        return ", ".join(c for c, _ in cats.most_common(2))
    return items[0].description[:80]
