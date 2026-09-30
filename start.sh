#!/bin/sh
# Запуск MVP одной командой: ./start.sh
set -e
cd "$(dirname "$0")"
# Окружение пересоздаётся, если его нет или оно сломалось после переноса папки.
if ! .venv/bin/python -c "import sys" >/dev/null 2>&1 || ! .venv/bin/pip --version >/dev/null 2>&1; then
  rm -rf .venv
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
# Локальный режим разработки/демо (на сервере используется .env из .env.example и docker compose).
[ -f .env ] || printf 'DJANGO_DEBUG=1\nDEMO_MODE=1\n' > .env
.venv/bin/python manage.py migrate -v0
.venv/bin/python manage.py createcachetable
.venv/bin/python manage.py seed_demo
echo "Откройте http://127.0.0.1:8000  (логины на странице входа, пароль demo12345)"
.venv/bin/python manage.py runserver 127.0.0.1:8000
