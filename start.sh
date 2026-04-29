#!/bin/sh

set -eu

cd /app
export TZ="${TZ:-Asia/Shanghai}"

python - <<'PY'
import os
import shlex

keys = [
    'TZ',
    'MYSQL_ADDRESS',
    'MYSQL_DATABASE',
    'MYSQL_USERNAME',
    'MYSQL_PASSWORD',
    'WECHAT_APP_ID',
    'WECHAT_APP_SECRET',
    'MERCHANT_NOTICE_TIMEZONE',
    'MERCHANT_SOURCE_URL',
    'MERCHANT_SOURCE_REFERER',
    'MERCHANT_SOURCE_USER_AGENT',
    'MERCHANT_SOURCE_PRIORITY',
    'MERCHANT_BACKUP_SOURCE_URL',
    'MERCHANT_BACKUP_SOURCE_REFERER',
    'MERCHANT_BACKUP_SOURCE_API_KEY',
    'MERCHANT_NOTIFY_TEMPLATE_ID',
    'MERCHANT_NOTIFY_SPECIAL_KEYWORDS',
    'MERCHANT_NOTIFY_DEFAULT_SELECTED_GOODS',
    'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS',
    'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS',
    'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS',
    'MERCHANT_NOTIFY_TRIGGER_GUARD_SECONDS',
    'MERCHANT_NOTICE_CACHE_TTL_SECONDS',
]

with open('/app/cron.env', 'w', encoding='utf-8') as handle:
    for key in keys:
        value = os.environ.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        handle.write(f'export {key}={shlex.quote(value)}\n')
PY

chmod 600 /app/cron.env

echo "[startup] applying database migrations"
python manage.py migrate --noinput

echo "[startup] starting cron"
cron

echo "[startup] cron ready"
if [ "${MERCHANT_NOTICE_DISPATCH_WORKER_ENABLED:-1}" != "0" ]; then
  echo "[startup] starting merchant notice dispatch worker"
  python manage.py run_merchant_notice_dispatch_worker &
fi

echo "[startup] starting gunicorn on port ${PORT:-80}"

exec gunicorn wxcloudrun.wsgi:application \
  --bind "0.0.0.0:${PORT:-80}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-180}"
