#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p backups
stamp="$(date +%F)"

tar czf "backups/site-${stamp}.tgz" site

# Postgres is added when the market API lands; skip cleanly in the static preview.
if docker compose ps --status running --services 2>/dev/null | grep -qx postgres; then
  # shellcheck disable=SC1091
  set -a
  . ./.env
  set +a
  docker compose exec -T postgres pg_dump -U "${POSTGRES_USER}" "${POSTGRES_DB}" \
    | gzip > "backups/db-${stamp}.sql.gz"
fi

find backups -type f -mtime +14 -delete
