#!/bin/bash
# Abre túnel público para acessar o Crypto AI Agent de qualquer rede

echo "=== Crypto AI Agent — Acesso Remoto ==="
echo ""

# Verifica se o frontend está rodando
if ! curl -s http://localhost:3000 > /dev/null 2>&1; then
  echo "Frontend não está rodando. Iniciando primeiro..."
  ./start.sh &
  sleep 5
fi

echo "Iniciando túnel ngrok..."
echo "(URL pública aparecerá abaixo — copie e abra no celular)"
echo ""

ngrok http 3000
