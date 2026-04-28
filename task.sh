#!/bin/sh

set -eu

if [ -f /app/cron.env ]; then
  # shellcheck disable=SC1091
  . /app/cron.env
fi

PORT_VALUE="${PORT:-80}"
POLL_TIMEOUT_SECONDS="${MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS:-30}"

echo "[merchant-watch] trigger start $(date '+%Y-%m-%d %H:%M:%S') port=${PORT_VALUE}"
python manage.py watch_merchant_notice \
  --timeout-seconds "${POLL_TIMEOUT_SECONDS}" \
  --poll-interval-seconds "${POLL_INTERVAL_SECONDS}"

echo
echo "[merchant-watch] trigger done $(date '+%Y-%m-%d %H:%M:%S')"
