#!/bin/bash
# Inicia ContentAI Agency — backend, frontend e túnel público

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENCY_DIR="$SCRIPT_DIR/marketing-agency"

echo "=== ContentAI Agency ==="
echo ""

# Backend
echo "[1/3] Iniciando backend (porta 8001)..."
cd "$AGENCY_DIR/backend"
[ ! -d ".venv" ] && python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -q
uvicorn main:app --host 0.0.0.0 --port 8001 &
BACKEND_PID=$!

sleep 3

# Frontend
echo "[2/3] Iniciando frontend (porta 5174)..."
cd "$AGENCY_DIR/frontend"
[ ! -d "node_modules" ] && npm install -q
npm run dev &
FRONTEND_PID=$!

sleep 3

# Túnel ngrok
echo "[3/3] Iniciando túnel ngrok..."
pkill -f "ngrok http 5174" 2>/dev/null || true
sleep 1
ngrok http 5174 --log=stdout > /tmp/ngrok-contentai.log 2>&1 &
NGROK_PID=$!

sleep 4

PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    urls = [t['public_url'] for t in data.get('tunnels', []) if t['public_url'].startswith('https')]
    print(urls[0] if urls else 'Aguarde...')
except:
    print('Aguarde...')
")

LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "SEU_IP")

echo ""
echo "================================================"
echo "  LOCAL:   http://localhost:5174"
echo "  WIFI:    http://$LOCAL_IP:5174"
echo "  PÚBLICO: $PUBLIC_URL"
echo "  API:     http://localhost:8001/docs"
echo "================================================"
echo ""
echo "Pressione Ctrl+C para parar tudo"

trap "kill $BACKEND_PID $FRONTEND_PID $NGROK_PID 2>/dev/null; exit" INT TERM
wait
