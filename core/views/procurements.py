"""Карточка закупки: позиции, запросы КП, ввод КП, сравнение, решение, договоры, история (шаги 3–8)."""
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import excel, roles, services
from ..forms import ProcurementForm, UploadForm
from ..models import (
    RFQ, DecisionApproval, DecisionLine, Procurement, ProcurementLine, Quote, QuoteLine, Supplier, WithdrawalRequest,
)
from .common import attempt, dec, posint

TABS = [("items", "Позиции"), ("suppliers", "Поставщики и запросы"), ("quotes", "КП"), ("compare", "Сравнение"),
        ("decision", "Решение"), ("contracts", "Договоры"), ("history", "История")]


def _steps(proc):
    """Этапы закупки для степпера: пройденные / текущий / будущие."""
    flow = [s for s in Procurement.Status if s != Procurement.Status.CANCELLED]
    if proc.status == Procurement.Status.CANCELLED:
        return [(s.label, "") for s in flow] + [("Отменена", "cancelled")]
    idx = flow.index(proc.status)
    return [(s.label, "done" if i < idx else "current" if i == idx else "") for i, s in enumerate(flow)]


def tab_url(proc, tab):
    return reverse("procurement_detail", args=[proc.pk]) + f"?tab={tab}"


@roles.require("procurement.view")
def procurement_list(request):
    qs = Procurement.objects.select_related("buyer").prefetch_related("lines", "rfqs")
    f = request.GET
    if f.get("status"):
        qs = qs.filter(status=f["status"])
    elif not f.get("all"):
        qs = qs.exclude(status__in=[Procurement.Status.CLOSED, Procurement.Status.CANCELLED])
    if f.get("mine"):
        qs = qs.filter(buyer=request.user)
    return render(request, "core/procurement_list.html", {
        "procurements": qs, "statuses": Procurement.Status.choices, "f": f,
    })


@roles.require("procurement.view")
def procurement_detail(request, pk):
    proc = get_object_or_404(Procurement.objects.select_related("buyer"), pk=pk)
    tab = request.GET.get("tab", "items")
    manage = roles.can(request.user, "procurement.manage")
    ctx = {"proc": proc, "tab": tab, "tabs": TABS, "manage": manage, "steps": _steps(proc)}
    lines = list(proc.lines.select_related("item", "item__memo", "item__memo__department", "item__memo__initiator",
                                           "item__nomenclature", "item__category"))
    ctx["lines"] = lines
    ctx["active_lines"] = [l for l in lines if l.state in (ProcurementLine.State.ACTIVE, ProcurementLine.State.DONE)]
    ctx["withdrawals"] = WithdrawalRequest.objects.filter(procurement=proc, state=WithdrawalRequest.State.PENDING) \
        .select_related("item", "requested_by")
    rfqs = list(proc.rfqs.select_related("supplier").order_by("supplier__name"))
    for r in rfqs:
        r.has_quote = Quote.objects.filter(rfq=r).exists()
    ctx["rfqs"] = rfqs
    ctx["can_edit_lines"] = proc.status in (Procurement.Status.DRAFT, Procurement.Status.RFQ, Procurement.Status.COLLECTING)
    ctx["can_edit_decision"] = proc.status in (Procurement.Status.RFQ, Procurement.Status.COLLECTING, Procurement.Status.ANALYSIS) \
        and proc.decision_state != Procurement.DecisionState.ON_APPROVAL

    if tab == "suppliers":
        cats = {l.item.category_id for l in ctx["active_lines"] if l.item.category_id}
        used = {r.supplier_id for r in rfqs}
        cand = Supplier.objects.exclude(pk__in=used).prefetch_related("categories")
        if not request.GET.get("all_suppliers") and cats:
            cand = cand.filter(categories__in=cats).distinct()
        ctx["candidates"] = cand
        ctx["consolidated"] = services.consolidated_rows(proc)
    if tab in ("quotes", "compare", "decision"):
        ctx["cmp"] = services.comparison(proc)
    if tab == "decision":
        existing = defaultdict(Decimal)
        for d in proc.decision_lines.all():
            existing[(d.procurement_line_id, d.quote_line_id)] += d.quantity
        ctx["decision_qty"] = {f"{k[0]}_{k[1]}": v for k, v in existing.items()}
        ctx["decision_lines"] = list(proc.decision_lines.select_related(
            "procurement_line__item__memo", "quote_line__quote__rfq__supplier"))
        per_line = defaultdict(Decimal)
        for d in ctx["decision_lines"]:
            per_line[d.procurement_line_id] += d.quantity
        for row in ctx["cmp"]["rows"]:
            row["decided"] = per_line.get(row["line"].pk, Decimal(0))
            for c in row["cells"]:
                c["qty"] = existing.get((row["line"].pk, c["ql"].pk)) if c["ql"] else None
        by_supplier = defaultdict(lambda: {"amount": Decimal(0), "count": 0})
        for d in ctx["decision_lines"]:
            by_supplier[d.supplier]["amount"] += d.amount
            by_supplier[d.supplier]["count"] += 1
        ctx["by_supplier"] = dict(by_supplier)
        ctx["approvals"] = proc.approvals.select_related("user")
        ctx["my_approvals"] = [a for a in proc.approvals.filter(state=DecisionApproval.State.PENDING)
                               if roles.has_role(request.user, a.role, roles.ADMIN)]
    if tab == "contracts":
        ctx["proposals"] = services.contract_proposals(proc) if proc.decision_state == Procurement.DecisionState.APPROVED else []
        ctx["contracts"] = proc.contracts.select_related("supplier", "parent").prefetch_related("lines")
    if tab == "history":
        ctx["history"] = proc.history.select_related("user")
    if tab == "items":
        ctx["edit_form"] = ProcurementForm(instance=proc)
    return render(request, "core/procurement_detail.html", ctx)


@roles.require("procurement.manage")
@require_POST
def procurement_action(request, pk, action):
    proc = get_object_or_404(Procurement, pk=pk)
    u = request.user
    p = request.POST
    tab = p.get("tab", "items")

    if action == "edit":
        title = proc.title
        form = ProcurementForm(p, instance=proc)
        if form.is_valid():
            if not form.cleaned_data["title"]:
                form.instance.title = title
            form.save()
            services.log("Изменены параметры закупки", u, procurement=proc)
            messages.success(request, "Сохранено")
        else:
            messages.error(request, "Ошибка в параметрах закупки")
    elif action == "release":
        line = get_object_or_404(ProcurementLine, pk=p.get("line"), procurement=proc)
        attempt(request, lambda: services.release_line(line, u), f"Позиция {line.item.code} возвращена в пул")
    elif action == "add_suppliers":
        suppliers = Supplier.objects.filter(pk__in=p.getlist("suppliers"))
        attempt(request, lambda: services.add_rfqs(proc, suppliers, u), "Поставщики добавлены")
        tab = "suppliers"
    elif action == "remove_rfq":
        rfq = get_object_or_404(RFQ, pk=p.get("rfq"), procurement=proc)
        if Quote.objects.filter(rfq=rfq).exists():
            messages.error(request, "По запросу уже есть КП — удалить нельзя.")
        else:
            rfq.delete()
            messages.success(request, "Поставщик убран из запроса")
        tab = "suppliers"
    elif action == "send":
        rfqs = proc.rfqs.filter(pk__in=p.getlist("rfqs")) if p.getlist("rfqs") else proc.rfqs.all()
        attempt(request, lambda: services.mark_rfqs_sent(proc, u, list(rfqs)), "Запросы отмечены как отправленные")
        tab = "suppliers"
    elif action == "analysis":
        attempt(request, lambda: services.start_analysis(proc, u), "Закупка переведена на анализ")
        tab = "compare"
    elif action == "preset":
        quote = get_object_or_404(Quote, pk=p["quote"], rfq__procurement=proc) if p.get("quote") else None
        mode = "single" if quote else "best"
        attempt(request, lambda: services.decision_preset(proc, mode, u, quote), "Решение заполнено")
        tab = "decision"
    elif action == "decision":
        tab = "decision"

        def build():
            entries = []
            for key, val in p.items():
                if not key.startswith("qty_"):
                    continue
                qty = dec(val)
                if not qty:
                    continue
                line_id, ql_id = map(int, key[4:].split("_"))
                line = ProcurementLine.objects.get(pk=line_id, procurement=proc)
                ql = QuoteLine.objects.select_related("quote__rfq").get(pk=ql_id, quote__rfq__procurement=proc)
                entries.append((line, ql, qty))
            services.set_decision(proc, entries, u, allow_over=bool(p.get("allow_over")))
        attempt(request, build, "Решение сохранено")
    elif action == "submit_decision":
        attempt(request, lambda: services.submit_decision(proc, u), "Решение отправлено")
        tab = "decision"
    elif action == "contracts":
        tab = "contracts"
        choices = {}
        for key, val in p.items():
            if key.startswith("mode_"):
                sid = int(key[5:])
                choices[sid] = {"mode": val, "number": p.get(f"number_{sid}", "")}
        attempt(request, lambda: services.create_contracts(proc, u, choices, allow_over=bool(p.get("allow_over"))),
                "Договоры созданы")
    elif action == "close":
        attempt(request, lambda: services.close_procurement(proc, u), "Закупка закрыта")
    elif action == "cancel":
        attempt(request, lambda: services.cancel_procurement(proc, u, p.get("reason", "")),
                "Закупка отменена, позиции возвращены в пул")
    elif action == "withdrawal":
        req = get_object_or_404(WithdrawalRequest, pk=p.get("req"), procurement=proc)
        attempt(request, lambda: services.resolve_withdrawal(req, u, p.get("approve") == "1"), "Запрос на отзыв обработан")
    else:
        raise Http404
    return redirect(tab_url(proc, tab))


@roles.require("decision.approve")
@require_POST
def decision_approve(request, pk):
    approval = get_object_or_404(DecisionApproval, pk=pk)
    approve = request.POST.get("approve") == "1"
    attempt(request, lambda: services.resolve_decision_approval(approval, request.user, approve,
                                                                  request.POST.get("comment", "")),
            "Решение согласовано" if approve else "Решение отклонено")
    return redirect(tab_url(approval.procurement, "decision"))


# ---------------------------------------------------------------- ввод КП (шаг 5)


def quote_groups(proc, quote=None):
    """Строки формы КП: консолидированные группы (п. 6.1) с текущими значениями."""
    existing = {}
    if quote:
        existing = {ql.procurement_line_id: ql for ql in quote.lines.all()}
    groups = services.consolidated_rows(proc)
    for g in groups:
        first = next((existing[l.pk] for l in g["lines"] if l.pk in existing), None)
        g["ql"] = first
        g["id"] = g["lines"][0].pk
    return groups


@roles.require("procurement.manage")
def quote_entry(request, pk, rfq_pk):
    proc = get_object_or_404(Procurement, pk=pk)
    rfq = get_object_or_404(RFQ, pk=rfq_pk, procurement=proc)
    quote = Quote.objects.filter(rfq=rfq).first()
    if request.method == "POST":
        if "file" in request.FILES:
            def do_import():
                rows = excel.parse_quote_workbook(request.FILES["file"])
                services.save_quote(rfq, {}, rows, request.user, Quote.Source.EXCEL)
            ok, _ = attempt(request, do_import, "КП загружено из Excel")
        else:
            ok, _ = attempt(request, lambda: services.save_quote(
                rfq, _quote_header(request.POST), _quote_rows(proc, request.POST), request.user, Quote.Source.MANUAL),
                f"КП {rfq.supplier} сохранено")
        if ok:
            nxt = request.POST.get("next_rfq")
            if nxt:
                return redirect("quote_entry", pk=proc.pk, rfq_pk=nxt)
            return redirect(tab_url(proc, "quotes"))
        return redirect("quote_entry", pk=proc.pk, rfq_pk=rfq.pk)
    others = list(proc.rfqs.exclude(pk=rfq.pk).select_related("supplier"))
    return render(request, "core/quote_entry.html", {
        "proc": proc, "rfq": rfq, "quote": quote, "groups": quote_groups(proc, quote), "others": others,
        "upload": UploadForm(),
    })


def _quote_header(p):
    from ..onec import _to_date
    return {
        "valid_until": _to_date(p["valid_until"]) if p.get("valid_until") else None,
        "payment_terms": p.get("payment_terms", ""),
        "comment": p.get("comment", ""),
    }


def _quote_rows(proc, p):
    """Цена по сводной строке применяется ко всем позициям группы; предлагаемое кол-во распределяется по порядку."""
    rows = {}
    for g in services.consolidated_rows(proc):
        gid = g["lines"][0].pk
        price = dec(p.get(f"price_{gid}"))
        offered = dec(p.get(f"offered_{gid}"))
        base = {
            "price": price,
            "lead_time_days": posint(p.get(f"lead_{gid}")),
            "not_offered": bool(p.get(f"no_{gid}")),
            "is_analog": bool((p.get(f"analog_{gid}") or "").strip()),
            "analog_description": (p.get(f"analog_{gid}") or "").strip(),
            "comment": p.get(f"comment_{gid}", ""),
        }
        left = offered
        for line in g["lines"]:
            data = dict(base, offered_qty=None)
            if offered is not None:
                take = max(min(left, line.quantity), Decimal(0))
                if take <= 0:
                    data["not_offered"] = True
                elif take < line.quantity:
                    data["offered_qty"] = take
                left -= take
            rows[line.pk] = data
    return rows


@roles.require("procurement.manage")
def quote_template(request, pk, rfq_pk):
    proc = get_object_or_404(Procurement, pk=pk)
    rfq = get_object_or_404(RFQ, pk=rfq_pk, procurement=proc)
    lines = proc.lines.filter(state=ProcurementLine.State.ACTIVE).select_related("item", "item__memo")
    return excel.response(excel.quote_template(proc, rfq, lines), f"КП_{rfq.supplier.name}_закупка_{proc.number}.xlsx")


@roles.require("procurement.view")
def comparison_export(request, pk):
    proc = get_object_or_404(Procurement, pk=pk)
    return excel.response(excel.comparison_workbook(proc, services.comparison(proc)), f"Сравнение_КП_закупка_{proc.number}.xlsx")


@roles.require("procurement.view")
def rfq_print(request, pk, rfq_pk):
    """Печатная форма запроса КП со сводными строками."""
    proc = get_object_or_404(Procurement, pk=pk)
    rfq = get_object_or_404(RFQ, pk=rfq_pk, procurement=proc)
    portal = request.build_absolute_uri(reverse("portal_quote", args=[rfq.token]))
    return render(request, "core/rfq_print.html", {
        "proc": proc, "rfq": rfq, "rows": services.consolidated_rows(proc), "portal_url": portal,
    })
