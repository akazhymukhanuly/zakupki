from decimal import Decimal

from django import template
from django.conf import settings
from django.utils.html import format_html

register = template.Library()

STATUS_COLORS = {
    # позиции
    "draft": "secondary", "on_approval": "info", "rejected": "danger", "approved": "primary",
    "in_procurement": "warning", "supplier_selected": "warning", "partially_contracted": "orange",
    "contracted": "teal", "partially_delivered": "teal", "delivered": "success", "closed": "dark", "withdrawn": "light",
    # СЗ сводный
    "new": "primary", "in_work": "warning", "done": "success", "problem": "danger",
    "partially_approved": "primary",
    # закупка
    "rfq": "info", "collecting": "info", "analysis": "warning", "decided": "primary", "contracts": "teal",
    "cancelled": "light", "none": "secondary", "pending": "warning",
    # договор
    "signing": "info", "signed": "primary", "execution": "warning", "executed": "success",
}


@register.simple_tag
def badge(code, label):
    color = STATUS_COLORS.get(code, "secondary")
    return format_html('<span class="badge st-{}">{}</span>', color, label)


@register.filter
def qty(value):
    """Количество без лишних нулей: 50.000 → 50, 2.500 → 2,5."""
    if value is None or value == "":
        return ""
    d = Decimal(value).normalize()
    s = f"{d:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    ip, _, fp = s.partition(".")
    ip = f"{int(ip):,}".replace(",", " ") if ip.lstrip("-").isdigit() else ip
    return ip + ("," + fp if fp else "")


@register.filter
def money(value):
    if value is None or value == "":
        return "—"
    s = f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return s


@register.filter
def money0(value):
    if value is None or value == "":
        return "—"
    return f"{Decimal(value):,.0f}".replace(",", " ")


@register.simple_tag
def cur():
    return settings.PROCUREMENT["CURRENCY"]


@register.filter
def get(d, key):
    if d is None:
        return None
    return d.get(key) if hasattr(d, "get") else None


@register.filter
def pct_class(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "bg-secondary"
    if v >= 100:
        return "bg-success"
    if v > 0:
        return "bg-warning"
    return "bg-secondary"


@register.simple_tag
def keyjoin(a, b):
    return f"{a}_{b}"
