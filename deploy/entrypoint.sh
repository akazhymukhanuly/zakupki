#!/bin/sh
# Запуск веб-приложения: миграции → кэш-таблица → gunicorn.
set -e
python manage.py migrate --noinput
python manage.py createcachetable
exec gunicorn zakupki.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --timeout 120 \
  --max-requests 1000 --max-requests-jitter 100 \
  --access-logfile - --error-logfile -
