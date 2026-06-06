#!/usr/bin/env bash
#
# Teste manual da auto-cura de proteção (verificação ao vivo na corretora).
#
# Monitora as ordens condicionais (SL/TP1/TP2) vivas de um símbolo e mostra,
# em tempo real, quando uma perna SOME (você deletou) e quando VOLTA (o bot
# recriou — aparece com um algoId NOVO).
#
# Uso:
#   ./test_protection_autoheal.sh BTCUSDT
#   ./test_protection_autoheal.sh BTCUSDT 5 40     # poll 5s, máx 40 ciclos
#   BASE_URL=http://localhost:8000 ./test_protection_autoheal.sh BTCUSDT
#
# Roteiro: deixe rodando, apague UMA perna pela corretora e observe o diff.
# (Ctrl+C pra parar a qualquer momento.)

set -euo pipefail

SYMBOL="${1:-}"
POLL="${2:-5}"          # segundos entre consultas
MAX_TICKS="${3:-60}"    # nº máximo de consultas (60 x 5s = 5 min)
BASE_URL="${BASE_URL:-https://crypto-agente-production.up.railway.app}"

if [[ -z "$SYMBOL" ]]; then
  echo "uso: $0 <SYMBOL> [poll_seg] [max_ciclos]"
  echo "ex.: $0 BTCUSDT"
  exit 1
fi

URL="${BASE_URL}/api/exchange/open-algo-orders?symbol=${SYMBOL}"

# Extrai 'algo_id|type|trigger_price|status' (uma linha por ordem) via python3.
snapshot() {
  curl -s --max-time 20 "$URL" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("__ERRO_JSON__"); sys.exit(0)
if not d.get("ok"):
    print("__ERRO_API__:" + str(d.get("error") or d.get("msg") or "?")); sys.exit(0)
for o in d.get("orders", []):
    print("|".join([
        str(o.get("algo_id")),
        str(o.get("type")),
        str(o.get("trigger_price")),
        str(o.get("status")),
    ]))
'
}

echo "═══════════════════════════════════════════════════════════════"
echo " Monitorando proteção de ${SYMBOL}"
echo " ${URL}"
echo " poll=${POLL}s  máx=${MAX_TICKS} ciclos"
echo " → apague UMA perna pela corretora e observe SUMIR e VOLTAR"
echo "═══════════════════════════════════════════════════════════════"

PREV=""
for ((i=1; i<=MAX_TICKS; i++)); do
  CUR="$(snapshot || true)"
  TS="$(date +%H:%M:%S)"

  if [[ "$CUR" == __ERRO* ]]; then
    echo "[$TS] (ciclo $i) erro lendo API: ${CUR}"
    sleep "$POLL"; continue
  fi

  # IDs atuais e anteriores (1ª coluna)
  CUR_IDS="$(echo "$CUR"  | awk -F'|' 'NF{print $1}' | sort -u)"
  PREV_IDS="$(echo "$PREV" | awk -F'|' 'NF{print $1}' | sort -u)"

  if [[ "$i" -eq 1 ]]; then
    N="$(echo "$CUR" | grep -c . || true)"
    echo "[$TS] estado inicial — ${N} ordem(ns) viva(s):"
    echo "$CUR" | awk -F'|' 'NF{printf "        %-12s %-20s trigger=%-12s %s\n",$1,$2,$3,$4}'
  else
    # Sumiram (estavam antes, não estão agora) = você deletou / disparou
    GONE="$(comm -23 <(echo "$PREV_IDS") <(echo "$CUR_IDS") || true)"
    # Apareceram (não estavam antes) = bot recriou
    NEW="$(comm -13 <(echo "$PREV_IDS") <(echo "$CUR_IDS") || true)"

    if [[ -n "${GONE//[$' \t\n']/}" ]]; then
      for g in $GONE; do
        DESC="$(echo "$PREV" | awk -F'|' -v id="$g" '$1==id{print $2" trigger="$3}')"
        echo "[$TS] 🔴 SUMIU   algoId=$g  ($DESC)"
      done
    fi
    if [[ -n "${NEW//[$' \t\n']/}" ]]; then
      for n in $NEW; do
        DESC="$(echo "$CUR" | awk -F'|' -v id="$n" '$1==id{print $2" trigger="$3}')"
        echo "[$TS] 🟢 RECRIADO algoId=$n  ($DESC)  ← auto-cura"
      done
    fi
    if [[ -z "${GONE//[$' \t\n']/}" && -z "${NEW//[$' \t\n']/}" ]]; then
      N="$(echo "$CUR" | grep -c . || true)"
      echo "[$TS] (ciclo $i) sem mudança — ${N} ordem(ns) viva(s)"
    fi
  fi

  PREV="$CUR"
  sleep "$POLL"
done

echo "═══════════════════════════════════════════════════════════════"
echo " Fim do monitoramento (${MAX_TICKS} ciclos)."
