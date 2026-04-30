#!/bin/bash
# Inicia backend e frontend em paralelo

echo "=== Crypto AI Agent ==="
echo ""

# Backend
echo "[1/2] Iniciando backend (porta 8000)..."
cd backend
if [ ! -d ".venv" ]; then
  echo "Criando ambiente virtual Python..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt -q
python main.py &
BACKEND_PID=$!
cd ..

sleep 2

# Frontend
echo "[2/2] Iniciando frontend (porta 3000)..."
cd frontend
npm install -q
npm run dev &
FRONTEND_PID=$!
cd ..

LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "SEU_IP")

echo ""
echo "=========================================="
echo "  PC:     http://localhost:3000"
echo "  CELULAR: http://$LOCAL_IP:3000"
echo "  API:    http://localhost:8000/docs"
echo "  (celular e PC devem estar no mesmo WiFi)"
echo "=========================================="
echo "Pressione Ctrl+C para parar"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
