"""Роли и права (п. 10 ТЗ — предложение, т.к. раздел в ТЗ не заполнен).

Роли реализованы через группы Django; у пользователя может быть несколько ролей.
"""
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

INITIATOR = "Инициатор"
APPROVER = "Руководитель подразделения"   # согласует СЗ своего подразделения (шаг 1 ТЗ)
BUYER = "Закупки (ОМТС)"
CHIEF = "Главный инженер"
CFO = "Финансы (ФЭО)"
LAWYER = "Юристы (ЮО)"
DIRECTOR = "Генеральный директор"
PTO = "ПТО"
ACCOUNTANT = "Бухгалтерия"
ADMIN = "Администратор"

ALL_ROLES = [INITIATOR, APPROVER, BUYER, CHIEF, CFO, LAWYER, DIRECTOR, PTO, ACCOUNTANT, ADMIN]

# Короткие подписи ролей для чипов согласования
SHORT = {
    INITIATOR: "Инициатор", APPROVER: "Руководитель", BUYER: "Закупки", CHIEF: "Главный инженер",
    CFO: "Финансы", LAWYER: "Юристы", DIRECTOR: "Ген. директор", PTO: "ПТО", ACCOUNTANT: "Бухгалтерия",
    ADMIN: "Администратор",
}

DECIDERS = [CHIEF, CFO, BUYER, LAWYER, DIRECTOR, PTO]
EVERYONE_BUT_INITIATOR = [APPROVER, BUYER, CHIEF, CFO, LAWYER, DIRECTOR, PTO, ACCOUNTANT, ADMIN]
MANAGEMENT = [CHIEF, DIRECTOR, CFO, ADMIN]

# Матрица прав: действие → роли, которым оно разрешено.
PERMISSIONS = {
    "memo.create": [INITIATOR, APPROVER, BUYER, PTO, ADMIN],
    "memo.view_all": [BUYER, CHIEF, CFO, LAWYER, DIRECTOR, PTO, ACCOUNTANT, ADMIN],
    "memo.approve": [APPROVER, CHIEF, ADMIN],
    "pool.view": [BUYER, CHIEF, ADMIN],
    "procurement.manage": [BUYER, ADMIN],
    "procurement.view": [BUYER, CHIEF, CFO, LAWYER, DIRECTOR, PTO, ACCOUNTANT, ADMIN],
    "decision.approve": DECIDERS + [ADMIN],
    "contract.manage": [BUYER, ADMIN],
    "contract.view": [BUYER, CHIEF, CFO, LAWYER, DIRECTOR, PTO, ACCOUNTANT, ADMIN],
    "contract.finance": [CFO, ACCOUNTANT, BUYER, ADMIN],
    "integration.1c": [ACCOUNTANT, CFO, BUYER, ADMIN],
    "reports.view": [BUYER, CHIEF, CFO, DIRECTOR, ADMIN],
    "dashboard.buyer": [BUYER, ADMIN],
    "director.overview": MANAGEMENT,
    "refs.import": [ADMIN],
    "access.manage": [ADMIN],
}

PERMISSION_LABELS = {
    "memo.create": "Создание СЗ (потребности)",
    "memo.view_all": "Просмотр всех СЗ",
    "memo.approve": "Согласование СЗ подразделения",
    "pool.view": "Пул потребностей",
    "procurement.manage": "Расценка: закупки, КП, решение, договоры",
    "procurement.view": "Просмотр закупок",
    "decision.approve": "Согласование по коридору (если роль входит в коридор)",
    "contract.manage": "Создание и ведение договоров",
    "contract.view": "Просмотр договоров",
    "contract.finance": "Оплаты (транши) и поступления",
    "integration.1c": "Обмен с 1С",
    "reports.view": "Отчёты",
    "dashboard.buyer": "Дэшборд закупщика",
    "director.overview": "Обзор руководителя",
    "refs.import": "Загрузка справочников из Excel",
    "access.manage": "Настройка доступа к показателям",
}


def sees_prices(user):
    """Инициатор цены не видит и не указывает (как в макете): только те, у кого есть другая роль."""
    r = user_roles(user)
    return bool(r - {INITIATOR})


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
