"""Справочник поставщиков (остальные справочники — в /admin/)."""
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .. import roles
from ..refs_import import SHEETS, build_template, import_workbook
from ..forms import SupplierForm
from ..models import Category, Supplier


@roles.require("procurement.view")
def supplier_list(request):
    qs = Supplier.objects.prefetch_related("categories")
    if request.GET.get("category"):
        qs = qs.filter(categories=request.GET["category"])
    if request.GET.get("q"):
        qs = qs.filter(name__icontains=request.GET["q"])
    return render(request, "core/supplier_list.html", {
        "suppliers": qs, "categories": Category.objects.all(), "f": request.GET,
        "manage": roles.can(request.user, "procurement.manage"),
    })


@roles.require("procurement.manage")
def supplier_edit(request, pk=None):
    obj = get_object_or_404(Supplier, pk=pk) if pk else None
    form = SupplierForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        s = form.save()
        messages.success(request, f"Поставщик «{s}» сохранён")
        return redirect("supplier_list")
    return render(request, "core/supplier_form.html", {"form": form, "obj": obj})


@roles.require("refs.import")
def refs_import(request):
    """Загрузка справочников заказчика из Excel (для администратора)."""
    report = None
    if request.method == "POST" and request.FILES.get("file"):
        dry_run = request.POST.get("mode") != "apply"
        report = import_workbook(request.FILES["file"].read(), dry_run=dry_run)
        report.dry_run = dry_run
    return render(request, "core/refs_import.html", {"report": report, "sheets": SHEETS})


@roles.require("refs.import")
def refs_template(request):
    resp = HttpResponse(build_template(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = "attachment; filename*=UTF-8''%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA%D0%B8.xlsx"
    return resp
