"""Роли и права (п. 10 ТЗ — предложение, т.к. раздел в ТЗ не заполнен).

Роли реализованы через группы Django; у пользователя может быть несколько ролей.
"""
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

INITIATOR = "Инициатор"
APPROVER = "Согласующий"
BUYER = "Закупщик"
DIRECTOR = "Директор"
CFO = "Финдиректор"
ACCOUNTANT = "Бухгалтер"
ADMIN = "Администратор"

ALL_ROLES = [INITIATOR, APPROVER, BUYER, DIRECTOR, CFO, ACCOUNTANT, ADMIN]

# Матрица прав: действие → роли, которым оно разрешено.
PERMISSIONS = {
    "memo.create": [INITIATOR, BUYER, ADMIN],
    "memo.view_all": [BUYER, DIRECTOR, CFO, ACCOUNTANT, ADMIN],
    "memo.approve": [APPROVER, DIRECTOR, ADMIN],
    "pool.view": [BUYER, DIRECTOR, ADMIN],
    "procurement.manage": [BUYER, ADMIN],
    "procurement.view": [BUYER, DIRECTOR, CFO, ACCOUNTANT, ADMIN],
    "decision.approve": [DIRECTOR, CFO, ADMIN],
    "contract.manage": [BUYER, ADMIN],
    "contract.view": [BUYER, DIRECTOR, CFO, ACCOUNTANT, ADMIN],
    "contract.finance": [ACCOUNTANT, BUYER, ADMIN],
    "integration.1c": [ACCOUNTANT, BUYER, ADMIN],
    "reports.view": [BUYER, DIRECTOR, CFO, ADMIN],
    "dashboard.buyer": [BUYER, ADMIN],
    "refs.import": [ADMIN],
}

PERMISSION_LABELS = {
    "memo.create": "Создание СЗ",
    "memo.view_all": "Просмотр всех СЗ",
    "memo.approve": "Согласование СЗ",
    "pool.view": "Пул потребностей",
    "procurement.manage": "Ведение закупок, ввод КП, решение",
    "procurement.view": "Просмотр закупок",
    "decision.approve": "Согласование решения по закупке (по порогам)",
    "contract.manage": "Создание и ведение договоров",
    "contract.view": "Просмотр договоров",
    "contract.finance": "Ввод оплат и поступлений",
    "integration.1c": "Обмен с 1С",
    "reports.view": "Отчёты",
    "dashboard.buyer": "Дэшборд закупщика",
    "refs.import": "Загрузка справочников из Excel",
}


def user_roles(user):
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(ALL_ROLES)
    if not hasattr(user, "_role_cache"):
        user._role_cache = set(user.groups.values_list("name", flat=True))
    return user._role_cache


def has_role(user, *roles):
    return bool(user_roles(user) & set(roles))


def can(user, perm):
    return has_role(user, *PERMISSIONS[perm])


def require(perm):
    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("login")
            if not can(request.user, perm):
                raise PermissionDenied(f"Нет права: {PERMISSION_LABELS.get(perm, perm)}")
            return view(request, *args, **kwargs)

        return wrapper

    return deco


def deny(request, text, url):
    messages.error(request, text)
    return redirect(url)
