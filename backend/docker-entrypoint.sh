#!/bin/sh
# Applies database migrations (MONXU_MIGRATE=1) and optionally loads the demo tenant
# (MONXU_SEED_DEMO=1, never in production), then runs the given command.
set -e
if [ "${MONXU_MIGRATE:-0}" = "1" ]; then
  echo "Applying database migrations…"
  alembic -c /app/monxuplan/alembic.ini upgrade head
fi
if [ "${MONXU_SEED_DEMO:-0}" = "1" ] && [ "${MONXU_ENV:-development}" != "production" ]; then
  python -m monxuplan.seed.demo
fi
exec "$@"
