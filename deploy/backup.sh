#!/bin/sh
# Ежедневный бэкап PostgreSQL, хранится BACKUP_KEEP_DAYS дней (по умолчанию 14).
set -e
while true; do
  f="/backups/zakupki_$(date +%Y%m%d_%H%M).sql.gz"
  if pg_dump -h db -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$f.tmp"; then
    mv "$f.tmp" "$f" && echo "backup ok: $f"
  else
    rm -f "$f.tmp"; echo "BACKUP FAILED" >&2
  fi
  find /backups -name 'zakupki_*.sql.gz' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
  sleep 86400
done
