"""Договоры, исполнение (оплаты/поступления) и обмен с 1С."""
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import excel, onec, roles, services
from ..forms import ContractForm, UploadForm
from ..models import Contract, ContractLine, ReceiptLine
from .common import attempt, dec


@roles.require("contract.view")
def contract_list(request):
    qs = Contract.objects.select_related("supplier", "procurement", "parent").prefetch_related("lines", "payments")
    f = request.GET
    if f.get("status"):
        qs = qs.filter(status=f["status"])
    if f.get("supplier"):
        qs = qs.filter(supplier__name__icontains=f["supplier"])
    if f.get("kind"):
        qs = qs.filter(kind=f["kind"])
    return render(request, "core/contract_list.html", {
        "contracts": qs, "statuses": Contract.Status.choices, "kinds": Contract.Kind.choices, "f": f,
    })


@roles.require("contract.manage")
def contract_create(request):
    form = ContractForm(request.POST or None, initial={"kind": Contract.Kind.FRAMEWORK, "status": Contract.Status.SIGNED,
                                                       "number": services.next_contract_number()})
    if request.method == "POST" and form.is_valid():
        c = form.save(commit=False)
        c.responsible = request.user
        c.save()
        services.log("Рамочный договор создан вручную", request.user, contract=c)
        messages.success(request, f"{c} создан. Спецификации к нему будут предлагаться при оформлении закупок.")
        return redirect(c)
    return render(request, "core/contract_form.html", {"form": form})


@roles.require("contract.view")
def contract_detail(request, pk):
    c = get_object_or_404(Contract.objects.select_related("supplier", "procurement", "parent"), pk=pk)
    tab = request.GET.get("tab", "spec")
    lines = list(c.lines.select_related("item", "item__memo", "item__memo__initiator"))
    unmatched = ReceiptLine.objects.filter(receipt__contract=c, confirmed=False).select_related("receipt", "contract_line__item")
    return render(request, "core/contract_detail.html", {
        "c": c, "tab": tab, "lines": lines, "flow": services.CONTRACT_FLOW.get(c.status, []),
        "status_labels": dict(Contract.Status.choices),
        "receipts": c.receipts.prefetch_related("lines__contract_line__item"),
        "payments": c.payments.all(), "unmatched": unmatched,
        "history": c.history.select_related("user"), "specs": c.specifications.all(),
        "manage": roles.can(request.user, "contract.manage"),
        "finance": roles.can(request.user, "contract.finance"),
        "today": timezone.localdate(),
    })


@roles.require("contract.view")
@require_POST
def contract_action(request, pk, action):
    c = get_object_or_404(Contract, pk=pk)
    u = request.user
    p = request.POST
    tab = "spec"
    if action == "status" and roles.can(u, "contract.manage"):
        attempt(request, lambda: services.set_contract_status(c, p["status"], u), "Статус договора изменён")
    elif action == "payment" and roles.can(u, "contract.finance"):
        tab = "payments"
        attempt(request, lambda: services.register_payment(
            c, onec._to_date(p.get("date")), dec(p.get("amount")) or 0, p.get("doc", ""), u), "Оплата внесена")
    elif action == "receipt" and roles.can(u, "contract.finance"):
        tab = "receipts"

        def build():
            entries = []
            for line in c.lines.all():
                q = dec(p.get(f"qty_{line.pk}"))
                if q:
                    entries.append((line, q, ReceiptLine.Match.MANUAL, True, line.description, line.item.code))
            if not entries:
                raise services.BusinessError("Укажите поставленное количество хотя бы по одной строке.")
            services.register_receipt(c, onec._to_date(p.get("date")), p.get("doc", ""), entries, u)
        attempt(request, build, "Поступление внесено")
    elif action == "match" and roles.can(u, "contract.finance"):
        tab = "receipts"
        rl = get_object_or_404(ReceiptLine, pk=p.get("rl"), receipt__contract=c)
        cl = get_object_or_404(ContractLine, pk=p.get("line"), contract=c)
        attempt(request, lambda: services.confirm_receipt_line(rl, cl, u), "Строка поступления разнесена")
    elif action == "export":
        fmt = p.get("format", "xml")
        return _export([c], fmt, mark=True)
    else:
        messages.error(request, "Действие недоступно")
    return redirect(c.get_absolute_url() + f"?tab={tab}")


def _export(contracts, fmt, mark=False, collapse=None):
    stamp = timezone.localtime().strftime("%Y%m%d_%H%M")
    if fmt == "json":
        resp = HttpResponse(onec.export_json(contracts, collapse), content_type="application/json; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="contracts_{stamp}.json"'
    elif fmt == "xlsx":
        resp = excel.rows_response(onec.export_rows(contracts, collapse), f"Договоры_для_1С_{stamp}.xlsx", sheet="Договоры")
    else:
        resp = HttpResponse(onec.export_xml(contracts, collapse), content_type="application/xml; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="contracts_{stamp}.xml"'
    if mark:
        onec.mark_exported(contracts)
    return resp


@roles.require("integration.1c")
def integration(request):
    report = None
    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind == "export":
            ids = request.POST.getlist("contracts")
            qs = Contract.objects.filter(pk__in=ids) if ids else Contract.objects.filter(
                exported_1c_at__isnull=True, status__in=[Contract.Status.SIGNED, Contract.Status.SIGNING])
            contracts = list(qs)
            if not contracts:
                messages.error(request, "Нет договоров для выгрузки")
                return redirect("integration")
            collapse = request.POST.get("collapse") == "1"
            return _export(contracts, request.POST.get("format", "xml"), mark=True, collapse=collapse)
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                if kind == "receipts":
                    report = onec.import_receipts(request.FILES["file"], request.user)
                    report["kind"] = "receipts"
                elif kind == "payments":
                    report = onec.import_payments(request.FILES["file"], request.user)
                    report["kind"] = "payments"
            except services.BusinessError as e:
                messages.error(request, str(e))
    contracts = Contract.objects.exclude(status__in=[Contract.Status.CANCELLED, Contract.Status.DRAFT]) \
        .select_related("supplier").order_by("exported_1c_at", "-created_at")
    return render(request, "core/integration.html", {
        "contracts": contracts, "report": report, "upload": UploadForm(),
        "collapse_default": services.cfg("ONEC_COLLAPSE_CONSOLIDATED"),
        "receipt_columns": onec.RECEIPT_COLUMNS, "payment_columns": onec.PAYMENT_COLUMNS,
    })


@roles.require("integration.1c")
def integration_template(request, kind):
    if kind == "payments":
        rows = [onec.PAYMENT_COLUMNS]
        for c in Contract.objects.filter(status__in=[Contract.Status.SIGNED, Contract.Status.EXECUTION])[:5]:
            rows.append([c.number, timezone.localdate().strftime("%d.%m.%Y"), "ПП-0001", float(c.amount)])
        return excel.rows_response(rows, "Шаблон_оплаты_1С.xlsx", sheet="Оплаты")
    contract = Contract.objects.filter(pk=request.GET.get("contract")).first()
    return excel.rows_response(onec.sample_receipt_rows(contract), "Шаблон_поступления_1С.xlsx", sheet="Поступления")
