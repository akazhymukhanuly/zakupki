"""Настройки проекта «Потребности, закупки и договоры».

Все параметры окружения задаются переменными (см. .env.example).
По умолчанию — безопасный продакшен-режим: DEBUG выключен, SECRET_KEY обязателен.
Для локальной разработки start.sh выставляет DJANGO_DEBUG=1.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Простая загрузка .env (KEY=VALUE) без внешних зависимостей; переменные окружения важнее."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Задайте DJANGO_SECRET_KEY (см. .env.example)")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
# Локальные адреса нужны для healthcheck внутри контейнера.
ALLOWED_HOSTS += [h for h in ("127.0.0.1", "localhost") if h not in ALLOWED_HOSTS]
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")  # напр. https://zakupki.company.kz

# Демо-режим: подсказки логинов на странице входа. На боевом сервере — выключен.
DEMO_MODE = env_bool("DEMO_MODE", DEBUG)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "zakupki.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.nav",
            ],
        },
    },
]

WSGI_APPLICATION = "zakupki.wsgi.application"

# --- База данных: PostgreSQL на сервере, SQLite — для локального запуска.
if env("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER", "postgres"),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
        }
    }
# Каждый запрос — одна транзакция: при ошибке на середине действия данные не останутся «наполовину».
DATABASES["default"]["ATOMIC_REQUESTS"] = True

# Общий для всех процессов кэш в БД (нужен для ограничения попыток входа). Таблица: manage.py createcachetable
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "cache_table"}}

AUTH_PASSWORD_VALIDATORS = [] if DEBUG else [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = env("TIME_ZONE", "Asia/Almaty")
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", BASE_DIR / "media"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 12 * 60 * 60  # рабочий день
CSRF_FAILURE_VIEW = "core.auth.csrf_failure"

# --- HTTPS (за nginx / балансировщиком). Включается переменной DJANGO_HTTPS=1.
if env_bool("DJANGO_HTTPS"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

# --- Почта: уведомления дублируются на e-mail, если задан EMAIL_HOST.
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "zakupki@localhost")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
ADMINS = [("Admin", a) for a in env_list("DJANGO_ADMINS")]  # письма об ошибках 500
SITE_URL = env("SITE_URL", "http://127.0.0.1:8000").rstrip("/")  # для ссылок в письмах

# --- Логи: в stdout (docker / journald их собирают), ошибки — отдельно.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "mail_admins": {"class": "django.utils.log.AdminEmailHandler", "level": "ERROR"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console", "mail_admins"], "level": "ERROR", "propagate": False},
        "core": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
    },
}

# --- Бизнес-параметры (п. 5, 6 ТЗ) ---
PROCUREMENT = {
    # Автозакрытие позиции через N дней после полной поставки (п. 4.1).
    "AUTO_CLOSE_DAYS": int(env("AUTO_CLOSE_DAYS", "14")),
    # SLA: напоминание закупщику о срочной позиции без движения N дней (п. 6.6).
    "URGENT_SLA_DAYS": int(env("URGENT_SLA_DAYS", "2")),
    # Дэшборд: позиция в пуле «старая», если лежит дольше N дней.
    "POOL_STALE_DAYS": int(env("POOL_STALE_DAYS", "5")),
    # Дэшборд: требуемый срок «приближается», если до него меньше N дней.
    "DEADLINE_WARN_DAYS": int(env("DEADLINE_WARN_DAYS", "10")),
    # Интеграция с 1С: сворачивать консолидированные строки в одну при выгрузке (п. 6.1).
    "ONEC_COLLAPSE_CONSOLIDATED": env_bool("ONEC_COLLAPSE_CONSOLIDATED", False),
    # Папка обмена с 1С (автоматический режим, см. manage.py onec_exchange).
    "ONEC_EXCHANGE_DIR": env("ONEC_EXCHANGE_DIR", ""),
    # Ограничение перебора паролей: N неудачных попыток → блок на M минут.
    "LOGIN_MAX_ATTEMPTS": int(env("LOGIN_MAX_ATTEMPTS", "5")),
    "LOGIN_LOCK_MINUTES": int(env("LOGIN_LOCK_MINUTES", "15")),
    "CURRENCY": env("CURRENCY_SIGN", "₸"),
}
