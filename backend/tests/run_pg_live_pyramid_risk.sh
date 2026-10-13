#!/usr/bin/env bash
# Mesmo padrão do runner de eventos: PG16 descartável, somente socket Unix.
set -euo pipefail
pyramid_pg_bin="/opt/homebrew/opt/postgresql@16/bin"
pyramid_backend="$(cd "$(dirname "$0")/.." && pwd)"
pyramid_python="$pyramid_backend/.venv311/bin/python"
if [[ ! -x "$pyramid_pg_bin/initdb" || ! -x "$pyramid_python" ]]; then
  echo "PYRAMID_RISK_PG_BLOCKED: runtime local indisponível"
  exit 2
fi
pyramid_data="$(mktemp -d /tmp/cw-pyr-risk-pg.XXXXXXXX)"
pyramid_socket="$(mktemp -d /tmp/cw-pyr-risk-sock.XXXXXXXX)"
pyramid_cleanup() {
  "$pyramid_pg_bin/pg_ctl" -D "$pyramid_data" -m fast -w stop >/dev/null 2>&1 || true
  if [[ "$pyramid_data" =~ ^/tmp/cw-pyr-risk-pg\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$pyramid_data"
  fi
  if [[ "$pyramid_socket" =~ ^/tmp/cw-pyr-risk-sock\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$pyramid_socket"
  fi
}
trap pyramid_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C "$pyramid_pg_bin/initdb" \
  -D "$pyramid_data" -U pyr_risk -A trust --locale=C -E UTF8 >/dev/null
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C "$pyramid_pg_bin/pg_ctl" \
  -D "$pyramid_data" -l "$pyramid_data/server.log" \
  -o "-c listen_addresses='' -k $pyramid_socket" -w start >/dev/null
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C "$pyramid_pg_bin/createdb" \
  -h "$pyramid_socket" -U pyr_risk pyr_risk_db
cd "$pyramid_backend"
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C PYTHONHASHSEED=0 PYTHONPATH="$pyramid_backend" \
  CW_PYRAMID_RISK_TEST_SOCKET="$pyramid_socket" \
  "$pyramid_python" -B tests/test_live_pyramid_retained_risk.py
