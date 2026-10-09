"""Главный экран «реестр + карточка» (формат макета ГРЭС) и его действия."""
import json
from urllib.parse import urlencode
import mimetypes
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import roles, services, workspace
from ..models import (
    Attachment, BudgetItem, Category, Contract, Corridor, DecisionApproval, DecisionLine, Memo, MemoItem,
    Nomenclature, Procurement, ProcurementLine, Question, Station, Supplier, WithdrawalRequest,
)
from .common import attempt, dec
from .memos import can_approve, can_edit, visible_memos

UNITS = ["шт", "компл.", "кг", "т", "м", "л", "упак.", "пач", "усл."]


def back(request, key=None):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and nxt.startswith("/"):
        return redirect(nxt)
    return redirect(f"/?sel={key}" if key else "/")


# ---------------------------------------------------------------- главный экран


@login_required
def home(request):
    u = request.user
    f = request.GET.get("f", "my")
    q = request.GET.get("q", "")
    station = request.GET.get("station") or None
    rows = workspace.build_rows(u, f, q, station)
    task_count = len(workspace.build_rows(u, "my")) if f != "my" else len(rows)
    sel = request.GET.get("sel")
    if not sel and rows and f == "my":
        sel = rows[0].key
    card = _card_context(request, sel) if sel else None
    kpis = workspace.visible_kpis(u)
    ctx = {
        "rows": rows, "f": f, "q": q, "station": station, "sel": sel, "card": card,
        "stations": Station.objects.all(), "task_count": task_count,
        "kpis": [(k, label, *workspace.kpi_values(u, station)[k]) for k, label in workspace.KPIS if k in kpis],
        "director": workspace.director_tiles(u) if roles.can(u, "director.overview") else None,
        "analytics": workspace.station_analytics(u) if roles.can(u, "director.overview") and not station else None,
        "stage_counts": workspace.stage_counts(rows),
        "steps": workspace.STEPS,
        "sees_prices": roles.sees_prices(u),
        "green_count": services.approvals_for(u).filter(procurement__corridor__color="g").count(),
        "pool_count": services.pool_queryset().count() if roles.can(u, "pool.view") else 0,
        "new_form": _new_form_context(u),
        "chips": _chips(u),
        "base_qs": urlencode({k: v for k, v in (("f", f), ("q", q), ("station", station or "")) if v}),
        "open_new": request.GET.get("new") == "1",
        "card_next": f"/?sel={sel}" + (f"&{urlencode({k: v for k, v in (('f', f), ('q', q), ('station', station or '')) if v})}" if f or q else ""),
        "src_memo": _src_memo(request),
    }
    return render(request, "core/workspace.html", ctx)


def _src_memo(request):
    """«Повторить закупку»: форма новой потребности заполняется позициями выбранной СЗ."""
    src = request.GET.get("repeat")
    if not src or not src.isdigit():
        return None
    return visible_memos(request.user).filter(pk=src).prefetch_related("items").first()


def _chips(u):
    chips = [("my", "Мои задачи"), ("all", "Все")]
    chips.append(("memos", "СЗ"))
    if roles.can(u, "procurement.view"):
        chips.append(("procs", "Закупки"))
    if roles.can(u, "contract.view"):
        chips.append(("contracts", "Договоры"))
    chips += [("late", "Просроченные"), ("done", "Закрытые")]
    return chips


def _new_form_context(u):
    prof = getattr(u, "profile", None)
    return {
        "units": UNITS,
        "budgets": BudgetItem.objects.all(),
        "categories": Category.objects.all(),
        "stations": Station.objects.all(),
        "station": prof.station_id if prof else None,
        "department": prof.department if prof else None,
        "noms": list(Nomenclature.objects.values_list("name", flat=True)[:2000]),
        "default_date": (timezone.localdate() + timedelta(days=21)).isoformat(),
    }


def _card_context(request, key):
    u = request.user
    kind, _, pk = key[:1], None, key[1:]
    if not pk.isdigit():
        return None
    pk = int(pk)
    b = workspace.Builder(u)
    if kind == "m":
        memo = Memo.objects.filter(pk=pk).first()
        if not memo or not visible_memos(u).filter(pk=pk).exists():
            return None
        memo = visible_memos(u).prefetch_related(
            "items__procurement_lines__procurement", "items__contract_lines__contract__supplier", "questions",
        ).get(pk=pk)
        row = b.memo(memo)
        items = list(memo.items.select_related("nomenclature", "category").prefetch_related(
            "contract_lines__contract", "procurement_lines__procurement"))
        return {
            "kind": "memo", "row": row, "memo": memo, "items": items,
            "is_owner": memo.initiator_id == u.pk or roles.has_role(u, roles.ADMIN),
            "can_edit": can_edit(u, memo), "can_approve": can_approve(u, memo),
            "questions": memo.questions.select_related("asked_by", "answered_by"),
            "open_question": next((x for x in memo.questions.all() if x.is_open), None),
            "attachments": [a for a in memo.attachments.all() if roles.sees_prices(u) or not a.has_prices],
            "feed": memo.history.select_related("user").order_by("created_at"),
            "analogs": DecisionLine.objects.filter(procurement_line__item__memo=memo,
                                                   analog_state=DecisionLine.AnalogState.PENDING
                                                   ).select_related("procurement_line__item", "quote_line__quote__rfq__supplier"),
            "budget": memo.budget_item or next((i.budget_item for i in items if i.budget_item), None),
            "can_ask": roles.can(u, "memo.approve") or roles.can(u, "procurement.manage") or can_approve(u, memo),
        }
    if kind == "p":
        if not roles.can(u, "procurement.view"):
            return None
        proc = Procurement.objects.select_related("buyer", "corridor", "station").filter(pk=pk).first()
        if not proc:
            return None
        row = b.proc(proc)
        lines = list(proc.lines.select_related("item__memo__initiator", "item__memo__station").prefetch_related(
            "decision_lines__quote_line__quote__rfq__supplier"))
        my_appr = services.approvals_for(u).filter(procurement=proc).first()
        manage = roles.can(u, "procurement.manage")
        corridors = [{"code": c.code, "name": c.name, "emoji": c.emoji, "max": float(c.max_amount) if c.max_amount else None,
                      "roles": ", ".join(roles.SHORT.get(r, r) for r in c.role_list), "sla": c.sla_days,
                      "by_contract": c.by_contract} for c in Corridor.objects.all()]
        return {
            "kind": "proc", "row": row, "proc": proc, "lines": lines, "manage": manage,
            "decision_lines": list(proc.decision_lines.select_related(
                "procurement_line__item__memo", "quote_line__quote__rfq__supplier")),
            "approvals": proc.approvals.select_related("user"),
            "my_approval": my_appr,
            "questions": proc.questions.select_related("asked_by", "answered_by"),
            "open_question": next((x for x in proc.questions.all() if x.is_open), None),
            "attachments": proc.attachments.all(),
            "contracts": proc.contracts.select_related("supplier").prefetch_related("lines", "payments"),
            "feed": proc.history.select_related("user").order_by("created_at"),
            "suppliers_fit": _suppliers_fit(lines),
            "suppliers_other": Supplier.objects.exclude(pk__in=[x.pk for x in _suppliers_fit(lines)]),
            "corridors_json": json.dumps(corridors, ensure_ascii=False),
            "can_quick": manage and proc.is_simple_method and proc.status in (
                Procurement.Status.DRAFT, Procurement.Status.RFQ, Procurement.Status.COLLECTING, Procurement.Status.ANALYSIS)
                and proc.decision_state != Procurement.DecisionState.ON_APPROVAL,
            "can_submit": manage and proc.decision_lines.exists() and proc.status in (
                Procurement.Status.RFQ, Procurement.Status.COLLECTING, Procurement.Status.ANALYSIS)
                and proc.decision_state in (Procurement.DecisionState.NONE, Procurement.DecisionState.REJECTED),
            "proposals": services.contract_proposals(proc) if proc.decision_state == Procurement.DecisionState.APPROVED and manage else [],
            "withdrawals": WithdrawalRequest.objects.filter(procurement=proc, state=WithdrawalRequest.State.PENDING),
            "methods": Procurement.Method.choices,
        }
    if kind == "c":
        if not roles.can(u, "contract.view"):
            return None
        c = Contract.objects.select_related("supplier", "procurement", "parent").filter(pk=pk).first()
        if not c:
            return None
        return {
            "kind": "contract", "row": b.contract(c), "c": c,
            "lines": list(c.lines.select_related("item__memo")),
            "payments": c.payments.all(), "flow": services.CONTRACT_FLOW.get(c.status, []),
            "status_labels": dict(Contract.Status.choices),
            "manage": roles.can(u, "contract.manage"), "finance": roles.can(u, "contract.finance"),
            "attachments": c.attachments.all(),
            "feed": c.history.select_related("user").order_by("created_at"),
            "rest": max(c.amount - c.paid_total, Decimal(0)) if c.amount else Decimal(0),
            "today": timezone.localdate(),
        }
    return None


def _suppliers_fit(lines):
    """Поставщики, закрывающие категории позиций закупки — в начало списка."""
    cats = {l.item.category_id for l in lines if l.item.category_id}
    return list(Supplier.objects.filter(categories__in=cats).distinct()) if cats else []


# ---------------------------------------------------------------- новая потребность (как в макете: позиции без цен)


@login_required
@require_POST
def new_memo(request):
    u = request.user
    if not roles.can(u, "memo.create"):
        raise PermissionDenied("Нет права создавать потребность")
    p = request.POST
    names, qtys, units, specs = p.getlist("item_name"), p.getlist("item_qty"), p.getlist("item_unit"), p.getlist("item_spec")
    items = []
    for n, qv, un, sp in zip(names, qtys, units, specs):
        n = n.strip()
        if not n and not qv:
            continue
        try:
            qty = dec(qv)
        except services.BusinessError:
            qty = None
        if not n or not qty or qty <= 0:
            messages.error(request, "Укажите наименование и количество по каждой позиции.")
            return redirect("/?new=1")
        items.append((n, qty, un or "шт", sp.strip()))
    if not items:
        messages.error(request, "Добавьте хотя бы одну позицию.")
        return redirect("/?new=1")
    prof = getattr(u, "profile", None)
    dept = prof.department if prof else None
    if not dept:
        messages.error(request, "У вашего пользователя не указано подразделение — обратитесь к администратору.")
        return redirect("/")
    try:
        required = date.fromisoformat(p.get("deliv")) if p.get("deliv") else timezone.localdate() + timedelta(days=21)
    except ValueError:
        required = timezone.localdate() + timedelta(days=21)
    budget = BudgetItem.objects.filter(pk=p.get("budget") or 0).first()
    cat = Category.objects.filter(pk=p.get("cat") or 0).first()
    station = Station.objects.filter(pk=p.get("station") or 0).first() or (prof.station if prof else None)
    memo = Memo.objects.create(
        department=dept, initiator=u, station=station, title=p.get("name", "").strip()[:300],
        justification=p.get("why", "").strip() or "—", required_date=required, budget_item=budget, category=cat,
    )
    noms = {n.name.lower(): n for n in Nomenclature.objects.filter(name__in=[i[0] for i in items])}
    for no, (n, qty, un, sp) in enumerate(items, 1):
        nom = noms.get(n.lower())
        MemoItem.objects.create(memo=memo, line_no=no, nomenclature=nom, description=n, spec=sp, quantity=qty,
                                unit=un, required_date=required, budget_item=budget,
                                category=cat or (nom.category if nom else None), urgent=bool(p.get("urgent")))
    src = p.get("src")
    services.log((f"создал потребность на основе СЗ-{src}" if src else "создал потребность")
                 + f" ({len(items)} поз.). Цены не указывались", u, memo=memo)
    if request.FILES.get("file"):
        services.add_attachment(u, request.FILES["file"], Attachment.Kind.MEMO, memo=memo)
    if p.get("send"):
        ok, _ = attempt(request, lambda: services.submit_memo(memo, u), f"{memo} отправлена на согласование")
    else:
        services.recalc_memo(memo)
        messages.success(request, f"Черновик {memo} сохранён")
    return redirect(f"/?sel=m{memo.pk}&f=all")


# ---------------------------------------------------------------- вопросы, комментарии, файлы


def _target(request, key):
    """m12 / p5 / c3 → объект с проверкой доступа."""
    u = request.user
    kind, pk = key[:1], key[1:]
    if not pk.isdigit():
        raise Http404
    if kind == "m":
        if not visible_memos(u).filter(pk=pk).exists():
            raise PermissionDenied
        return {"memo": Memo.objects.get(pk=pk)}
    if kind == "p" and roles.can(u, "procurement.view"):
        return {"procurement": get_object_or_404(Procurement, pk=pk)}
    if kind == "c" and roles.can(u, "contract.view"):
        return {"contract": get_object_or_404(Contract, pk=pk)}
    raise PermissionDenied


@login_required
@require_POST
def ask(request, key):
    t = _target(request, key)
    if "contract" in t:
        raise Http404
    attempt(request, lambda: services.ask_question(request.user, request.POST.get("text", ""), **t),
            "Вопрос отправлен — согласования других участников не сбрасываются")
    return back(request, key)


@login_required
@require_POST
def answer(request, pk):
    q = get_object_or_404(Question, pk=pk)
    key = f"m{q.memo_id}" if q.memo_id else f"p{q.procurement_id}"
    _target(request, key)
    attempt(request, lambda: services.answer_question(q, request.user, request.POST.get("text", "")), "Ответ отправлен")
    return back(request, key)


@login_required
@require_POST
def comment(request, key):
    t = _target(request, key)
    attempt(request, lambda: services.add_comment(request.user, request.POST.get("text", ""), **t))
    return back(request, key)


@login_required
@require_POST
def attach(request, key):
    t = _target(request, key)
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Выберите файл")
        return back(request, key)
    kind = request.POST.get("kind") or Attachment.Kind.OTHER
    attempt(request, lambda: services.add_attachment(request.user, f, kind, **t), f"Файл «{f.name}» прикреплён")
    return back(request, key)


@login_required
def download(request, pk):
    a = get_object_or_404(Attachment, pk=pk)
    key = f"m{a.memo_id}" if a.memo_id else f"p{a.procurement_id}" if a.procurement_id else f"c{a.contract_id}"
    _target(request, key)
    if a.has_prices and not roles.sees_prices(request.user):
        raise PermissionDenied("Документ содержит цены — недоступен для вашей роли")
    ctype = mimetypes.guess_type(a.name)[0] or "application/octet-stream"
    return FileResponse(a.file.open("rb"), content_type=ctype, as_attachment=request.GET.get("dl") == "1",
                        filename=a.name)


# ---------------------------------------------------------------- расценка и согласование


@roles.require("procurement.manage")
@require_POST
def quick_price(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)
    p = request.POST
    sup_id = p.get("supplier")
    supplier = Supplier.objects.filter(pk=sup_id).first() if sup_id and sup_id != "new" else None
    if not supplier:
        name = p.get("supplier_name", "").strip()
        if not name:
            messages.error(request, "Укажите поставщика")
            return back(request, f"p{pk}")
        supplier = Supplier.objects.filter(name=name).first() or Supplier.objects.create(name=name, bin=p.get("bin", "").strip())

    def run():
        prices = {}
        for line in proc.lines.filter(state=ProcurementLine.State.ACTIVE):
            prices[line.pk] = dec(p.get(f"price_{line.pk}"))
        if p.get("method") in dict(Procurement.Method.choices):
            proc.method = p["method"]
            proc.save(update_fields=["method"])
        services.quick_price(proc, supplier, prices, request.user, p.get("pay", "").strip())
        for f in request.FILES.getlist("files"):
            services.add_attachment(request.user, f, Attachment.Kind.QUOTE, procurement=proc)
    attempt(request, run, "Расценка сохранена и отправлена на согласование")
    return back(request, f"p{pk}")


@roles.require("procurement.manage")
@require_POST
def submit_decision(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)
    attempt(request, lambda: services.submit_decision(proc, request.user), "Отправлено на согласование по коридору")
    return back(request, f"p{pk}")


@login_required
@require_POST
def approve(request, pk):
    a = get_object_or_404(DecisionApproval, pk=pk)
    ok = request.POST.get("approve") == "1"
    attempt(request, lambda: services.resolve_decision_approval(a, request.user, ok, request.POST.get("comment", "")),
            "СОГЛАСОВАНО" if ok else "Отклонено")
    return back(request, f"p{a.procurement_id}")


@login_required
@require_POST
def approve_green(request):
    ok, n = attempt(request, lambda: services.approve_all_green(request.user))
    if ok:
        messages.success(request, f"Согласовано зелёных закупок: {n}" if n else "Зелёных задач на согласование нет")
    return back(request)


# ---------------------------------------------------------------- выгрузка реестра


@login_required
def export(request):
    rows = workspace.build_rows(request.user, request.GET.get("f", "all"), request.GET.get("q", ""),
                                request.GET.get("station") or None, limit=5000)
    prices = roles.sees_prices(request.user)
    from .. import excel
    data = [["№", "Тип", "Что закупаем", "Инициатор / закупщик", "Станция", "Сумма, ₸", "Поставщик", "Этап", "Ждёт", "Дней"]]
    kinds = {"memo": "СЗ", "proc": "Закупка", "contract": "Договор"}
    for r in rows:
        data.append([r.number, kinds[r.kind], r.title, r.who, r.station,
                     float(r.amount) if (prices and r.amount is not None) else None, r.supplier,
                     workspace.stage_label(r.stage, r.state), r.wait_who, r.wait_days])
    return excel.rows_response(data, "Реестр_закупок_ГРЭС.xlsx", sheet="Реестр")


# ---------------------------------------------------------------- демо: переключение пользователя


@require_POST
def demo_switch(request):
    """Только в демо-режиме: войти под другим демо-пользователем одним кликом (как селектор «Вы:» в макете)."""
    if not settings.DEMO_MODE:
        raise PermissionDenied
    user = get_object_or_404(User, username=request.POST.get("username"), is_active=True)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("/")


def demo_users():
    if not settings.DEMO_MODE:
        return []
    return User.objects.filter(is_active=True).exclude(username="admin").select_related("profile").order_by("last_name")


# ---------------------------------------------------------------- матрица доступа к показателям


@roles.require("access.manage")
def access_matrix(request):
    access = workspace.kpi_access()
    if request.method == "POST":
        new = {k: [r for r in roles.ALL_ROLES if request.POST.get(f"{k}|{r}")] for k, _ in workspace.KPIS}
        from ..models import AppSetting
        AppSetting.put("kpi_access", new)
        messages.success(request, "Доступ к показателям сохранён")
        return redirect("access_matrix")
    matrix = [(k, label, [(r, r in access[k]) for r in roles.ALL_ROLES]) for k, label in workspace.KPIS]
    return render(request, "core/access_matrix.html", {"matrix": matrix, "roles": roles.ALL_ROLES})
