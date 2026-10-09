from django.conf import settings

from . import roles


class _Perms:
    def __init__(self, user):
        self.user = user

    def __getitem__(self, key):
        # В шаблонах точка — оператор поиска, поэтому там пишем perm.memo_create.
        return roles.can(self.user, key.replace("_", ".", 1))


def nav(request):
    user = request.user
    if not user.is_authenticated:
        return {}
    return {
        "perm": _Perms(user),
        "my_roles": sorted(roles.user_roles(user)),
        "unread_count": user.notifications.filter(is_read=False).count(),
        "demo_mode": settings.DEMO_MODE,
        "help_corridors": _corridors,
        "demo_users": _demo_users() if settings.DEMO_MODE else [],
    }


def _demo_users():
    from .views.workspace import demo_users
    return demo_users()


def _corridors():
    from .models import Corridor
    return list(Corridor.objects.all())
