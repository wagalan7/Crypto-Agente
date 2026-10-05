#!/usr/bin/env bash
# Lote 02 — regressão + circuito real, PG16 UTF-8 descartável, só Unix socket.
set -euo pipefail
export LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1
lote02_pg_bin="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
lote02_python="${PY311:-/Users/alanmalta/Agente de IA Crypto/backend/.venv311/bin/python}"
lote02_here="$(cd "$(dirname "$0")" && pwd)"
lote02_backend="$(cd "$lote02_here/.." && pwd)"
if [[ ! -x "$lote02_pg_bin/initdb" || ! -x "$lote02_python" ]]; then
  echo "LOTE02_PG_BLOCKED: PostgreSQL 16/Python indisponível"; exit 2
fi
if [[ "$("$lote02_pg_bin/initdb" --version)" != *" 16."* ]]; then
  echo "LOTE02_PG_BLOCKED: exige PostgreSQL 16"; exit 2
fi
lote02_pg_data="$(mktemp -d /tmp/cw-lote02-pg.XXXXXX)"
lote02_pg_socket="$(mktemp -d /tmp/cw-lote02-sock.XXXXXX)"
lote02_cleanup() {
  "$lote02_pg_bin/pg_ctl" -D "$lote02_pg_data" -m immediate -w stop >/dev/null 2>&1 || true
  # Somente diretórios concretos criados por ESTE runner, nunca repo/variável ampla.
  if [[ "$lote02_pg_data" =~ ^/tmp/cw-lote02-pg\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$lote02_pg_data"
  fi
  if [[ "$lote02_pg_socket" =~ ^/tmp/cw-lote02-sock\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$lote02_pg_socket"
  fi
}
trap lote02_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$lote02_pg_bin/initdb" -D "$lote02_pg_data" -U lote02 -A trust --locale=C -E UTF8 >/dev/null
"$lote02_pg_bin/pg_ctl" -D "$lote02_pg_data" -l "$lote02_pg_data/log" \
  -o "-c listen_addresses='' -k $lote02_pg_socket" -w start >/dev/null
"$lote02_pg_bin/createdb" -h "$lote02_pg_socket" -U lote02 lote02db
export LOTE02_TEST_SOCKET="$lote02_pg_socket"
export DATABASE_URL="postgresql+asyncpg://lote02@/lote02db?host=$lote02_pg_socket"
cd "$lote02_backend"
"$lote02_python" -B tests/pg_integration_lote02_closure.py
