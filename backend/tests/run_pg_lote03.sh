#!/usr/bin/env bash
# Matriz Lote 03: PG16 UTF-8 descartável, somente socket Unix.
set -euo pipefail
export LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1
lote03_pg_bin="${PGBIN:-/opt/homebrew/opt/postgresql@16/bin}"
lote03_python="${PY311:-/Users/alanmalta/Agente de IA Crypto/backend/.venv311/bin/python}"
lote03_here="$(cd "$(dirname "$0")" && pwd)"
lote03_backend="$(cd "$lote03_here/.." && pwd)"
if [[ ! -x "$lote03_pg_bin/initdb" || ! -x "$lote03_python" ]]; then
  echo "LOTE03_PG_BLOCKED: PostgreSQL/Python indisponível"; exit 2
fi
if [[ "$("$lote03_pg_bin/initdb" --version)" != *" 16."* ]]; then
  echo "LOTE03_PG_BLOCKED: exige PostgreSQL 16"; exit 2
fi
lote03_pg_data="$(mktemp -d /tmp/cw-lote03-pg.XXXXXX)"
lote03_pg_socket="$(mktemp -d /tmp/cw-lote03-sock.XXXXXX)"
lote03_cleanup() {
  "$lote03_pg_bin/pg_ctl" -D "$lote03_pg_data" -m immediate -w stop >/dev/null 2>&1 || true
  # Só caminhos concretos criados por este runner e conferidos pelo prefixo.
  if [[ "$lote03_pg_data" =~ ^/tmp/cw-lote03-pg\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$lote03_pg_data"
  fi
  if [[ "$lote03_pg_socket" =~ ^/tmp/cw-lote03-sock\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$lote03_pg_socket"
  fi
}
trap lote03_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$lote03_pg_bin/initdb" -D "$lote03_pg_data" -U lote03 -A trust --locale=C -E UTF8 >/dev/null
"$lote03_pg_bin/pg_ctl" -D "$lote03_pg_data" -l "$lote03_pg_data/log" \
  -o "-c listen_addresses='' -k $lote03_pg_socket" -w start >/dev/null
"$lote03_pg_bin/createdb" -h "$lote03_pg_socket" -U lote03 lote03db
export LOTE03_TEST_SOCKET="$lote03_pg_socket"
export DATABASE_URL="postgresql+asyncpg://lote03@/lote03db?host=$lote03_pg_socket"
cd "$lote03_backend"
"$lote03_python" -B tests/pg_integration_lote03_governed.py
