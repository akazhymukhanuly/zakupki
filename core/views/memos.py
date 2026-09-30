"""Реестр и карточка СЗ, согласование, отзыв позиций, подтверждение аналогов."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import roles, services
from ..forms import MemoForm, MemoItemForm
from ..models import (
    DecisionApproval, DecisionLine, Department, Memo, MemoItem, MemoTemplate, MemoTemplateLine, Nomenclature,
    Procurement, WithdrawalRequest,
)
from .common import attempt, back


def visible_memos(user):
    qs = Memo.objects.select_related("department", "initiator", "approver").prefetch_related("items")
    if roles.can(user, "memo.view_all"):
        return qs
    return qs.filter(
        Q(initiator=user) | Q(approver=user) | Q(department__head=user)
        | Q(department=getattr(getattr(user, "profile", None), "department", None))
    ).distinct()


def get_memo(user, pk):
    memo = get_object_or_404(Memo, pk=pk)
    if not visible_memos(user).filter(pk=pk).exists():
        raise PermissionDenied("Нет доступа к этой СЗ")
    return memo


def can_edit(user, memo):
    return memo.is_editable and (memo.initiator_id == user.pk or roles.has_role(user, roles.ADMIN))


def can_approve(user, memo):
    return memo.state == Memo.State.ON_APPROVAL and (
        memo.approver_id == user.pk
        or (memo.approver_id is None and roles.can(user, "memo.approve"))
        or roles.has_role(user, roles.ADMIN)
    )


@login_required
def memo_list(request):
    qs = visible_memos(request.user)
    f = request.GET
    if f.get("mine"):
        qs = qs.filter(initiator=request.user)
    if f.get("department"):
        qs = qs.filter(department_id=f["department"])
    if f.get("q"):
        q = f["q"].strip()
        cond = Q(justification__icontains=q) | Q(items__description__icontains=q)
        if q.isdigit():
            cond |= Q(number=int(q))
        qs = qs.filter(cond).distinct()
    memos = list(qs)
    if f.get("status"):
        memos = [m for m in memos if m.aggregate_status[0] == f["status"]]
    statuses = [("draft", "Черновик"), ("on_approval", "На согласовании"), ("new", "Новая"), ("in_work", "В работе"),
                ("problem", "Проблемная"), ("done", "Исполнена"), ("rejected", "Отклонена")]
    return render(request, "core/memo_list.html", {
        "memos": memos, "departments": Department.objects.all(), "statuses": statuses, "f": f,
    })


@login_required
def memo_create(request):
    if not roles.can(request.user, "memo.create"):
        raise PermissionDenied("Нет права создавать СЗ")
    profile = getattr(request.user, "profile", None)
    if request.method == "POST":
        form = MemoForm(request.POST)
        if form.is_valid():
            memo = form.save(commit=False)
            memo.initiator = request.user
            memo.save()
            services.log("СЗ создана", request.user, memo=memo)
            tpl = request.POST.get("template")
            if tpl:
                _apply_template(memo, get_object_or_404(MemoTemplate, pk=tpl))
            messages.success(request, f"{memo} создана. Добавьте позиции.")
            return redirect(memo)
    else:
        form = MemoForm(initial={
            "department": profile.department_id if profile else None,
            "required_date": timezone.localdate() + timedelta(days=14),
        })
    templates = MemoTemplate.objects.filter(Q(owner=request.user) | Q(owner__isnull=True))
    return render(request, "core/memo_form.html", {"form": form, "templates": templates})


@login_required
def memo_edit(request, pk):
    memo = get_memo(request.user, pk)
    if not can_edit(request.user, memo):
        return redirect(memo)
    form = MemoForm(request.POST or None, instance=memo)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Шапка СЗ сохранена")
        return redirect(memo)
    return render(request, "core/memo_form.html", {"form": form, "memo": memo})


@login_required
def memo_detail(request, pk):
    memo = get_memo(request.user, pk)
    items = list(memo.items.select_related("nomenclature", "category", "budget_item").prefetch_related(
        "procurement_lines__procurement", "contract_lines__contract__supplier"))
    rows = []
    for item in items:
        plines = [pl for pl in item.procurement_lines.all()]
        active = next((pl for pl in plines if pl.state == "active"), None)
        last = active or (plines[-1] if plines else None)
        contracts = [cl.contract for cl in item.contract_lines.all() if cl.contract.status != "cancelled"]
        rows.append({
            "item": item,
            "procurement": last.procurement if last else None,
            "contracts": contracts,
            "suppliers": sorted({c.supplier.name for c in contracts}),
            "contracted": item.contracted_qty,
            "delivered": item.delivered_qty,
            "pending_withdrawal": item.withdrawals.filter(state=WithdrawalRequest.State.PENDING).first(),
        })
    analogs = DecisionLine.objects.filter(
        procurement_line__item__memo=memo, analog_state=DecisionLine.AnalogState.PENDING
    ).select_related("procurement_line__item", "quote_line__quote__rfq__supplier", "procurement")
    is_owner = memo.initiator_id == request.user.pk or roles.has_role(request.user, roles.ADMIN)
    return render(request, "core/memo_detail.html", {
        "memo": memo, "rows": rows, "item_form": MemoItemForm(initial={"required_date": memo.required_date}),
        "can_edit": can_edit(request.user, memo), "can_approve": can_approve(request.user, memo),
        "is_owner": is_owner, "analogs": analogs, "history": memo.history.select_related("user")[:50],
        "summary": [(label, memo.summary().get(g, 0)) for g, label in Memo.SUMMARY_GROUPS if memo.summary().get(g)],
        "nom_data": {n.pk: {"unit": n.unit, "category": n.category_id} for n in Nomenclature.objects.all()},
    })


@login_required
def memo_print(request, pk):
    """Печатная форма служебной записки (печать / сохранение в PDF из браузера)."""
    memo = get_memo(request.user, pk)
    items = list(memo.items.select_related("budget_item"))
    for i in items:
        i.est_amount = i.estimated_price * i.quantity if i.estimated_price else None
    total = sum((i.est_amount for i in items if i.est_amount and not i.rejected), 0)
    head = memo.department.head
    return render(request, "core/memo_print.html", {
        "memo": memo, "items": items, "total": total,
        "addressee": head.get_full_name() if head else "",
        "position": getattr(getattr(memo.initiator, "profile", None), "position", ""),
    })


@login_required
def item_edit(request, pk, item_pk=None):
    memo = get_memo(request.user, pk)
    if not can_edit(request.user, memo):
        messages.error(request, "После отправки на согласование позиции изменять нельзя (п. 6.5).")
        return redirect(memo)
    item = get_object_or_404(MemoItem, pk=item_pk, memo=memo) if item_pk else None
    form = MemoItemForm(request.POST or None, instance=item, initial=None if item else {"required_date": memo.required_date})
    if request.method == "POST":
        if form.is_valid():
            obj = form.save(commit=False)
            obj.memo = memo
            if not obj.line_no:
                obj.line_no = services.next_line_no(memo)
            obj.save()
            messages.success(request, f"Позиция {obj.code} сохранена")
            if "add_more" in request.POST:
                return redirect(memo.get_absolute_url() + "#add")
            return redirect(memo)
        if not item:
            messages.error(request, "Позиция не добавлена: " + "; ".join(e for errs in form.errors.values() for e in errs))
            return redirect(memo.get_absolute_url() + "#add")
    return render(request, "core/item_form.html", {"form": form, "memo": memo, "item": item})


@login_required
@require_POST
def item_delete(request, pk, item_pk):
    memo = get_memo(request.user, pk)
    if can_edit(request.user, memo):
        get_object_or_404(MemoItem, pk=item_pk, memo=memo).delete()
        messages.success(request, "Позиция удалена")
    return redirect(memo)


def _apply_template(memo, tpl):
    for tl in tpl.lines.select_related("nomenclature"):
        MemoItem.objects.create(
            memo=memo, line_no=services.next_line_no(memo), nomenclature=tl.nomenclature,
            description=tl.nomenclature.name, quantity=tl.quantity, unit=tl.nomenclature.unit,
            required_date=memo.required_date, category=tl.nomenclature.category,
        )


@login_required
@require_POST
def memo_action(request, pk, action):
    memo = get_memo(request.user, pk)
    u = request.user
    p = request.POST
    if action == "submit" and can_edit(u, memo):
        attempt(request, lambda: services.submit_memo(memo, u), "СЗ отправлена на согласование")
    elif action == "return" and memo.initiator_id == u.pk:
        attempt(request, lambda: services.return_memo_to_draft(memo, u), "СЗ возвращена в черновик")
    elif action == "approve" and can_approve(u, memo):
        rejected = {int(k.split("_")[1]): p.get(f"reason_{k.split('_')[1]}", "")
                    for k in p if k.startswith("reject_")}
        attempt(request, lambda: services.approve_memo(memo, u, rejected, p.get("comment", "")), "Решение по СЗ сохранено")
    elif action == "reject" and can_approve(u, memo):
        attempt(request, lambda: services.reject_memo(memo, u, p.get("comment", "")), "СЗ отклонена")
    elif action == "copy":
        ok, new = attempt(request, lambda: services.copy_memo(memo, u), "Создана копия СЗ")
        if ok:
            return redirect(new)
    elif action == "template" and can_edit(u, memo):
        tpl = get_object_or_404(MemoTemplate, pk=p.get("template"))
        _apply_template(memo, tpl)
        messages.success(request, f"Добавлены позиции из шаблона «{tpl.name}»")
    elif action == "save_template":
        tpl = MemoTemplate.objects.create(name=p.get("name") or f"Шаблон из {memo}", owner=u, department=memo.department)
        for i in memo.items.filter(nomenclature__isnull=False):
            MemoTemplateLine.objects.create(template=tpl, nomenclature=i.nomenclature, quantity=i.quantity)
        messages.success(request, f"Шаблон «{tpl.name}» сохранён (позиции из справочника номенклатуры)")
    else:
        messages.error(request, "Действие недоступно")
    return redirect(memo)


@login_required
@require_POST
def item_action(request, item_pk, action):
    item = get_object_or_404(MemoItem, pk=item_pk)
    memo = get_memo(request.user, item.memo_id)
    u = request.user
    owner = memo.initiator_id == u.pk or roles.has_role(u, roles.ADMIN)
    if action == "withdraw" and owner:
        ok, req = attempt(request, lambda: services.withdraw_item(item, u, request.POST.get("reason", "")))
        if ok:
            messages.success(request, "Запрос на отзыв отправлен закупщику" if req else "Позиция отозвана")
    elif action == "close" and owner:
        attempt(request, lambda: services.close_item(item, u), "Получение подтверждено, позиция закрыта")
    else:
        messages.error(request, "Действие недоступно")
    return redirect(memo)


@login_required
@require_POST
def analog_confirm(request, pk):
    d = get_object_or_404(DecisionLine, pk=pk)
    accept = request.POST.get("accept") == "1"
    attempt(request, lambda: services.confirm_analog(d, request.user, accept),
            "Спасибо, ответ передан закупщику")
    return back(request, d.procurement_line.item.memo.get_absolute_url())


@login_required
def approvals(request):
    """Входящие на согласование: СЗ и решения по закупкам."""
    u = request.user
    memos = [m for m in Memo.objects.filter(state=Memo.State.ON_APPROVAL).select_related("department", "initiator")
             .prefetch_related("items") if can_approve(u, m)]
    my_roles = roles.user_roles(u)
    decisions = DecisionApproval.objects.filter(
        state=DecisionApproval.State.PENDING, procurement__decision_state=Procurement.DecisionState.ON_APPROVAL,
    ).select_related("procurement", "procurement__buyer")
    if not roles.has_role(u, roles.ADMIN):
        decisions = decisions.filter(role__in=my_roles)
    analogs = DecisionLine.objects.filter(
        analog_state=DecisionLine.AnalogState.PENDING, procurement_line__item__memo__initiator=u
    ).select_related("procurement_line__item__memo", "quote_line__quote__rfq__supplier")
    withdrawals = WithdrawalRequest.objects.filter(state=WithdrawalRequest.State.PENDING, procurement__buyer=u) \
        .select_related("item__memo", "procurement", "requested_by")
    return render(request, "core/approvals.html", {
        "memos": memos, "decisions": decisions, "analogs": analogs, "withdrawals": withdrawals,
    })
