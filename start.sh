#!/bin/sh

set -eu

: "${TZ:=Asia/Shanghai}"
: "${COS_BUCKET:=7072-prod-6g1lay55fc214666-1422312924}"
: "${COS_REGION:=ap-shanghai}"
: "${MYSQL_ADDRESS:=10.5.103.163:3306}"
: "${MYSQL_DATABASE:=django_demo}"
: "${MYSQL_USERNAME:=root}"
: "${MYSQL_PASSWORD:=sXbhXf6E}"
: "${EGG_DEV_ADMIN_TOKEN:=EGG_DEV_ADMIN_TOKEN_xxw}"
: "${WECHAT_APP_ID:=wxde93838d75a20409}"
: "${WECHAT_APP_SECRET:=027bdcb188e4ff7ed6bc35b01cd199b8}"
: "${MERCHANT_NOTICE_TIMEZONE:=Asia/Shanghai}"
: "${MERCHANT_NOTIFY_TEMPLATE_ID:=NA9mVDvFObzNcV9QbXJbUfyoRw_XAw0fLYd8TvIKNpo}"

export TZ COS_BUCKET COS_REGION
export MYSQL_ADDRESS MYSQL_DATABASE MYSQL_USERNAME MYSQL_PASSWORD
export EGG_DEV_ADMIN_TOKEN WECHAT_APP_ID WECHAT_APP_SECRET
export MERCHANT_NOTICE_TIMEZONE MERCHANT_NOTIFY_TEMPLATE_ID

cd /app
export TZ="${TZ:-Asia/Shanghai}"

python - <<'PY'
import os
import shlex

keys = [
    'PORT',
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
    'MERCHANT_NOTIFY_PAGE',
    'MERCHANT_NOTIFY_SPECIAL_KEYWORDS',
    'MERCHANT_NOTIFY_DEFAULT_SELECTED_GOODS',
    'MERCHANT_NOTIFY_JOB_TOKEN',
    'MERCHANT_NOTIFY_JOB_URL',
    'MERCHANT_NOTIFY_FETCH_TIMEOUT_SECONDS',
    'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS',
    'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS',
    'MERCHANT_NOTIFY_TRIGGER_GUARD_SECONDS',
    'MERCHANT_NOTICE_CACHE_TTL_SECONDS',
    'MERCHANT_NOTICE_DAILY_REWARDED_STEP',
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
