#!/bin/sh

set -eu

cd /app

python - <<'PY'
import os
import shlex

keys = [
    'PORT',
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
echo "[startup] starting gunicorn on port ${PORT:-80}"

exec gunicorn wxcloudrun.wsgi:application \
  --bind "0.0.0.0:${PORT:-80}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-180}"
