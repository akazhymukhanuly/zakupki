"""Вход с ограничением перебора паролей и проверка здоровья сервиса."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse

log = logging.getLogger("core.auth")


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")


class LoginView(auth_views.LoginView):
    template_name = "core/login.html"

    def _key(self):
        username = (self.request.POST.get("username") or "").lower()[:150]
        return f"login-fail:{_client_ip(self.request)}:{username}"

    def post(self, request, *args, **kwargs):
        cfg = settings.PROCUREMENT
        fails = cache.get(self._key(), 0)
        if fails >= cfg["LOGIN_MAX_ATTEMPTS"]:
            messages.error(request, f"Слишком много неудачных попыток. Повторите через {cfg['LOGIN_LOCK_MINUTES']} мин.")
            log.warning("Login locked: %s", self._key())
            return self.get(request, *args, **kwargs)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        key = self._key()
        cache.set(key, cache.get(key, 0) + 1, settings.PROCUREMENT["LOGIN_LOCK_MINUTES"] * 60)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._key())
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["demo_mode"] = settings.DEMO_MODE
        if settings.DEMO_MODE:
            from django.contrib.auth.models import User
            ctx["demo_login_users"] = User.objects.filter(is_active=True).select_related("profile").order_by("last_name")
        return ctx


def health(request):
    """Для мониторинга / docker healthcheck: 200 — приложение и БД доступны."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception as e:  # noqa: BLE001 — мониторингу нужен любой сбой
        log.exception("Healthcheck failed")
        return JsonResponse({"status": "error", "detail": str(e)[:200]}, status=503)


def csrf_failure(request, reason=""):
    from django.shortcuts import render
    log.warning("CSRF failure %s: %s", request.path, reason)
    return render(request, "403_csrf.html", status=403)
