"""Бизнес-логика раздела «Потребности, закупки и договоры».

Все изменения состояния идут через функции этого модуля, чтобы правила
целостности (п. 3 ТЗ) и пересчёт статусов позиций (п. 4.1) были в одном месте.
"""
from collections import OrderedDict, defaultdict
from datetime import timedelta
from decimal import Decimal

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from . import roles
from .models import (
    ZERO, ApprovalRule, Contract, Counter, ContractLine, DecisionApproval, DecisionLine, HistoryEntry, Memo, MemoItem,
    Notification, Payment, Procurement, ProcurementLine, Quote, QuoteLine, Receipt, ReceiptLine, ReminderLog, RFQ,
    Supplier, WithdrawalRequest,
)

S = MemoItem.Status
P = Procurement.Status
logger = logging.getLogger("core.services")


class BusinessError(Exception):
    """Нарушение бизнес-правила — показывается пользователю как есть."""


def cfg(key):
    return settings.PROCUREMENT[key]


def fmt_qty(value):
    value = Decimal(value).normalize()
    return f"{value:f}".rstrip("0").rstrip(".") if "." in f"{value:f}" else f"{value:f}"


# ---------------------------------------------------------------- журнал и уведомления


def log(text, user=None, memo=None, procurement=None, contract=None):
    HistoryEntry.objects.create(text=text, user=user, memo=memo, procurement=procurement, contract=contract)


def notify(users, text, url=""):
    seen = set()
    emails = []
    for u in users:
        if u and u.pk not in seen:
            seen.add(u.pk)
            Notification.objects.create(user=u, text=text, url=url)
            if u.email:
                emails.append(u.email)
    if emails and settings.EMAIL_HOST:
        # Письмо уходит только после успешной фиксации транзакции и никогда не ломает действие пользователя.
        transaction.on_commit(lambda: _send_email(emails, text, url))


def _send_email(emails, text, url):
    link = f"{settings.SITE_URL}{url}" if url.startswith("/") else url
    body = f"{text}\n\n{link}\n\n— Система «Закупки»" if link else text
    try:
        send_mail(f"Закупки: {text[:80]}", body, None, emails, fail_silently=False)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить письмо %s", emails)


def users_with_role(*role_names):
    return User.objects.filter(Q(groups__name__in=role_names) | Q(is_superuser=True), is_active=True).distinct()


# ---------------------------------------------------------------- статус позиции (п. 4.1)


def compute_status(item):
    memo = item.memo
    if item.withdrawn:
        return S.WITHDRAWN
    if item.rejected or memo.state == Memo.State.REJECTED:
        return S.REJECTED
    if memo.state == Memo.State.DRAFT:
        return S.DRAFT
    if memo.state == Memo.State.ON_APPROVAL:
        return S.ON_APPROVAL
    if item.closed:
        return S.CLOSED

    contracted = item.contracted_qty
    delivered = item.delivered_qty
    target = item.target_qty
    if contracted > 0 and contracted >= target and delivered >= target:
        return S.DELIVERED
    if delivered > 0:
        return S.PARTIALLY_DELIVERED
    if contracted > 0 and contracted >= target:
        return S.CONTRACTED
    active = item.active_procurement_line
    if active:
        proc = active.procurement
        if proc.decision_state == Procurement.DecisionState.APPROVED and active.decision_lines.exists():
            return S.SUPPLIER_SELECTED
        return S.IN_PROCUREMENT
    if contracted > 0:
        return S.PARTIALLY_CONTRACTED
    return S.APPROVED


def recalc_item(item, refresh_memo=True):
    """Пересчитать количества и статус позиции (и сводный статус её СЗ)."""
    changed = []
    contracted, delivered = item.calc_contracted_qty(), item.calc_delivered_qty()
    if contracted != item.contracted_qty or delivered != item.delivered_qty:
        item.contracted_qty, item.delivered_qty = contracted, delivered
        changed += ["contracted_qty", "delivered_qty"]
    new = compute_status(item)
    if new != item.status:
        item.status = new
        item.status_changed_at = timezone.now()
        changed += ["status", "status_changed_at"]
        if new == S.DELIVERED and not item.delivered_at:
            item.delivered_at = timezone.now()
            changed.append("delivered_at")
    in_pool = pool_eligible(item)
    if in_pool and not item.pool_since:
        item.pool_since = timezone.now()
        changed.append("pool_since")
    elif not in_pool and item.pool_since:
        item.pool_since = None
        changed.append("pool_since")
    if changed:
        item.save(update_fields=changed)
    if refresh_memo:
        recalc_memo(item.memo)
    return item


def recalc_items(items):
    memos = {}
    for item in items:
        recalc_item(item, refresh_memo=False)
        memos[item.memo_id] = item.memo
    for memo in memos.values():
        recalc_memo(memo)


def recalc_memo(memo):
    code = memo.compute_status_code()
    if code != memo.status_code:
        memo.status_code = code
        memo.save(update_fields=["status_code"])


def pool_eligible(item):
    return (
        item.memo.is_approved
        and not (item.rejected or item.withdrawn or item.closed)
        and item.remaining_qty > 0
        and not item.active_procurement_line
    )


def pool_queryset():
    """Пул потребностей: утверждённые позиции с нераспределённым остатком."""
    return (
        MemoItem.objects.filter(
            memo__state__in=[Memo.State.APPROVED, Memo.State.PARTIALLY_APPROVED],
            rejected=False, withdrawn=False, closed=False, remainder_closed=False,
            quantity__gt=F("contracted_qty"),
        )
        .exclude(procurement_lines__state=ProcurementLine.State.ACTIVE)
        .select_related("memo", "memo__department", "memo__initiator", "nomenclature", "category", "budget_item")
    )


def pool_items():
    return list(pool_queryset())


# ---------------------------------------------------------------- СЗ (шаг 1)


def next_line_no(memo):
    last = memo.items.order_by("-line_no").values_list("line_no", flat=True).first()
    return (last or 0) + 1


@transaction.atomic
def submit_memo(memo, user):
    if memo.state != Memo.State.DRAFT:
        raise BusinessError("Отправить можно только черновик СЗ.")
    if not memo.items.exists():
        raise BusinessError("Добавьте хотя бы одну позицию.")
    memo.state = Memo.State.ON_APPROVAL
    memo.submitted_at = timezone.now()
    if not memo.approver and memo.department.head:
        memo.approver = memo.department.head
    memo.save()
    recalc_items(memo.items.all())
    recalc_memo(memo)
    log("СЗ отправлена на согласование", user, memo=memo)
    approvers = [memo.approver] if memo.approver else list(users_with_role(roles.APPROVER))
    notify(approvers, f"{memo} от {memo.initiator.get_full_name() or memo.initiator} ждёт согласования", memo.get_absolute_url())


@transaction.atomic
def approve_memo(memo, user, rejected_items=None, comment=""):
    """Согласование целиком по СЗ; отдельные позиции можно отклонить с комментарием."""
    if memo.state != Memo.State.ON_APPROVAL:
        raise BusinessError("СЗ не находится на согласовании.")
    rejected_items = rejected_items or {}
    items = list(memo.items.all())
    for item in items:
        if item.pk in rejected_items:
            reason = rejected_items[item.pk].strip()
            if not reason:
                raise BusinessError(f"Укажите причину отклонения позиции {item.code}.")
            item.rejected = True
            item.reject_comment = reason
            item.save(update_fields=["rejected", "reject_comment"])
    approved = [i for i in items if not i.rejected]
    if not approved:
        memo.state = Memo.State.REJECTED
    elif len(approved) < len(items):
        memo.state = Memo.State.PARTIALLY_APPROVED
    else:
        memo.state = Memo.State.APPROVED
    memo.approver = user
    memo.approved_at = timezone.now()
    memo.approval_comment = comment
    memo.save()
    recalc_items(items)
    recalc_memo(memo)
    log(f"СЗ согласована: {memo.get_state_display()}" + (f". {comment}" if comment else ""), user, memo=memo)
    notify([memo.initiator], f"{memo}: {memo.get_state_display().lower()}", memo.get_absolute_url())
    if approved:
        notify(users_with_role(roles.BUYER), f"В пуле новые позиции из {memo} ({len(approved)} шт.)", "/pool/")


@transaction.atomic
def reject_memo(memo, user, comment):
    if memo.state != Memo.State.ON_APPROVAL:
        raise BusinessError("СЗ не находится на согласовании.")
    if not comment.strip():
        raise BusinessError("Укажите причину отклонения.")
    memo.state = Memo.State.REJECTED
    memo.approver = user
    memo.approval_comment = comment
    memo.approved_at = timezone.now()
    memo.save()
    recalc_items(memo.items.all())
    recalc_memo(memo)
    log(f"СЗ отклонена: {comment}", user, memo=memo)
    notify([memo.initiator], f"{memo} отклонена: {comment}", memo.get_absolute_url())


@transaction.atomic
def return_memo_to_draft(memo, user):
    """Отозвать СЗ с согласования (до решения согласующего) для правки."""
    if memo.state != Memo.State.ON_APPROVAL:
        raise BusinessError("Вернуть в черновик можно только СЗ на согласовании.")
    memo.state = Memo.State.DRAFT
    memo.save()
    recalc_items(memo.items.all())
    recalc_memo(memo)
    log("СЗ возвращена в черновик инициатором", user, memo=memo)


@transaction.atomic
def copy_memo(memo, user):
    new = Memo.objects.create(
        department=memo.department, initiator=user, justification=memo.justification,
        required_date=max(memo.required_date, timezone.localdate()),
    )
    for i in memo.items.all():
        MemoItem.objects.create(
            memo=new, line_no=i.line_no, nomenclature=i.nomenclature, description=i.description,
            quantity=i.quantity, unit=i.unit, required_date=new.required_date, budget_item=i.budget_item,
            category=i.category, estimated_price=i.estimated_price, urgent=i.urgent,
        )
    log(f"Создана копией {memo}", user, memo=new)
    return new


# ---------------------------------------------------------------- отзыв и закрытие позиции (п. 6.4, 4.1)


@transaction.atomic
def withdraw_item(item, user, reason):
    """Отзыв позиции инициатором (п. 6.4)."""
    item.refresh_from_db()
    if item.status in (S.WITHDRAWN, S.REJECTED, S.CLOSED):
        raise BusinessError("Позиция уже не активна.")
    if not item.memo.is_approved:
        raise BusinessError("До утверждения СЗ позицию можно просто удалить из черновика.")
    if not reason.strip():
        raise BusinessError("Укажите причину отзыва.")
    active = item.active_procurement_line
    if active:
        if WithdrawalRequest.objects.filter(item=item, state=WithdrawalRequest.State.PENDING).exists():
            raise BusinessError("Запрос на отзыв уже отправлен закупщику.")
        req = WithdrawalRequest.objects.create(item=item, procurement=active.procurement, requested_by=user, reason=reason)
        log(f"Запрос на отзыв позиции {item.code}: {reason}", user, memo=item.memo, procurement=active.procurement)
        notify([active.procurement.buyer], f"Инициатор просит отозвать позицию {item.code} из закупки №{active.procurement.number}",
               active.procurement.get_absolute_url())
        return req
    contracted = item.contracted_qty
    if contracted >= item.quantity or (contracted > 0 and item.remaining_qty == 0):
        raise BusinessError("Позиция в договоре — отзыв невозможен, только через изменение договора вручную.")
    if contracted > 0:
        item.remainder_closed = True
        item.save(update_fields=["remainder_closed"])
        log(f"Остаток позиции {item.code} закрыт инициатором: {reason}", user, memo=item.memo)
    else:
        item.withdrawn = True
        item.save(update_fields=["withdrawn"])
        log(f"Позиция {item.code} отозвана: {reason}", user, memo=item.memo)
    recalc_item(item)
    return None


@transaction.atomic
def resolve_withdrawal(req, user, approve):
    if req.state != WithdrawalRequest.State.PENDING:
        raise BusinessError("Запрос уже обработан.")
    item = req.item
    item.refresh_from_db()
    req.state = WithdrawalRequest.State.APPROVED if approve else WithdrawalRequest.State.REJECTED
    req.resolved_by = user
    req.resolved_at = timezone.now()
    req.save()
    if approve:
        active = item.active_procurement_line
        if active:
            if active.decision_lines.filter(procurement__decision_state=Procurement.DecisionState.APPROVED).exists():
                raise BusinessError("По позиции уже утверждено решение — сначала отмените решение.")
            active.decision_lines.all().delete()
            active.state = ProcurementLine.State.REMOVED
            active.save(update_fields=["state"])
            active.procurement.rfqs.filter(sent_at__isnull=False).update(needs_correction=True)
        if item.contracted_qty > 0:
            item.remainder_closed = True
        else:
            item.withdrawn = True
        item.save(update_fields=["remainder_closed", "withdrawn"])
        recalc_item(item)
        log(f"Отзыв позиции {item.code} подтверждён закупщиком", user, memo=item.memo, procurement=req.procurement)
    else:
        log(f"Отзыв позиции {item.code} отклонён закупщиком", user, memo=item.memo, procurement=req.procurement)
    notify([req.requested_by], f"Запрос на отзыв {item.code}: {req.get_state_display().lower()}", item.memo.get_absolute_url())


@transaction.atomic
def close_item(item, user):
    item.refresh_from_db()
    if item.status != S.DELIVERED:
        raise BusinessError("Закрыть можно только полностью поставленную позицию.")
    item.closed = True
    item.save(update_fields=["closed"])
    recalc_item(item)
    log(f"Позиция {item.code} закрыта (получение подтверждено)", user, memo=item.memo)


# ---------------------------------------------------------------- закупка (шаг 3)


@transaction.atomic
def create_procurement(user, items, title, method=Procurement.Method.RFQ, kp_deadline=None, buyer=None):
    if not items:
        raise BusinessError("Выберите позиции из пула.")
    proc = Procurement.objects.create(
        title=title or "Закупка", buyer=buyer or user, method=method, kp_deadline=kp_deadline
    )
    add_items_to_procurement(proc, items, user)
    log(f"Закупка создана из {len(items)} поз.", user, procurement=proc)
    return proc


def add_items_to_procurement(proc, items, user):
    if proc.status not in (P.DRAFT, P.RFQ, P.COLLECTING):
        raise BusinessError("Добавлять позиции можно только до этапа анализа КП.")
    for item in items:
        item = MemoItem.objects.select_for_update().get(pk=item.pk)
        if not pool_eligible(item):
            raise BusinessError(f"Позиция {item.code} уже не в пуле.")
        note = item.pool_note
        ProcurementLine.objects.create(procurement=proc, item=item, quantity=item.remaining_qty)
        if note:
            item.pool_note = ""
            item.save(update_fields=["pool_note"])
        recalc_item(item)
        log(f"Позиция {item.code} включена в закупку №{proc.number}", user, memo=item.memo, procurement=proc)
    if proc.rfqs.filter(sent_at__isnull=False).exists():
        proc.rfqs.filter(sent_at__isnull=False).update(needs_correction=True)


@transaction.atomic
def release_line(line, user, note=""):
    """Вернуть позицию из закупки в пул."""
    if line.state != ProcurementLine.State.ACTIVE:
        raise BusinessError("Строка уже не активна.")
    proc = line.procurement
    if proc.decision_state in (Procurement.DecisionState.ON_APPROVAL, Procurement.DecisionState.APPROVED) and \
            line.decision_lines.exists():
        raise BusinessError("По позиции есть решение на согласовании/утверждённое.")
    line.decision_lines.all().delete()
    line.state = ProcurementLine.State.RELEASED
    line.save(update_fields=["state"])
    item = line.item
    item.pool_note = note or f"возвращена из закупки №{proc.number}"
    item.save(update_fields=["pool_note"])
    recalc_item(item)
    if proc.rfqs.filter(sent_at__isnull=False).exists():
        proc.rfqs.filter(sent_at__isnull=False).update(needs_correction=True)
    log(f"Позиция {item.code} возвращена в пул", user, memo=item.memo, procurement=proc)


@transaction.atomic
def cancel_procurement(proc, user, reason=""):
    if proc.status in (P.CONTRACTS, P.CLOSED, P.CANCELLED):
        raise BusinessError("Закупку на этом этапе отменить нельзя.")
    proc.decision_lines.all().delete()
    proc.approvals.all().delete()
    for line in proc.lines.filter(state=ProcurementLine.State.ACTIVE):
        line.state = ProcurementLine.State.RELEASED
        line.save(update_fields=["state"])
        line.item.pool_note = f"закупка №{proc.number} отменена"
        line.item.save(update_fields=["pool_note"])
        recalc_item(line.item)
    proc.status = P.CANCELLED
    proc.decision_state = Procurement.DecisionState.NONE
    proc.save()
    log(f"Закупка отменена. {reason}", user, procurement=proc)


# ---------------------------------------------------------------- запросы и КП (шаги 4–5)


def consolidated_rows(proc):
    """Сводные строки для запроса КП (п. 6.1): одинаковая номенклатура → одна строка с суммой."""
    rows = OrderedDict()
    for line in proc.lines.filter(state=ProcurementLine.State.ACTIVE).select_related("item", "item__memo", "item__nomenclature"):
        key = line.consolidation_key
        if key not in rows:
            rows[key] = {"key": key, "description": line.item.nomenclature.name if line.item.nomenclature else line.item.description,
                         "unit": line.item.unit, "quantity": ZERO, "lines": [], "required_date": line.item.required_date}
        row = rows[key]
        row["quantity"] += line.quantity
        row["lines"].append(line)
        row["required_date"] = min(row["required_date"], line.item.required_date)
    for row in rows.values():
        row["codes"] = ", ".join(l.item.code for l in row["lines"])
        row["consolidated"] = len(row["lines"]) > 1
    return list(rows.values())


@transaction.atomic
def add_rfqs(proc, suppliers, user):
    if not proc.is_open or proc.status in (P.DECIDED, P.CONTRACTS):
        raise BusinessError("На этом этапе нельзя добавлять поставщиков.")
    created = 0
    for s in suppliers:
        _, was_created = RFQ.objects.get_or_create(procurement=proc, supplier=s)
        created += was_created
    if proc.status == P.DRAFT:
        proc.status = P.RFQ
        proc.save(update_fields=["status"])
    log(f"Добавлены поставщики в запрос КП: {created}", user, procurement=proc)


@transaction.atomic
def mark_rfqs_sent(proc, user, rfqs=None):
    if not proc.lines.filter(state=ProcurementLine.State.ACTIVE).exists():
        raise BusinessError("В закупке нет позиций.")
    rfqs = rfqs if rfqs is not None else proc.rfqs.all()
    now = timezone.now()
    n = 0
    for rfq in rfqs:
        rfq.sent_at = now
        rfq.needs_correction = False
        rfq.save(update_fields=["sent_at", "needs_correction"])
        n += 1
    if n and proc.status in (P.DRAFT, P.RFQ):
        proc.status = P.COLLECTING
        proc.save(update_fields=["status"])
    log(f"Запросы КП отправлены: {n}", user, procurement=proc)


@transaction.atomic
def save_quote(rfq, header, rows, user=None, source=Quote.Source.MANUAL):
    """rows: {procurement_line_id: {price, offered_qty, lead_time_days, not_offered, is_analog, analog_description, comment}}"""
    proc = rfq.procurement
    if proc.status in (P.DECIDED, P.CONTRACTS, P.CLOSED, P.CANCELLED) or \
            proc.decision_state in (Procurement.DecisionState.ON_APPROVAL, Procurement.DecisionState.APPROVED):
        raise BusinessError("Решение по закупке уже принято — КП менять нельзя.")
    quote, _ = Quote.objects.get_or_create(rfq=rfq, defaults={"source": source})
    for k, v in header.items():
        setattr(quote, k, v)
    quote.source = source
    quote.received_at = timezone.now()
    quote.save()
    active_ids = set(proc.lines.filter(state=ProcurementLine.State.ACTIVE).values_list("pk", flat=True))
    for line_id, data in rows.items():
        if line_id not in active_ids:
            continue
        has_data = data.get("not_offered") or data.get("price") is not None
        if not has_data:
            QuoteLine.objects.filter(quote=quote, procurement_line_id=line_id).delete()
            continue
        if data.get("is_analog") and not (data.get("analog_description") or "").strip():
            raise BusinessError("Для аналога укажите его описание.")
        QuoteLine.objects.update_or_create(quote=quote, procurement_line_id=line_id, defaults={
            "price": None if data.get("not_offered") else data.get("price"),
            "offered_qty": data.get("offered_qty"),
            "lead_time_days": data.get("lead_time_days"),
            "not_offered": bool(data.get("not_offered")),
            "is_analog": bool(data.get("is_analog")),
            "analog_description": data.get("analog_description") or "",
            "comment": data.get("comment") or "",
        })
    if proc.status in (P.RFQ, P.COLLECTING, P.DRAFT):
        answered = Quote.objects.filter(rfq__procurement=proc).count()
        if answered >= proc.rfqs.count():
            proc.status = P.ANALYSIS
        elif proc.status != P.COLLECTING:
            proc.status = P.COLLECTING
        proc.save(update_fields=["status"])
    log(f"КП {rfq.supplier}: сохранено ({quote.get_source_display()})", user, procurement=proc)
    if source == Quote.Source.PORTAL:
        notify([proc.buyer], f"{rfq.supplier} заполнил КП по закупке №{proc.number}", proc.get_absolute_url() + "?tab=quotes")
    return quote


def start_analysis(proc, user):
    if proc.status not in (P.RFQ, P.COLLECTING):
        raise BusinessError("Перейти к анализу можно из этапа сбора КП.")
    if not Quote.objects.filter(rfq__procurement=proc).exists():
        raise BusinessError("Нет ни одного КП.")
    proc.status = P.ANALYSIS
    proc.save(update_fields=["status"])
    log("Переход к анализу КП", user, procurement=proc)


# ---------------------------------------------------------------- сравнительная таблица (шаг 6)


def comparison(proc):
    lines = list(proc.lines.filter(state__in=[ProcurementLine.State.ACTIVE, ProcurementLine.State.DONE])
                 .select_related("item", "item__memo", "item__nomenclature"))
    quotes = list(Quote.objects.filter(rfq__procurement=proc).select_related("rfq__supplier").prefetch_related("lines"))
    by_quote = {q.pk: {ql.procurement_line_id: ql for ql in q.lines.all()} for q in quotes}
    rows = []
    best_total = ZERO
    totals = {q.pk: ZERO for q in quotes}
    covered = {q.pk: 0 for q in quotes}
    for line in lines:
        cells = []
        offers = []
        for q in quotes:
            ql = by_quote[q.pk].get(line.pk)
            cells.append({"quote": q, "ql": ql})
            if ql and not ql.not_offered and ql.price is not None:
                qty = min(ql.qty, line.quantity)
                offers.append((ql.price, qty))
                totals[q.pk] += ql.price * qty
                covered[q.pk] += 1
        min_price = min(p for p, _ in offers) if offers else None
        for c in cells:
            c["is_min"] = bool(c["ql"] and min_price is not None and c["ql"].price == min_price and not c["ql"].not_offered)
        # «Лучшее по строке» с учётом частичных предложений: добираем количество от дешёвых к дорогим.
        left = line.quantity
        for price, qty in sorted(offers):
            take = min(left, qty)
            best_total += price * take
            left -= take
            if left <= 0:
                break
        rows.append({"line": line, "cells": cells, "min_price": min_price})
    return {
        "quotes": quotes,
        "rows": rows,
        "totals": [{"quote": q, "total": totals[q.pk], "covered": covered[q.pk], "full": covered[q.pk] == len(lines)} for q in quotes],
        "best_total": best_total,
        "line_count": len(lines),
    }


# ---------------------------------------------------------------- решение (шаг 7)


def _editable_decision(proc):
    if proc.status not in (P.COLLECTING, P.ANALYSIS, P.RFQ):
        raise BusinessError("Решение формируется на этапе анализа КП.")
    if proc.decision_state == Procurement.DecisionState.ON_APPROVAL:
        raise BusinessError("Решение на согласовании — изменения невозможны.")


@transaction.atomic
def set_decision(proc, entries, user, allow_over=False):
    """entries: список (procurement_line, quote_line, qty). Полностью заменяет текущее решение."""
    _editable_decision(proc)
    per_line = defaultdict(Decimal)
    for line, ql, qty in entries:
        if line.procurement_id != proc.pk or line.state != ProcurementLine.State.ACTIVE:
            raise BusinessError("Строка не относится к закупке.")
        if ql.procurement_line_id != line.pk or ql.not_offered or ql.price is None:
            raise BusinessError(f"У поставщика нет предложения по {line.item.code}.")
        if qty <= 0:
            raise BusinessError(f"Количество по {line.item.code} должно быть больше нуля.")
        if ql.offered_qty is not None and qty > ql.offered_qty:
            raise BusinessError(f"{ql.quote.supplier} предлагает по {line.item.code} только {fmt_qty(ql.offered_qty)}.")
        per_line[line.pk] += qty
    over = [l for l, _, _ in entries if per_line[l.pk] > l.quantity]
    if over and not allow_over:
        codes = ", ".join(sorted({l.item.code for l in over}))
        raise BusinessError(f"Количество превышает потребность по {codes}. Подтвердите превышение (например, минимальная партия).")
    old_analog = {(d.procurement_line_id, d.quote_line_id): d.analog_state for d in proc.decision_lines.all()}
    proc.decision_lines.all().delete()
    for line, ql, qty in entries:
        state = DecisionLine.AnalogState.NA
        if ql.is_analog:
            state = old_analog.get((line.pk, ql.pk), DecisionLine.AnalogState.PENDING)
            if state == DecisionLine.AnalogState.NA:
                state = DecisionLine.AnalogState.PENDING
        DecisionLine.objects.create(procurement=proc, procurement_line=line, quote_line=ql, quantity=qty, analog_state=state)
    if proc.decision_state == Procurement.DecisionState.REJECTED:
        proc.decision_state = Procurement.DecisionState.NONE
        proc.save(update_fields=["decision_state"])
    if proc.status in (P.RFQ, P.COLLECTING):
        proc.status = P.ANALYSIS
        proc.save(update_fields=["status"])
    log(f"Решение обновлено: {len(entries)} строк, сумма {proc.decision_total:,.2f}", user, procurement=proc)
    request_analog_confirmations(proc, user)


def request_analog_confirmations(proc, user):
    for d in proc.decision_lines.filter(analog_state=DecisionLine.AnalogState.PENDING).select_related(
        "procurement_line__item__memo__initiator", "quote_line__quote__rfq__supplier"
    ):
        item = d.procurement_line.item
        key = f"analog:{d.procurement_line_id}:{d.quote_line_id}"
        if ReminderLog.objects.filter(key=key).exists():
            continue
        ReminderLog.objects.create(key=key)
        notify([item.memo.initiator],
               f"По позиции {item.code} «{item.description}» предложен аналог: «{d.quote_line.analog_description}» "
               f"({d.supplier}). Подтвердите замену.", item.memo.get_absolute_url())


def decision_preset(proc, mode, user, quote=None):
    """Быстрые варианты решения: один поставщик на весь лот / лучший по каждой строке."""
    _editable_decision(proc)
    cmp = comparison(proc)
    entries = []
    for row in cmp["rows"]:
        line = row["line"]
        if line.state != ProcurementLine.State.ACTIVE:
            continue
        candidates = [c["ql"] for c in row["cells"] if c["ql"] and not c["ql"].not_offered and c["ql"].price is not None]
        if mode == "single":
            candidates = [ql for ql in candidates if ql.quote_id == quote.pk]
        if not candidates:
            continue
        best = min(candidates, key=lambda ql: (ql.price, ql.lead_time_days or 0))
        entries.append((line, best, min(best.qty, line.quantity)))
    set_decision(proc, entries, user)
    return entries


@transaction.atomic
def confirm_analog(decision_line, user, accept):
    item = decision_line.procurement_line.item
    if item.memo.initiator_id != user.pk and not roles.has_role(user, roles.ADMIN):
        raise BusinessError("Подтвердить замену может только инициатор СЗ.")
    if decision_line.analog_state != DecisionLine.AnalogState.PENDING:
        raise BusinessError("Замена уже рассмотрена.")
    decision_line.analog_state = DecisionLine.AnalogState.ACCEPTED if accept else DecisionLine.AnalogState.DECLINED
    decision_line.save(update_fields=["analog_state"])
    proc = decision_line.procurement
    log(f"Инициатор {'согласен' if accept else 'не согласен'} на аналог по {item.code}", user,
        memo=item.memo, procurement=proc)
    notify([proc.buyer], f"Аналог по {item.code}: инициатор {'согласен' if accept else 'не согласен'}",
           proc.get_absolute_url() + "?tab=decision")


@transaction.atomic
def submit_decision(proc, user):
    _editable_decision(proc)
    lines = list(proc.decision_lines.all())
    if not lines:
        raise BusinessError("Решение пустое: назначьте поставщиков хотя бы по одной позиции.")
    pending = [d for d in lines if d.analog_state == DecisionLine.AnalogState.PENDING]
    if pending:
        raise BusinessError("Есть аналоги без подтверждения инициатора.")
    declined = [d for d in lines if d.analog_state == DecisionLine.AnalogState.DECLINED]
    if declined:
        raise BusinessError("Инициатор отказался от аналога — измените решение по этим позициям.")
    total = proc.decision_total
    proc.approvals.all().delete()
    needed = list(ApprovalRule.objects.filter(min_amount__lte=total).values_list("role", flat=True).distinct())
    if not needed:
        _approve_decision(proc, user, auto=True)
        return
    for role in needed:
        DecisionApproval.objects.create(procurement=proc, role=role)
    proc.decision_state = Procurement.DecisionState.ON_APPROVAL
    proc.save(update_fields=["decision_state"])
    log(f"Решение на сумму {total:,.2f} отправлено на согласование: {', '.join(needed)}", user, procurement=proc)
    notify(users_with_role(*needed), f"Решение по закупке №{proc.number} на {total:,.0f} ждёт согласования", proc.get_absolute_url() + "?tab=decision")


@transaction.atomic
def resolve_decision_approval(approval, user, approve, comment=""):
    proc = approval.procurement
    if proc.decision_state != Procurement.DecisionState.ON_APPROVAL or approval.state != DecisionApproval.State.PENDING:
        raise BusinessError("Согласование неактуально.")
    if not roles.has_role(user, approval.role, roles.ADMIN):
        raise BusinessError(f"Согласовать может только роль «{approval.role}».")
    approval.state = DecisionApproval.State.APPROVED if approve else DecisionApproval.State.REJECTED
    approval.user = user
    approval.comment = comment
    approval.decided_at = timezone.now()
    approval.save()
    log(f"{approval.role}: решение {'согласовано' if approve else 'отклонено'}. {comment}", user, procurement=proc)
    if not approve:
        proc.decision_state = Procurement.DecisionState.REJECTED
        proc.decision_comment = comment
        proc.save(update_fields=["decision_state", "decision_comment"])
        notify([proc.buyer], f"Решение по закупке №{proc.number} отклонено ({approval.role}): {comment}", proc.get_absolute_url() + "?tab=decision")
        return
    if not proc.approvals.filter(state=DecisionApproval.State.PENDING).exists():
        _approve_decision(proc, user)


def _approve_decision(proc, user, auto=False):
    proc.decision_state = Procurement.DecisionState.APPROVED
    proc.status = P.DECIDED
    proc.save(update_fields=["decision_state", "status"])
    # Позиции без решения — «вернуть в пул» (никто не предложил / цены неприемлемы).
    decided_ids = set(proc.decision_lines.values_list("procurement_line_id", flat=True))
    for line in proc.lines.filter(state=ProcurementLine.State.ACTIVE).exclude(pk__in=decided_ids):
        line.state = ProcurementLine.State.RELEASED
        line.save(update_fields=["state"])
        line.item.pool_note = f"без решения в закупке №{proc.number}"
        line.item.save(update_fields=["pool_note"])
        recalc_item(line.item)
    for d in proc.decision_lines.select_related("procurement_line__item"):
        recalc_item(d.procurement_line.item)
    log("Решение утверждено" + (" (ниже порогов согласования)" if auto else ""), user, procurement=proc)
    notify([proc.buyer], f"Решение по закупке №{proc.number} утверждено — можно оформлять договоры", proc.get_absolute_url() + "?tab=contracts")


# ---------------------------------------------------------------- договоры (шаг 8)


def active_framework(supplier):
    today = timezone.localdate()
    return Contract.objects.filter(
        supplier=supplier, kind=Contract.Kind.FRAMEWORK,
        status__in=[Contract.Status.SIGNED, Contract.Status.EXECUTION],
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today)).order_by("-date").first()


def next_contract_number(reserve=True):
    """Номер договора Д-ГГГГ-NNNN. reserve=False — только подсказка для формы, счётчик не тратится."""
    year = timezone.localdate().year
    prefix = f"Д-{year}-"
    existing = lambda: Contract.objects.filter(number__startswith=prefix).count()
    if not reserve:
        n = existing() + 1
    else:
        n = Counter.next(f"contract-{year}", existing)
    while Contract.objects.filter(number=f"{prefix}{n:04d}").exists():
        n = Counter.next(f"contract-{year}") if reserve else n + 1
    return f"{prefix}{n:04d}"


def contract_proposals(proc):
    """Предложение по договорам: один на каждого выбранного поставщика."""
    groups = OrderedDict()
    contracted = set(
        ContractLine.objects.filter(decision_line__procurement=proc)
        .exclude(contract__status=Contract.Status.CANCELLED)
        .values_list("decision_line_id", flat=True)
    )
    for d in proc.decision_lines.select_related("quote_line__quote__rfq__supplier", "procurement_line__item__memo"):
        if d.pk in contracted:
            continue
        s = d.supplier
        groups.setdefault(s.pk, {"supplier": s, "lines": [], "framework": active_framework(s)})
        groups[s.pk]["lines"].append(d)
    for g in groups.values():
        g["amount"] = sum((d.amount for d in g["lines"]), ZERO)
    return list(groups.values())


def check_contract_qty(item, extra_qty):
    """Правило: сумма по строкам договоров ≤ количества позиции (иначе — предупреждение)."""
    return item.calc_contracted_qty() + extra_qty <= item.quantity


@transaction.atomic
def create_contracts(proc, user, choices, allow_over=False):
    """choices: {supplier_id: {"mode": "new"|"spec", "number": str}}"""
    if proc.decision_state != Procurement.DecisionState.APPROVED:
        raise BusinessError("Сначала решение должно быть утверждено.")
    created = []
    for g in contract_proposals(proc):
        s = g["supplier"]
        choice = choices.get(s.pk, {"mode": "new"})
        over = [d for d in g["lines"] if not check_contract_qty(d.procurement_line.item, d.quantity)]
        if over and not allow_over:
            codes = ", ".join(d.procurement_line.item.code for d in over)
            raise BusinessError(f"Сумма по договорам превысит количество позиций {codes}. Подтвердите превышение.")
        parent = None
        kind = Contract.Kind.REGULAR
        if choice.get("mode") == "spec" and g["framework"]:
            parent = g["framework"]
            kind = Contract.Kind.SPECIFICATION
        number = (choice.get("number") or "").strip()
        if not number:
            number = f"{parent.number}/С{parent.specifications.count() + 1}" if parent else next_contract_number()
        contract = Contract.objects.create(
            number=number, kind=kind, parent=parent, supplier=s, procurement=proc, responsible=proc.buyer,
        )
        for d in g["lines"]:
            item = d.procurement_line.item
            ql = d.quote_line
            desc = item.description
            if ql.is_analog:
                desc = f"{ql.analog_description} (аналог: {item.description})"
            ContractLine.objects.create(
                contract=contract, item=item, decision_line=d, description=desc, quantity=d.quantity,
                unit=item.unit, price=d.price, over_quantity_confirmed=item.pk in {o.procurement_line.item_id for o in over},
            )
        log(f"Создан по закупке №{proc.number}", user, contract=contract)
        log(f"Создан {contract} с {s} на {contract.amount:,.2f}", user, procurement=proc)
        created.append(contract)
    if not created:
        raise BusinessError("Нет строк решения без договора.")
    _finish_contracting(proc, user)
    return created


def _finish_contracting(proc, user):
    if contract_proposals(proc):
        return
    proc.status = P.CONTRACTS
    proc.save(update_fields=["status"])
    for line in proc.lines.filter(state=ProcurementLine.State.ACTIVE).select_related("item"):
        line.state = ProcurementLine.State.DONE
        line.save(update_fields=["state"])
        item = recalc_item(line.item)
        if item.remaining_qty > 0:
            # п. 6.2: остаток автоматически возвращается в пул как остаток той же позиции.
            item.pool_note = f"остаток от закупки №{proc.number}"
            item.save(update_fields=["pool_note"])
        memo_items = item.memo
        log(f"Позиция {item.code}: в договоре {fmt_qty(item.contracted_qty)} из {fmt_qty(item.quantity)}", user, memo=memo_items)
    initiators = {}
    for line in proc.lines.filter(state=ProcurementLine.State.DONE).select_related("item__memo__initiator"):
        initiators.setdefault(line.item.memo.initiator_id, (line.item.memo.initiator, line.item.memo))
    for u, memo in initiators.values():
        notify([u], f"По вашей {memo} заключены договоры (закупка №{proc.number})", memo.get_absolute_url())
    log("Договоры оформлены", user, procurement=proc)


def close_procurement(proc, user):
    if proc.status != P.CONTRACTS:
        raise BusinessError("Закрыть можно закупку с оформленными договорами.")
    proc.status = P.CLOSED
    proc.save(update_fields=["status"])
    log("Закупка закрыта", user, procurement=proc)


CONTRACT_FLOW = {
    Contract.Status.DRAFT: [Contract.Status.SIGNING, Contract.Status.CANCELLED],
    Contract.Status.SIGNING: [Contract.Status.SIGNED, Contract.Status.DRAFT, Contract.Status.CANCELLED],
    Contract.Status.SIGNED: [Contract.Status.EXECUTION, Contract.Status.CANCELLED],
    Contract.Status.EXECUTION: [Contract.Status.EXECUTED],
    Contract.Status.EXECUTED: [Contract.Status.CLOSED],
    Contract.Status.CLOSED: [],
    Contract.Status.CANCELLED: [],
}


@transaction.atomic
def set_contract_status(contract, status, user):
    if status not in CONTRACT_FLOW[contract.status]:
        raise BusinessError(f"Переход «{contract.get_status_display()}» → «{Contract.Status(status).label}» недопустим.")
    if status == Contract.Status.CANCELLED and (contract.receipts.exists() or contract.payments.exists()):
        raise BusinessError("По договору есть оплаты/поступления — аннулировать нельзя.")
    contract.status = status
    contract.save(update_fields=["status"])
    log(f"Статус: {contract.get_status_display()}", user, contract=contract)
    if status == Contract.Status.CANCELLED:
        for line in contract.lines.select_related("item"):
            line.item.pool_note = f"договор {contract.number} аннулирован"
            line.item.save(update_fields=["pool_note"])
            recalc_item(line.item)


# ---------------------------------------------------------------- исполнение (шаг 9)


@transaction.atomic
def register_receipt(contract, date, doc_number, entries, user=None):
    """entries: список (contract_line|None, qty, matched_by, confirmed, raw_name, raw_ref)."""
    if contract.status in (Contract.Status.DRAFT, Contract.Status.SIGNING, Contract.Status.CANCELLED):
        raise BusinessError(f"{contract} ещё не подписан — поступления не принимаются.")
    receipt = Receipt.objects.create(contract=contract, date=date, doc_number=doc_number)
    touched = []
    for cl, qty, matched_by, confirmed, raw_name, raw_ref in entries:
        if cl is not None and cl.contract_id != contract.pk:
            raise BusinessError("Строка поступления не относится к договору.")
        ReceiptLine.objects.create(receipt=receipt, contract_line=cl, quantity=qty, matched_by=matched_by,
                                   confirmed=confirmed and cl is not None, raw_name=raw_name, raw_ref=raw_ref)
        if cl is not None and confirmed:
            touched.append(cl)
    _after_receipt(contract, touched, user)
    log(f"Поступление {doc_number or ''} от {date:%d.%m.%Y}: {len(entries)} строк", user, contract=contract)
    return receipt


def _after_receipt(contract, touched_lines, user):
    items = {cl.item_id: cl.item for cl in touched_lines}
    recalc_items(items.values())
    if contract.status == Contract.Status.SIGNED:
        contract.status = Contract.Status.EXECUTION
    if contract.lines.exists() and all(l.delivered_qty >= l.quantity for l in contract.lines.all()):
        contract.status = Contract.Status.EXECUTED
    contract.save(update_fields=["status"])
    # Уведомление инициаторам только по их позициям.
    by_memo = defaultdict(list)
    for item in items.values():
        by_memo[item.memo].append(item)
    for memo, its in by_memo.items():
        parts = [f"{i.description} — {fmt_qty(i.delivered_qty)} из {fmt_qty(i.quantity)}" for i in its]
        notify([memo.initiator], f"По вашей СЗ №{memo.number} поставлено: " + ", ".join(parts), memo.get_absolute_url())
        log("Поставлено: " + ", ".join(parts), user, memo=memo)


@transaction.atomic
def confirm_receipt_line(rl, contract_line, user):
    """Ручное подтверждение сопоставления строки поступления (п. 8)."""
    contract = rl.receipt.contract
    if contract_line.contract_id != contract.pk:
        raise BusinessError("Строка не относится к договору.")
    rl.contract_line = contract_line
    rl.confirmed = True
    rl.matched_by = rl.matched_by if rl.matched_by != ReceiptLine.Match.ID else ReceiptLine.Match.MANUAL
    rl.save()
    _after_receipt(contract, [contract_line], user)
    log(f"Строка поступления «{rl.raw_name}» сопоставлена с {contract_line.item.code}", user, contract=contract)


@transaction.atomic
def register_payment(contract, date, amount, doc_number, user=None):
    if amount <= 0:
        raise BusinessError("Сумма оплаты должна быть больше нуля.")
    if contract.status in (Contract.Status.DRAFT, Contract.Status.CANCELLED):
        raise BusinessError("Оплата по неподписанному/аннулированному договору невозможна.")
    Payment.objects.create(contract=contract, date=date, amount=amount, doc_number=doc_number)
    log(f"Оплата {amount:,.2f} от {date:%d.%m.%Y} {doc_number}", user, contract=contract)


# ---------------------------------------------------------------- периодические задачи


def run_periodic(now=None):
    """Автозакрытие позиций и SLA-напоминания. Запускать по крону: manage.py run_periodic."""
    now = now or timezone.now()
    result = {"auto_closed": 0, "reminders": 0}
    limit = now - timedelta(days=cfg("AUTO_CLOSE_DAYS"))
    for item in MemoItem.objects.filter(status=S.DELIVERED, closed=False, delivered_at__lte=limit):
        item.closed = True
        item.save(update_fields=["closed"])
        recalc_item(item)
        log(f"Позиция {item.code} закрыта автоматически через {cfg('AUTO_CLOSE_DAYS')} дн. после поставки", memo=item.memo)
        result["auto_closed"] += 1

    # «Проблемная» зависит от даты — пересчитываем незавершённые СЗ.
    for memo in Memo.objects.exclude(status_code__in=["done", "rejected", "draft"]):
        recalc_memo(memo)

    sla = now - timedelta(days=cfg("URGENT_SLA_DAYS"))
    buyers = list(users_with_role(roles.BUYER))
    for item in MemoItem.objects.filter(urgent=True, status_changed_at__lte=sla).exclude(
        status__in=[S.DRAFT, S.ON_APPROVAL, S.REJECTED, S.WITHDRAWN, S.CLOSED, S.DELIVERED]
    ).select_related("memo"):
        key = f"sla:{item.pk}:{item.status}:{item.status_changed_at:%Y%m%d%H%M}"
        if ReminderLog.objects.filter(key=key).exists():
            continue
        ReminderLog.objects.create(key=key)
        active = item.active_procurement_line
        targets = [active.procurement.buyer] if active else buyers
        days = (now - item.status_changed_at).days
        notify(targets, f"СРОЧНО: позиция {item.code} «{item.description}» без движения {days} дн. "
                        f"(статус «{item.get_status_display()}»)", item.memo.get_absolute_url())
        result["reminders"] += 1
    return result
