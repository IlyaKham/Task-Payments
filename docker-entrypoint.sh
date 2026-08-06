#!/bin/sh
# Схема приводится к актуальной до старта приложения.
#
# Postgres в compose объявлен healthy до того, как отдаст первое
# соединение, поэтому упереться в "connection refused" на первой попытке
# — норма, а не сбой. Ждём в цикле, но не бесконечно: если база не
# поднялась за отведённое время, контейнер должен упасть громко, а не
# висеть в вечном ретрае.
set -eu

ATTEMPTS=${MIGRATION_ATTEMPTS:-30}
DELAY=${MIGRATION_RETRY_DELAY:-2}

attempt=1
while : ; do
    if alembic upgrade head; then
        break
    fi

    if [ "$attempt" -ge "$ATTEMPTS" ]; then
        echo "migrations: база недоступна после $ATTEMPTS попыток, сдаёмся" >&2
        exit 1
    fi

    echo "migrations: попытка $attempt из $ATTEMPTS не удалась, повтор через ${DELAY}s" >&2
    attempt=$((attempt + 1))
    sleep "$DELAY"
done

echo "migrations: схема актуальна"

# exec, чтобы uvicorn стал PID 1 и получал SIGTERM напрямую: без этого
# docker stop не даст диспетчеру корректно завершиться.
exec "$@"
