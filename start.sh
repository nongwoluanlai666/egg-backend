#!/bin/sh

set -eu

cd /app
export TZ="${TZ:-Asia/Shanghai}"

python - <<'PY'
import os
import shlex

keys = [
    'PORT',
    'TZ',
    'MERCHANT_NOTIFY_JOB_TOKEN',
    'MERCHANT_NOTIFY_JOB_URL',
    'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS',
    'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS',
    'MERCHANT_NOTIFY_HTTP_TIMEOUT_SECONDS',
]

with open('/app/cron.env', 'w', encoding='utf-8') as handle:
    for key in keys:
        handle.write(f'export {key}={shlex.quote(os.environ.get(key, ""))}\n')
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
