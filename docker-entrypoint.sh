#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/app"
DISC_BIN="${APP_DIR}/.venv/bin/discorsair"
CONFIG_PATH="${DISCORSAIR_CONFIG:-${APP_DIR}/config/app.json}"
TEMPLATE_PATH="${APP_DIR}/config/app.json.template"
FLARESOLVERR_URL="${FLARESOLVERR_INTERNAL_URL:-http://127.0.0.1:8191}"
SERVER_HOST="${DISCORSAIR_SERVER_HOST:-0.0.0.0}"
SERVER_PORT="${DISCORSAIR_SERVER_PORT:-17880}"
STARTUP_TIMEOUT_SECS="${FLARESOLVERR_STARTUP_TIMEOUT_SECS:-60}"

FS_PID=""
APP_PID=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for pid in "${APP_PID}" "${FS_PID}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${APP_PID}" "${FS_PID}"; do
    if [[ -n "${pid}" ]]; then
      wait "${pid}" 2>/dev/null || true
    fi
  done
  exit "${status}"
}

wait_for_flaresolverr() {
  local deadline=$((SECONDS + STARTUP_TIMEOUT_SECS))
  until curl --silent --show-error "${FLARESOLVERR_URL}/" >/dev/null 2>&1; do
    if [[ -n "${FS_PID}" ]] && ! kill -0 "${FS_PID}" 2>/dev/null; then
      echo "flaresolverr exited before becoming ready" >&2
      wait "${FS_PID}" || true
      return 1
    fi
    if (( SECONDS >= deadline )); then
      echo "timed out waiting for flaresolverr at ${FLARESOLVERR_URL}" >&2
      return 1
    fi
    sleep 1
  done
}

trap cleanup EXIT INT TERM

if [[ ! -x "${DISC_BIN}" ]]; then
  echo "discorsair binary not found: ${DISC_BIN}" >&2
  exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "discorsair config not found: ${CONFIG_PATH}" >&2
  if [[ -f "${TEMPLATE_PATH}" ]]; then
    echo "copy ${TEMPLATE_PATH} to ${CONFIG_PATH} or mount your own config file" >&2
  fi
  exit 1
fi

mkdir -p /data /data/locks

flaresolverr &
FS_PID=$!

wait_for_flaresolverr

if (($# > 0)); then
  "$@" &
else
  "${DISC_BIN}" --config "${CONFIG_PATH}" serve --host "${SERVER_HOST}" --port "${SERVER_PORT}" &
fi
APP_PID=$!

wait -n "${APP_PID}" "${FS_PID}"
status=$?

if kill -0 "${APP_PID}" 2>/dev/null; then
  echo "flaresolverr exited unexpectedly" >&2
fi

exit "${status}"
