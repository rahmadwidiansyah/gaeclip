#!/bin/sh
# Resolve runtime worker options from persistent app settings (DB -> env
# -> default), then start Celery. Never prints secrets.
set -u

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

exec celery -A gaeclip.celery_app:app worker --loglevel="$LOGLEVEL" --concurrency="$CONCURRENCY"
