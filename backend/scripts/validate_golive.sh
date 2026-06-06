#!/usr/bin/env bash
#
# Roteiro de validação go-live (demo/testnet → mainnet).
#
# Roda as checagens READ-ONLY das travas de segurança e imprime PASS/WARN/FAIL.
# As fases que exigem mudar env no Railway ou abrir trade real ficam comentadas
# como guia — siga na ordem.
#
# Uso:
#   ./validate_golive.sh                 # usa o Railway de produção
#   BASE_URL=http://localhost:8000 ./validate_golive.sh
#   ./validate_golive.sh BTCUSDT         # símbolo p/ checar algo-orders
#
# Pré-requisito (Fase 0, no Railway → Variables):
#   EXCHANGE=binance  BINANCE_MODE=demo  EXCHANGE_SHADOW=false
#   LIVE_SIZE_MULT=0.1  KILL_MAX_DAILY_LOSS_PCT=4
#   BINANCE_API_KEY=<demo>  BINANCE_API_SECRET=<demo>
#
# (Ctrl+C pra parar.)

set -uo pipefail

BASE_URL="${BASE_URL:-https://crypto-agente-production.up.railway.app}"
SYMBOL="${1:-BTCUSDT}"

PASS=0; WARN=0; FAIL=0

# get <path> → corpo JSON em stdout (silencioso)
get() { curl -s --max-time 25 "${BASE_URL}$1" 2>/dev/null; }

# jqp <json> <python-expr usando d> → imprime resultado
jqp() {
  python3 -c '
import sys, json
try:
    d = json.loads(sys.argv[1])
except Exception as e:
    print("__ERR__", e); sys.exit(0)
print(eval(sys.argv[2]))
' "$1" "$2" 2>/dev/null
}

ok()   { echo "  ✅ PASS  $1"; PASS=$((PASS+1)); }
warn() { echo "  ⚠️  WARN  $1"; WARN=$((WARN+1)); }
bad()  { echo "  ❌ FAIL  $1"; FAIL=$((FAIL+1)); }

echo "═══════════════════════════════════════════════════════════════"
echo " Validação go-live — ${BASE_URL}"
echo "═══════════════════════════════════════════════════════════════"

# ── Fase 1: conectividade & estado ──────────────────────────────────────────
echo; echo "── Fase 1: conectividade & estado ──"

ENV_J="$(get /api/exchange/env)"
MODE="$(jqp "$ENV_J" 'd.get("mode")')"
CONF="$(jqp "$ENV_J" 'd.get("configured")')"
echo "  exchange/env: mode=${MODE} configured=${CONF}"
[[ "$CONF" == "True" ]] && ok "chaves configuradas" || bad "chaves NÃO configuradas (BINANCE_API_KEY/SECRET)"
case "$MODE" in
  demo|testnet) ok "modo ${MODE} (dinheiro fake — seguro pra testar)";;
  mainnet)      warn "modo MAINNET — dinheiro REAL! confirme que é intencional";;
  *)            warn "modo desconhecido: ${MODE}";;
esac

SH_J="$(get /api/shadow/env)"
SHADOW="$(jqp "$SH_J" 'd.get("shadow_enabled")')"
ARMED="$(jqp "$SH_J" 'd.get("live_money_armed")')"
PROD="$(jqp "$SH_J" 'd.get("is_production")')"
MULT="$(jqp "$SH_J" 'd.get("live_size_mult")')"
echo "  shadow/env: shadow_enabled=${SHADOW} is_production=${PROD} armed=${ARMED} size_mult=${MULT}"
if [[ "$SHADOW" == "True" ]]; then
  warn "shadow AINDA ligado — nenhuma ordem real sai (set EXCHANGE_SHADOW=false p/ validar live)"
else
  ok "shadow desligado — execução real ativa"
  [[ "$ARMED" == "True" ]] && ok "live_money_armed=true (travas liberam execução)" \
                           || bad "live_money_armed=false — execução bloqueada (falta LIVE_TRADING_CONFIRM em mainnet?)"
fi
[[ "$MULT" != "1.0" && "$MULT" != "None" && -n "$MULT" ]] && ok "canary ativo (×${MULT})" \
                                                          || warn "canary em ×${MULT} (1.0 = tamanho cheio)"

EQ_J="$(get /api/exchange/equity)"
EQ_OK="$(jqp "$EQ_J" 'd.get("ok")')"
EQ_SRC="$(jqp "$EQ_J" 'd.get("source")')"
EQ_TOT="$(jqp "$EQ_J" 'd.get("total_usd")')"
echo "  exchange/equity: ok=${EQ_OK} source=${EQ_SRC} total_usd=${EQ_TOT}"
if [[ "$EQ_OK" == "True" && ( "$EQ_SRC" == "live" || "$EQ_SRC" == "cache" ) ]]; then
  ok "equity REAL lido (\$${EQ_TOT}, source=${EQ_SRC})"
else
  bad "equity em fallback/erro — em live, trades serão ABORTADOS (item #5)"
fi

KS_J="$(get /api/kill-switch/status)"
KS_SRC="$(jqp "$KS_J" 'd.get("checks",{}).get("daily_loss_limit_src")')"
KS_LIM="$(jqp "$KS_J" 'd.get("checks",{}).get("daily_loss_limit_usd")')"
echo "  kill-switch: daily_loss_limit=\$${KS_LIM} (${KS_SRC})"
[[ "$KS_SRC" == *"%"* ]] && ok "limite diário escala com a banca (item #3)" \
                         || warn "limite diário em USD fixo (set KILL_MAX_DAILY_LOSS_PCT p/ escalar)"

# ── Fase 3 (read-only): posições / proteção / reconcile ─────────────────────
echo; echo "── Fase 3: posições, proteção & reconciliação ──"

POS_J="$(get /api/exchange/positions)"
POS_N="$(jqp "$POS_J" 'd.get("count", len(d.get("positions",[])))')"
echo "  exchange/positions: ${POS_N} posição(ões) viva(s)"

ALGO_J="$(get "/api/exchange/open-algo-orders?symbol=${SYMBOL}")"
ALGO_N="$(jqp "$ALGO_J" 'len(d.get("orders",[]))')"
echo "  open-algo-orders ${SYMBOL}: ${ALGO_N} ordem(ns) condicional(is)"

REC_J="$(get /api/exchange/reconcile)"
REC_ORPH="$(jqp "$REC_J" 'len(d.get("db_orphans",[]))')"
REC_UNTR="$(jqp "$REC_J" 'len(d.get("untracked",[]))')"
REC_MATCH="$(jqp "$REC_J" 'len(d.get("matched",[]))')"
echo "  reconcile: matched=${REC_MATCH} db_orphans=${REC_ORPH} untracked=${REC_UNTR}"
if [[ "$REC_ORPH" == "0" && "$REC_UNTR" == "0" ]]; then
  ok "exchange↔DB sincronizado, sem drift (item #4)"
else
  warn "DRIFT detectado — db_orphans=${REC_ORPH} untracked=${REC_UNTR} (veja /api/exchange/reconcile)"
fi

# ── Resumo ──────────────────────────────────────────────────────────────────
echo; echo "═══════════════════════════════════════════════════════════════"
echo " Resumo: ${PASS} PASS · ${WARN} WARN · ${FAIL} FAIL"
echo "═══════════════════════════════════════════════════════════════"

cat <<'GUIDE'

── Passos MANUAIS (não automatizáveis aqui) ──
 Fase 2  Ordem real end-to-end (demo):
   curl -s -X POST "$BASE_URL/api/admin/force-test-trade?symbol=BTCUSDT&side=Buy&notional_usd=50&leverage=5&close_after=true"
   (em mainnet exige header: -H "X-Admin-Token: <ADMIN_API_TOKEN>")
   → espere ok:true em open_response e close_response

 Auto-cura  apague UMA perna pela corretora e rode:
   bash backend/scripts/test_protection_autoheal.sh BTCUSDT
   → 🔴 SUMIU depois 🟢 RECRIADO

 Fase 4  kill-switch manual: set KILL_SWITCH=true no Railway → status deve dar allowed:false

── Logs do Railway a observar no boot ──
   [boot-safety] ... (🟢 shadow / 🟡 live-demo / 🔴 LIVE PRODUÇÃO / ⛔ bloqueado)
   [reconcile] ✓ sincronizado  |  ⚠ DRIFT ...
   [shadow→live] canary ... ; EXECUTED ...

── Aprovou tudo? Migra pra mainnet ──
   BINANCE_MODE=mainnet
   LIVE_TRADING_CONFIRM=ENTENDO_RISCO_DINHEIRO_REAL
   LIVE_SIZE_MULT=0.1
   ADMIN_API_TOKEN=<token forte>   # protege /api/admin/force-test-trade
GUIDE
