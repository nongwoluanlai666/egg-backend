#!/bin/sh

set -eu

cd /app

if [ -f /app/cron.env ]; then
  # shellcheck disable=SC1091
  . /app/cron.env
fi

: "${TZ:=Asia/Shanghai}"
: "${MYSQL_ADDRESS:=10.5.103.163:3306}"
: "${MYSQL_DATABASE:=django_demo_test}"
: "${MYSQL_USERNAME:=root}"
: "${MYSQL_PASSWORD:=sXbhXf6E}"
: "${WECHAT_APP_ID:=wxde93838d75a20409}"
: "${WECHAT_APP_SECRET:=027bdcb188e4ff7ed6bc35b01cd199b8}"
: "${MERCHANT_NOTICE_TIMEZONE:=Asia/Shanghai}"
: "${MERCHANT_NOTIFY_TEMPLATE_ID:=NA9mVDvFObzNcV9QbXJbUfyoRw_XAw0fLYd8TvIKNpo}"

export TZ MYSQL_ADDRESS MYSQL_DATABASE MYSQL_USERNAME MYSQL_PASSWORD
export WECHAT_APP_ID WECHAT_APP_SECRET MERCHANT_NOTICE_TIMEZONE MERCHANT_NOTIFY_TEMPLATE_ID

PORT_VALUE="${PORT:-80}"
POLL_TIMEOUT_SECONDS="${MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS:-30}"

echo "[merchant-watch] trigger start $(date '+%Y-%m-%d %H:%M:%S') port=${PORT_VALUE} cwd=$(pwd)"
python /app/manage.py watch_merchant_notice \
  --timeout-seconds "${POLL_TIMEOUT_SECONDS}" \
  --poll-interval-seconds "${POLL_INTERVAL_SECONDS}"

echo
echo "[merchant-watch] trigger done $(date '+%Y-%m-%d %H:%M:%S')"
