"""Пул потребностей (шаг 2) и формирование закупки (шаг 3)."""
from collections import Counter, OrderedDict
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import roles, services
from ..forms import ProcurementForm
from ..models import BudgetItem, Category, Department, MemoItem, Procurement
from .common import attempt, paginate

POOL_PAGE_SIZE = 200


@roles.require("pool.view")
def pool(request):
    qs = services.pool_queryset()
    f = request.GET
    if f.get("category"):
        qs = qs.filter(category_id=f["category"])
    if f.get("department"):
        qs = qs.filter(memo__department_id=f["department"])
    if f.get("budget"):
        qs = qs.filter(budget_item_id=f["budget"])
    if f.get("urgent"):
        qs = qs.filter(urgent=True)
    if f.get("due", "").isdigit():
        qs = qs.filter(required_date__lte=timezone.localdate() + timedelta(days=int(f["due"])))
    if f.get("q"):
        q = f["q"].strip()
        cond = Q(description__icontains=q) | Q(nomenclature__name__icontains=q)
        if q.isdigit():
            cond |= Q(memo__number=int(q))
        qs = qs.filter(cond)
    # Кандидаты на консолидацию: номенклатура, которая есть в пуле из разных СЗ (по всему пулу, а не странице).
    dup_noms = set(
        services.pool_queryset().exclude(nomenclature=None).values("nomenclature")
        .annotate(n=Count("memo", distinct=True)).filter(n__gt=1).values_list("nomenclature", flat=True)
    )
    qs = qs.order_by("-urgent", "required_date", "memo__number", "line_no")
    page, qs_params = paginate(request, qs, POOL_PAGE_SIZE)
    items = list(page)

    stale_days = services.cfg("POOL_STALE_DAYS")
    now = timezone.now()
    for i in items:
        i.is_dup = i.nomenclature_id in dup_noms
        i.days_in_pool = (now - i.pool_since).days if i.pool_since else 0
        i.is_stale = i.days_in_pool >= stale_days

    group = f.get("group", "category")
    groups = OrderedDict()
    for i in sorted(items, key=lambda i: (str(i.category or "яяя") if group == "category" else "")):
        key = (str(i.category) if i.category else "Без категории") if group == "category" else "Все позиции"
        groups.setdefault(key, []).append(i)

    open_procs = Procurement.objects.filter(status__in=[Procurement.Status.DRAFT, Procurement.Status.RFQ,
                                                        Procurement.Status.COLLECTING])
    return render(request, "core/pool.html", {
        "groups": groups, "count": page.paginator.count, "page": page, "qs_params": qs_params,
        "dup_count": services.pool_queryset().filter(nomenclature__in=dup_noms).count(),
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
