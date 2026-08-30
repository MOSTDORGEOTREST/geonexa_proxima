#!/usr/bin/env sh
# Один вход на все сервисы: api, bot, worker.
#
# Подъём схемы и сидирование делает сам сервис (bootstrap.start_service):
# так поведение одинаково и в Docker, и при локальном запуске без контейнеров,
# и не расходится между тремя Dockerfile.
set -eu

SERVICE="${1:-api}"
shift 2>/dev/null || true

echo "[proxima] запускаю ${SERVICE}"

case "${SERVICE}" in
  api)
    exec python -m uvicorn geonexa_proxima.api:app \
      --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}" \
      --workers "${API_WORKERS:-1}" "$@"
    ;;
  bot)
    exec python -m geonexa_proxima.cli bot "$@"
    ;;
  worker)
    # Деплойменты регистрируются до старта воркера: иначе админка увидит
    # пустой список флоу и не сможет ничего запустить вручную.
    if ! python -m geonexa_proxima.cli prefect deploy; then
      echo "[proxima] ВНИМАНИЕ: деплойменты не зарегистрированы." >&2
      echo "[proxima] Воркер стартует, но расписания выполняться не будут и" >&2
      echo "[proxima] в админке список флоу будет пуст. Причина — в трейсбеке выше." >&2
    fi
    exec python -m prefect worker start \
      --pool "${PREFECT_WORK_POOL:-geonexa-pool}" \
      --type process "$@"
    ;;
  migrate)
    exec python -m geonexa_proxima.cli db upgrade "$@"
    ;;
  shell)
    exec "$@"
    ;;
  *)
    echo "[proxima] неизвестный сервис: ${SERVICE}" >&2
    echo "доступны: api, bot, worker, migrate, shell" >&2
    exit 64
    ;;
esac
