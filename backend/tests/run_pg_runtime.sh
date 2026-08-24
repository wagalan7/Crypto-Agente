#!/usr/bin/env bash
# P03.1E — roda a INTEGRAÇÃO PostgreSQL REAL (código real: record_incident /
# _SqlIncidentRepo / risk_service via SQLAlchemy+asyncpg) num cluster PostgreSQL 16
# DESCARTÁVEL (socket unix, listen_addresses='', sem TCP/rede, nunca Railway).
# Usa o Python 3.11 real com asyncpg. Sai 0 só se PG_INTEGRATION_OK.
set -euo pipefail
export LC_ALL=C LANG=C
PGBIN="${PGBIN:-/opt/homebrew/bin}"
PY311="${PY311:-/Users/alanmalta/Agente de IA Crypto/backend/.venv311/bin/python}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(cd "$HERE/.." && pwd)"
PGDATA="$(mktemp -d /tmp/p03e_pg.XXXXXX)"
SOCK="$(mktemp -d /tmp/p03e_sock.XXXXXX)"
cleanup() { "$PGBIN/pg_ctl" -D "$PGDATA" -w stop >/dev/null 2>&1 || true; rm -rf "$PGDATA" "$SOCK"; }
trap cleanup EXIT

if [ ! -x "$PGBIN/initdb" ] || [ ! -x "$PY311" ]; then
  echo "POSTGRES_RUNTIME_TEST=BLOCKED (faltam binários postgres ou python3.11)"; exit 2
fi
"$PGBIN/initdb" -D "$PGDATA" -U p03 -A trust --locale=C >/dev/null 2>&1
"$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses='' -k $SOCK" -l "$PGDATA/log" -w start >/dev/null 2>&1
"$PGBIN/createdb" -h "$SOCK" -U p03 p03db

# asyncpg via socket unix (sem TCP). host= aponta pro diretório do socket.
export DATABASE_URL="postgresql+asyncpg://p03@/p03db?host=$SOCK"

# (auxiliar, NÃO conta como aprovação) — checagem de expressões via psql:
if [ "${RUN_PSQL_AUX:-0}" = "1" ]; then
  "$PGBIN/psql" -h "$SOCK" -U p03 -d p03db -v ON_ERROR_STOP=1 -q -f "$HERE/pg_runtime_check.sql" || true
fi

# PRINCIPAL: integração pelo CÓDIGO REAL.
cd "$BACKEND"
"$PY311" tests/pg_integration_p03.py
