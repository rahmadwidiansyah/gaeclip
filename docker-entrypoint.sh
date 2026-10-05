#!/usr/bin/env bash
# Mono supervisor: API (uvicorn) + worker (celery) + frontend (node) in one
# container. Exits non-zero when ANY child dies (fail fast, let the
# orchestrator restart us); forwards SIGTERM/SIGINT to all children.
set -u

# Worker options resolved like the old worker/entrypoint.sh: persistent app
# settings (DB -> env -> default). Never prints secrets.
VALUES=$(python3 -c "
try:
    import os
    import redis
    from gaeclip.app_settings import AppSettings
    client = redis.Redis.from_url(
        os.getenv('REDIS_URL', 'redis://redis:6379/0'),
        decode_responses=False, socket_timeout=10,
    )
    service = AppSettings(client)
    service.migrate_from_env()
    conc = int(service.get('WORKER_CONCURRENCY'))
    level = str(service.get('LOG_LEVEL')).upper()
    if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
        level = 'INFO'
    print(f'{conc} {level}')
except Exception:
    print('1 INFO')
" 2>/dev/null) || VALUES="1 INFO"

CONCURRENCY=$(echo "$VALUES" | cut -d' ' -f1)
LOGLEVEL=$(echo "$VALUES" | cut -d' ' -f2)
case "$CONCURRENCY" in ''|*[!0-9]*) CONCURRENCY=1 ;; esac
case "$LOGLEVEL" in DEBUG|INFO|WARNING|ERROR) ;; *) LOGLEVEL=INFO ;; esac

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

pids=()
uvicorn gaeclip.api:create_app --factory --host "$API_HOST" --port "$API_PORT" &
pids+=($!)
celery -A gaeclip.celery_app:app worker --loglevel="$LOGLEVEL" --concurrency="$CONCURRENCY" &
pids+=($!)
# Next.js standalone must run with its own dir as CWD (resolves .next/).
(cd /app/frontend && exec node server.js -p "$FRONTEND_PORT") &
pids+=($!)

shutdown() {
  kill "${pids[@]}" 2>/dev/null || true
}
trap shutdown TERM INT

echo "gaeclip mono: api=:${API_PORT} frontend=:${FRONTEND_PORT} worker_concurrency=${CONCURRENCY} (pids: ${pids[*]})"
while true; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      code=$?
      echo "gaeclip mono: pid $pid exited ($code), shutting down siblings"
      shutdown
      wait
      exit "$code"
    fi
  done
  sleep 2
done
