#!/bin/sh
# Runs a service under uvicorn. SERVICE_MODULE and PORT are provided by compose.
set -eu

: "${SERVICE_MODULE:?SERVICE_MODULE must be set, e.g. order_service.main:app}"
: "${PORT:=8000}"

exec uvicorn "${SERVICE_MODULE}" \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --no-access-log \
  --proxy-headers \
  --timeout-graceful-shutdown 20
