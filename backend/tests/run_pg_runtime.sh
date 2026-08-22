#!/usr/bin/env bash
# P03.1C — roda pg_runtime_check.sql num cluster PostgreSQL DESCARTÁVEL (socket
# unix, sem TCP/rede, NUNCA Railway/produção). Requer os binários postgres locais
# (initdb/pg_ctl/psql). Sai 0 se PG_RUNTIME_OK.
set -euo pipefail
export LC_ALL=C LANG=C
PGBIN="${PGBIN:-/opt/homebrew/bin}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SQL="$HERE/pg_runtime_check.sql"
PGDATA="$(mktemp -d /tmp/p03c_pg.XXXXXX)"
SOCK="$(mktemp -d /tmp/p03c_sock.XXXXXX)"
cleanup() { "$PGBIN/pg_ctl" -D "$PGDATA" -w stop >/dev/null 2>&1 || true; rm -rf "$PGDATA" "$SOCK"; }
trap cleanup EXIT

if [ ! -x "$PGBIN/initdb" ]; then
  echo "POSTGRES_RUNTIME_TEST=BLOCKED (sem binários postgres em $PGBIN)"; exit 2
fi
"$PGBIN/initdb" -D "$PGDATA" -U p03 -A trust --locale=C >/dev/null 2>&1
"$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses='' -k $SOCK" -l "$PGDATA/log" -w start >/dev/null 2>&1
"$PGBIN/createdb" -h "$SOCK" -U p03 p03db
"$PGBIN/psql" -h "$SOCK" -U p03 -d p03db -v ON_ERROR_STOP=1 -q -f "$SQL"
