#!/bin/zsh
# Prévia LOCAL do E08B: banco temporário, provedor mock, sem chaves, notícias FICTÍCIAS.
S="/private/tmp/claude-501/-Users-alanmalta-Agente-atendimento--claude-worktrees-nifty-black-9b0c2c/792d2f48-49a6-4b04-b63f-4d5cb4ede7c4/scratchpad"
B="${S}/e08b/whatsapp-agent/backend"
T=$(mktemp -d "${S}/previa-e08b-XXXX")
export_env=(PATH=/usr/bin:/bin HOME=/tmp DATA_DIR="$T" WHATSAPP_PROVIDER=mock MASTER_KEY=previa-master
  PYTHONPATH="${B}" ADMIN_PASSWORD=previa-admin INSTANCE_MONITOR_ENABLED=0 NOTICIAS_JOB_ENABLED=1 NOTICIAS_TICK_S=5
  NOTICIAS_FIXTURES="${S}/e08b/whatsapp-agent/tests/boletim/fixtures_previa")
cd "$B"
env -i $export_env "${S}/venv311/bin/python" "/private/tmp/claude-501/-Users-alanmalta-Agente-atendimento--claude-worktrees-nifty-black-9b0c2c/792d2f48-49a6-4b04-b63f-4d5cb4ede7c4/scratchpad/e08b-checagens/seed_previa.py"
exec env -i $export_env "${S}/venv311/bin/python" -m uvicorn main:app --host 127.0.0.1 --port 8781 --log-level warning
