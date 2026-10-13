#!/usr/bin/env bash
# Existing PostgreSQL 16 / Python 3.11 runtime, disposable existing-model schema.
# Pattern reused from run_pg_lote02_correction.sh and run_pg_research_acceptance.sh.
set -euo pipefail
event_pg_bin="/opt/homebrew/opt/postgresql@16/bin"
event_backend="$(cd "$(dirname "$0")/.." && pwd)"
event_python="$event_backend/.venv311/bin/python"
if [[ ! -x "$event_pg_bin/initdb" || ! -x "$event_python" ]]; then
  echo "LIVE_EVENT_PG_BLOCKED: PostgreSQL 16/Python 3.11 indisponível"
  exit 2
fi
event_pg_data="$(mktemp -d /tmp/cw-live-event-pg.XXXXXXXX)"
event_pg_socket="$(mktemp -d /tmp/cw-live-event-sock.XXXXXXXX)"
event_cleanup() {
  "$event_pg_bin/pg_ctl" -D "$event_pg_data" -m fast -w stop >/dev/null 2>&1 || true
  # Concrete, validated directories created by THIS runner only.
  if [[ "$event_pg_data" =~ ^/tmp/cw-live-event-pg\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$event_pg_data"
  fi
  if [[ "$event_pg_socket" =~ ^/tmp/cw-live-event-sock\.[[:alnum:]]+$ ]]; then
    rm -rf -- "$event_pg_socket"
  fi
}
trap event_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C "$event_pg_bin/initdb" \
  -D "$event_pg_data" -U event_research -A trust --locale=C -E UTF8 >/dev/null
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C "$event_pg_bin/pg_ctl" \
  -D "$event_pg_data" -l "$event_pg_data/server.log" \
  -o "-c listen_addresses='' -k $event_pg_socket" -w start >/dev/null
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C "$event_pg_bin/createdb" \
  -h "$event_pg_socket" -U event_research event_research_db
cd "$event_backend"
# No real DATABASE_URL, credentials, exchange configuration or inherited flags.
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C PYTHONHASHSEED=0 \
  CW_LIVE_EVENT_TEST_SOCKET="$event_pg_socket" \
  "$event_python" -B tests/pg_integration_live_event_study.py
