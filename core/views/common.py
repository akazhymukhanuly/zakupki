from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect

from ..services import BusinessError


def attempt(request, fn, success=None, redirect_to=None):
    """Выполнить действие; BusinessError показать пользователю. Возвращает (ok, result)."""
    try:
        result = fn()
    except BusinessError as e:
        messages.error(request, str(e))
        return (False, redirect(redirect_to) if redirect_to else None)
    if success:
        messages.success(request, success)
    return (True, result)


def dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace(" ", "").replace("\xa0", ""))
    except InvalidOperation:
        raise BusinessError(f"Не число: «{value}»")


def posint(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        raise BusinessError(f"Не целое число: «{value}»")


def back(request, fallback):
    return redirect(request.POST.get("next") or request.META.get("HTTP_REFERER") or fallback)


def paginate(request, qs, per_page=50):
    """Страница списка + строка параметров без page (для ссылок пагинации)."""
    page = Paginator(qs, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


def honor_next(view):
    """После POST-действия вернуться туда, откуда пришли (?next= или поле next) — нужно для карточки на главном экране."""
    from functools import wraps

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = view(request, *args, **kwargs)
        nxt = request.POST.get("next") if request.method == "POST" else None
        if nxt and nxt.startswith("/") and not nxt.startswith("//") and getattr(resp, "status_code", 0) == 302:
            return redirect(nxt)
        return resp
    return wrapper
