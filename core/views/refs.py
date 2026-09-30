"""Справочник поставщиков (остальные справочники — в /admin/)."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .. import roles
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
