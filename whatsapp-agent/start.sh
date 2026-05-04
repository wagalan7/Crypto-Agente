#!/bin/bash
set -e

cd "$(dirname "$0")/backend"

if [ ! -f .env ]; then
  cp ../.env.example .env
  echo "⚠️  Arquivo .env criado. Configure antes de continuar."
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

echo "🚀 Agente iniciando em http://localhost:8001"
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
