#!/usr/bin/env bash
set -euo pipefail
repo_backend="$(cd "$(dirname "$0")/.." && pwd)"
pg_bin="${PG16_BIN:-/opt/homebrew/opt/postgresql@16/bin}"
python_bin="${ACCEPTANCE_PYTHON:-/Users/alanmalta/Agente de IA Crypto/backend/.venv311/bin/python}"
test_cluster="$(mktemp -d /tmp/cw-acceptance-pg.XXXXXXXX)"
test_sock="$(mktemp -d /tmp/cw-acceptance-sock.XXXXXXXX)"
cleanup() {
  "$pg_bin/pg_ctl" -D "$test_cluster" -m fast -w stop >/dev/null 2>&1 || true
  case "$test_cluster" in /tmp/cw-acceptance-pg.*) rm -rf -- "$test_cluster";; esac
  case "$test_sock" in /tmp/cw-acceptance-sock.*) rm -rf -- "$test_sock";; esac
}
trap cleanup EXIT
"$pg_bin/initdb" -D "$test_cluster" --encoding=UTF8 --locale=C -U acceptance -A trust >/dev/null
"$pg_bin/pg_ctl" -D "$test_cluster" -o "-k $test_sock -c listen_addresses='' -c port=5432" -w start >/dev/null
"$pg_bin/createdb" -h "$test_sock" -U acceptance acceptance_db
cd "$repo_backend"
ACCEPTANCE_TEST_SOCKET="$test_sock" "$python_bin" -B tests/pg_integration_research_acceptance.py
