#!/bin/sh

set -eu

if [ -f /app/cron.env ]; then
  # shellcheck disable=SC1091
  . /app/cron.env
fi

PORT_VALUE="${PORT:-80}"
JOB_URL="${MERCHANT_NOTIFY_JOB_URL:-http://127.0.0.1:${PORT_VALUE}/api/internal/merchant-watch}"
JOB_TOKEN="${MERCHANT_NOTIFY_JOB_TOKEN:-}"
POLL_TIMEOUT_SECONDS="${MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS:-30}"
HTTP_TIMEOUT_SECONDS="${MERCHANT_NOTIFY_HTTP_TIMEOUT_SECONDS:-1200}"

echo "[merchant-watch] trigger start $(date '+%Y-%m-%d %H:%M:%S') ${JOB_URL}"

if [ -n "${JOB_TOKEN}" ]; then
  curl --silent --show-error --fail \
    --max-time "${HTTP_TIMEOUT_SECONDS}" \
    -X POST "${JOB_URL}" \
    -H "Content-Type: application/json" \
    -H "X-Merchant-Job-Token: ${JOB_TOKEN}" \
    --data "{\"timeoutSeconds\": ${POLL_TIMEOUT_SECONDS}, \"pollIntervalSeconds\": ${POLL_INTERVAL_SECONDS}}"
else
  curl --silent --show-error --fail \
    --max-time "${HTTP_TIMEOUT_SECONDS}" \
    -X POST "${JOB_URL}" \
    -H "Content-Type: application/json" \
    --data "{\"timeoutSeconds\": ${POLL_TIMEOUT_SECONDS}, \"pollIntervalSeconds\": ${POLL_INTERVAL_SECONDS}}"
fi

echo
echo "[merchant-watch] trigger done $(date '+%Y-%m-%d %H:%M:%S')"
