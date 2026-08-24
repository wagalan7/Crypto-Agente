"""
Binance Futures (USDT-M) — cliente ASSINADO (HMAC-SHA256) — #11.

Interface 100% compatível com bybit_signed_service.py (mesmas funções,
mesmo shape de retorno) — o resto do app não precisa saber qual exchange
está em uso. Selecione via env var EXCHANGE=binance|bybit.

Auth (Binance):
  - Sign = HMAC-SHA256(secret, querystring) onde querystring inclui timestamp
  - Anexa "&signature=<hex>" no final da URL (GET/POST/DELETE)
  - Header X-MBX-APIKEY: <key>
  - GET/POST/DELETE — todos signed seguem o mesmo padrão

Refs: https://binance-docs.github.io/apidocs/futures/en/

Env:
  BINANCE_API_KEY        — API key
  BINANCE_API_SECRET     — secret
  BINANCE_TESTNET        — "true" (default) → testnet.binancefuture.com
  BINANCE_RECV_WINDOW    — janela em ms (default 5000)

Nota regulatória: o acesso a futures mainnet pelo BR depende da conta/KYC.
Contas com o módulo de futuros liberado operam normalmente. Antes de ligar
dinheiro real, confirme: BINANCE_MODE=mainnet + EXCHANGE_SHADOW=false +
LIVE_TRADING_CONFIRM=ENTENDO_RISCO_DINHEIRO_REAL (trava de segurança no
shadow_trade_service). Comece com LIVE_SIZE_MULT pequeno (ex: 0.1).
"""
from __future__ import annotations
import asyncio
import hmac
import hashlib
import os
import time
import logging
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

# ── Retry de ordens condicionais (algo orders) ──────────────────────────────
# As 3 ordens de proteção (SL/TP1/TP2) são emitidas em sequência. Falhas
# transitórias (rate-limit, timeout, indisponibilidade momentânea) deixavam a
# posição parcialmente desprotegida. Retry com backoff cobre esse buraco.
_ALGO_MAX_ATTEMPTS = int(os.getenv("BINANCE_ALGO_MAX_ATTEMPTS", "3"))
_ALGO_RETRY_BASE_DELAY = float(os.getenv("BINANCE_ALGO_RETRY_DELAY", "0.4"))  # s, escala com a tentativa

# Fechamento fail-safe quando a entrada foi aceita mas o SL não pôde ser
# confirmado. Curto e limitado: a posição sem stop é uma emergência, não deve
# esperar o grace normal do auto-heal.
_EMERGENCY_CLOSE_ATTEMPTS = max(1, int(os.getenv("BINANCE_EMERGENCY_CLOSE_ATTEMPTS", "3")))
_EMERGENCY_CLOSE_RETRY_DELAY = max(0.0, float(os.getenv("BINANCE_EMERGENCY_CLOSE_RETRY_DELAY", "0.35")))
_POST_FILL_PROTECTION_TIMEOUT_S = max(
    0.5, float(os.getenv("BINANCE_POST_FILL_PROTECTION_TIMEOUT_S", "8"))
)

# O restante de uma entrada maker precisa estar comprovadamente terminal antes
# de proteger/rollbackar a parte preenchida ou cair para MARKET. Caso contrário,
# a LIMIT ainda pode preencher depois e recriar exposição sem stop.
_MAKER_CANCEL_CONFIRM_ATTEMPTS = max(
    1, int(os.getenv("BINANCE_MAKER_CANCEL_CONFIRM_ATTEMPTS", "2"))
)
_MAKER_CANCEL_CONFIRM_DELAY = max(
    0.0, float(os.getenv("BINANCE_MAKER_CANCEL_CONFIRM_DELAY", "0.25"))
)

# ── #1 Runner com trailing pós-TP2 (default OFF) ────────────────────────────
# Ideia: em vez de o TP2 fechar 100% do restante (closePosition=true), deixar
# uma FRAÇÃO da posição correndo ("runner") após o TP2, gerida por um stop
# chandelier (ATR trailing) no trade_manager. Assim capturamos a cauda de
# tendências fortes que hoje o TP2 fixo corta cedo — sem risco extra, porque o
# runner só é armado DEPOIS do TP1 (parcial embolsada) e do TP2 (parcial 2
# embolsada), já em lucro estrutural, com SL no BE ou acima.
#
# Implementação de superfície mínima: a divisão do TP2 acontece AQUI dentro,
# SÓ no bracket de abertura (quando `tp1` E `tp2` vêm juntos = has_partial).
# Chamadas de auto-cura / transição de BE passam tp1=None ou tp2=None e NÃO
# disparam o split. Com RUNNER_ENABLED=false o TP2 volta a ser closePosition
# (comportamento idêntico ao de hoje — zero mudança em produção).
RUNNER_ENABLED = os.getenv("RUNNER_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# Fração da posição TOTAL (qty na abertura) que vira runner. 0.20 = 20%.
RUNNER_QTY_PCT = float(os.getenv("RUNNER_QTY_PCT", "0.20"))

# Códigos de erro Binance que NÃO adianta repetir (problema é o pedido, não o canal).
_PERMANENT_ALGO_CODES = {
    -2021,  # Order would immediately trigger (preço do gatilho do lado errado do mercado)
    -2022,  # ReduceOnly Order is rejected
    -1111,  # Precision is over the maximum defined for this asset
    -1102,  # Mandatory parameter not sent / empty / malformed
    -1106,  # Parameter not required
    -1130,  # Invalid data sent for a parameter
    -4003,  # Quantity less than zero
    -4014,  # Price not increased by tick size
    -4131,  # Counterparty best price exceeds permissible range
}
_PERMANENT_ALGO_SUBSTRINGS = (
    "would immediately trigger",
    "reduceonly",
    "precision",
    "tick size",
    "min notional",
    "notional must be no smaller",
)

# Nesses retornos a própria Binance avisa (ou o transporte implica) que o
# status de execução pode ser desconhecido. Uma segunda entrada não é segura
# até reconciliar pelo clientOrderId.
_AMBIGUOUS_ORDER_CODES = {-1000, -1001, -1003, -1006, -1007, -1008, -4116}


def _is_permanent_algo_error(code, msg: str | None) -> bool:
    """True se o erro é do próprio pedido (sem chance de sucesso ao repetir)."""
    try:
        if code is not None and int(code) in _PERMANENT_ALGO_CODES:
            return True
    except (TypeError, ValueError):
        pass
    if msg:
        low = msg.lower()
        if any(s in low for s in _PERMANENT_ALGO_SUBSTRINGS):
            return True
    return False


def _is_ambiguous_order_submission(result: dict) -> bool:
    # Falhas locais antes de qualquer request (credencial ausente/cooldown) não
    # podem ter criado ordem e não devem armar quarentena falsa.
    if result.get("_preflight") or result.get("_cooldown"):
        return False
    try:
        code = int(result.get("code"))
    except (TypeError, ValueError):
        code = None
    msg = str(result.get("msg") or result.get("error") or "").lower()
    return bool(
        code in _AMBIGUOUS_ORDER_CODES
        or (code is not None and code >= 500)
        or code in {408, 418, 429}
        or (code is None and result.get("_request_sent"))
        or any(token in msg for token in (
            "timeout", "timed out", "execution status unknown",
            "connection", "network", "disconnected", "non-json",
            "duplicate order", "duplicate clientorderid",
        ))
        or ("duplicat" in msg and "clientorder" in msg)
    )

_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
# BINANCE_MODE: "demo" (default — demo-fapi.binance.com, conta principal Binance),
#               "testnet" (testnet.binancefuture.com — sistema legado, GitHub login),
#               "mainnet" (fapi.binance.com — produção real)
# Backward-compat: se BINANCE_MODE não setado, usa BINANCE_TESTNET (true=demo, false=mainnet).
_MODE = os.getenv("BINANCE_MODE", "").strip().lower()
if not _MODE:
    _TESTNET_LEGACY = os.getenv("BINANCE_TESTNET", "true").strip().lower() in ("1", "true", "yes")
    _MODE = "demo" if _TESTNET_LEGACY else "mainnet"

_BASE_BY_MODE = {
    "demo":    "https://demo-fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "mainnet": "https://fapi.binance.com",
}
BASE = _BASE_BY_MODE.get(_MODE, "https://demo-fapi.binance.com")
_TESTNET = _MODE in ("demo", "testnet")  # mantém flag pra compat com env_info
_RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))

# Proxy de IP fixo (opcional): rotear as chamadas ASSINADAS por um proxy com IP
# estável, pra a whitelist da Binance não quebrar quando o egress IP do host
# mudar. Vazio = sem proxy (comportamento idêntico ao de sempre). Só as chamadas
# autenticadas passam por aqui; dados públicos não precisam (funcionam de qq IP).
# Formato: "http://user:pass@host:porta" (http/https/socks5).
_PROXY_URL = os.getenv("BINANCE_PROXY_URL", "").strip() or None

_http_client: Optional[httpx.AsyncClient] = None


def is_configured() -> bool:
    return bool(_API_KEY and _API_SECRET)


def _proxy_masked() -> Optional[str]:
    """Host:porta do proxy sem credenciais (pra logar/expor sem vazar segredo)."""
    if not _PROXY_URL:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(_PROXY_URL)
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        return f"{p.scheme}://{netloc}" if netloc else "set"
    except Exception:  # noqa: BLE001
        return "set"


def env_info() -> dict:
    return {
        "configured": is_configured(),
        "mode": _MODE,
        "testnet": _TESTNET,
        "base_url": BASE,
        "key_prefix": _API_KEY[:4] + "..." if _API_KEY else None,
        "recv_window_ms": _RECV_WINDOW,
        "proxy_enabled": bool(_PROXY_URL),
        "proxy": _proxy_masked(),
    }


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        kwargs: dict = {"timeout": 15.0, "headers": {"X-MBX-APIKEY": _API_KEY}}
        if _PROXY_URL:
            kwargs["proxy"] = _PROXY_URL
        _http_client = httpx.AsyncClient(**kwargs)
    return _http_client


def _sign(qs: str) -> str:
    return hmac.new(_API_SECRET.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()


def _fire_telegram(msg: str, event_type: str) -> None:
    """Dispara um alerta no Telegram SEM bloquear o caller (fire-and-forget).
    Import preguiçoso + create_task — evita ciclo de import e nunca propaga erro
    de notificação pro caminho de trading."""
    try:
        from services.notification_service import send_telegram
        loop = asyncio.get_running_loop()
        loop.create_task(send_telegram(msg, event_type=event_type))
    except Exception as e:  # noqa: BLE001 — alerta é best-effort
        log.warning(f"[binance] alerta telegram '{event_type}' falhou: {e}")


def _arm_ban(ban_ms: float, origin: str) -> None:
    """Arma o cooldown de ban e ALERTA o usuário 1x por janela. Antes, o bot
    ficava cego (parava de ler posições/ordens → sem detecção de fechamento e
    sem autocura) SEM avisar. Agora manda um Telegram explícito."""
    global _ban_until_ms, _ban_alert_sent_for_ms, _ban_recovery_pending
    _ban_until_ms = ban_ms
    _ban_recovery_pending = True
    if _ban_alert_sent_for_ms != ban_ms:  # 1x por janela de ban
        _ban_alert_sent_for_ms = ban_ms
        try:
            import datetime as _dt
            _BRT = _dt.timezone(_dt.timedelta(hours=-3))  # America/Sao_Paulo (sem DST)
            until = _dt.datetime.fromtimestamp(ban_ms / 1000.0, tz=_BRT)
            mins = max(0.0, (ban_ms - time.time() * 1000.0) / 60000.0)
            _fire_telegram(
                f"\u26A0\uFE0F *Bot cego \u2014 rate-limit Binance*\n"
                f"used-weight estourou o teto (\u2248{_used_weight_1m}/min). A Binance "
                f"baniu o IP via `{origin}` at\u00E9 *{until.strftime('%H:%M')} BRT* "
                f"(~{mins:.0f}min).\n"
                f"\U0001F6D1 *Monitoramento e autocura PAUSADOS* \u2014 sem leitura de "
                f"posi\u00E7\u00F5es/ordens. Fechamentos e pernas faltantes ser\u00E3o "
                f"reconciliados quando voltar. *Cheque o SL manualmente se preciso.*",
                event_type="rate_limit",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"[binance] alerta de ban falhou: {e}")


def _clear_ban_if_recovered() -> None:
    """Chamado após uma chamada assinada bem-sucedida. Se havia um ban ativo,
    avisa que o monitoramento voltou."""
    global _ban_recovery_pending
    if _ban_recovery_pending:
        _ban_recovery_pending = False
        _fire_telegram(
            "\u2705 *Rate-limit normalizado* \u2014 leitura da Binance retomada. "
            "Monitoramento e autocura voltaram ao ar; fechamentos/pernas pendentes "
            "ser\u00E3o reconciliados nos pr\u00F3ximos ciclos.",
            event_type="rate_limit",
        )


def _build_signed_url(path: str, params: Optional[dict] = None) -> str:
    """Monta querystring + timestamp + signature. Funciona pra GET/POST/DELETE."""
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = _RECV_WINDOW
    # Remove None
    p = {k: v for k, v in p.items() if v is not None}
    qs = urlencode(p)
    sig = _sign(qs)
    return f"{BASE}{path}?{qs}&signature={sig}"


async def _signed_request(method: str, path: str, params: Optional[dict] = None) -> dict:
    global _ban_until_ms, _throttle_until_ms, _used_weight_1m
    if not is_configured():
        return {
            "ok": False,
            "error": "BINANCE_API_KEY/SECRET não configurados",
            "_preflight": True,
        }

    now_ms = time.time() * 1000.0
    # ── Proteção 1: cooldown de ban (-1003). Vale pra TODOS os endpoints
    #    assinados. Durante o ban NÃO chama a API (senão a Binance escala a
    #    duração). Callers que têm cache (ex.: get_positions) servem o stale.
    if now_ms < _ban_until_ms:
        return {"ok": False, "code": -1003, "_cooldown": True,
                "msg": f"rate-limit cooldown local ativo até {int(_ban_until_ms)}",
                "ban_until_ms": _ban_until_ms}
    # ── Proteção 2: throttle proativo. Se o peso usado recente passou do teto
    #    macio/duro, espaça/pausa as chamadas pra não chegar no hard limit
    #    (2400/min) que dispara o -1003. Teto = _MAX_THROTTLE_SLEEP_S (cobre a
    #    pausa dura de 15s; antes era fixo 2s e não segurava o burst).
    if now_ms < _throttle_until_ms:
        await asyncio.sleep(min(_MAX_THROTTLE_SLEEP_S, max(0.0, (_throttle_until_ms - now_ms) / 1000.0)))

    url = _build_signed_url(path, params)
    try:
        r = await _get_client().request(method, url)
        # Lê o peso consumido (header da Binance) → throttle proativo.
        try:
            uw = r.headers.get("x-mbx-used-weight-1m") or r.headers.get("X-MBX-USED-WEIGHT-1M")
            if uw is not None:
                _used_weight_1m = int(uw)
                if _used_weight_1m >= _WEIGHT_HARD_LIMIT:
                    # Perto do teto duro (2400) → PAUSA dura pra a janela drenar.
                    _throttle_until_ms = time.time() * 1000.0 + _HARD_PAUSE_MS
                    log.warning(
                        f"[binance] used-weight-1m={_used_weight_1m} >= HARD {_WEIGHT_HARD_LIMIT} "
                        f"— PAUSA dura {_HARD_PAUSE_MS}ms (evita -1003/ban)"
                    )
                elif _used_weight_1m >= _WEIGHT_SOFT_LIMIT:
                    _throttle_until_ms = time.time() * 1000.0 + _THROTTLE_MS
                    log.warning(
                        f"[binance] used-weight-1m={_used_weight_1m} >= soft {_WEIGHT_SOFT_LIMIT} "
                        f"— throttle proativo {_THROTTLE_MS}ms"
                    )
        except Exception:
            pass
        try:
            data = r.json()
        except Exception:
            return {
                "ok": False,
                "code": r.status_code,
                "error": f"resposta não-JSON ({r.status_code}): {r.text[:200]}",
                "_request_sent": True,
            }
        # `code` pode vir int (Futures clássico: -2011) OU string (endpoints
        # algoOrder: "000000"=sucesso). Coage com segurança — só erro se < 0.
        _raw_code = data.get("code") if isinstance(data, dict) else None
        _code_int: Optional[int] = None
        if _raw_code is not None:
            try:
                _code_int = int(_raw_code)
            except (ValueError, TypeError):
                _code_int = None
        if r.status_code >= 400 or (_code_int is not None and _code_int < 0):
            log.warning(f"[binance] {method} {path} status={r.status_code} resp={data}")
            err = {"ok": False, "code": _raw_code if _raw_code is not None else r.status_code,
                   "msg": data.get("msg") if isinstance(data, dict) else r.text, "raw": data,
                   "_request_sent": True}
            # ── Detecção CENTRALIZADA de ban (-1003 / HTTP 418/429). Arma o
            #    cooldown global a partir de QUALQUER endpoint assinado.
            ban_ms = _parse_ban_until_ms(err)
            if ban_ms <= 0 and r.status_code in (418, 429):
                ban_ms = now_ms + _RATE_LIMIT_COOLDOWN_MS
            if ban_ms > 0:
                _arm_ban(ban_ms, path)  # arma cooldown + ALERTA Telegram "bot cego"
                err["ban_until_ms"] = ban_ms
                log.warning(
                    f"[binance] rate-limit/ban via {path} (status={r.status_code} code={_raw_code}) "
                    f"— cooldown local até {int(ban_ms)}. Parando de chamar pra não escalar."
                )
            return err
        _clear_ban_if_recovered()  # 1ª chamada OK pós-ban → avisa que voltou
        return {"ok": True, "result": data, "raw": data}
    except Exception as e:
        log.exception(f"[binance] {method} {path} falhou")
        return {"ok": False, "error": str(e), "_request_sent": True}


# ─── Symbol helpers ────────────────────────────────────────────────────────────


def to_binance(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTCUSDT' (mesma convenção Bybit)."""
    return symbol.split(":")[0].replace("/", "")


# ─── Precision (stepSize/tickSize) cache ──────────────────────────────────────
# Binance Futures rejeita ordens com qty/preço fora do stepSize/tickSize do
# símbolo (erro "Precision is over the maximum"). Buscamos exchangeInfo 1x
# e cacheamos os filtros por símbolo — depois truncamos qty/SL/TP antes do
# submit. ExchangeInfo é público; usa o mesmo BASE.

_filters_cache: dict = {}  # sym → {"step": float, "tick": float, "min_qty": float}
_filters_lock = None  # lazy: criado no primeiro uso pra herdar o loop ativo


async def _load_exchange_info() -> dict:
    """Pega /fapi/v1/exchangeInfo (público, sem assinar) e popula o cache."""
    try:
        r = await _get_client().get(f"{BASE}/fapi/v1/exchangeInfo")
        data = r.json()
        for s in (data.get("symbols") or []):
            sym = s.get("symbol")
            if not sym:
                continue
            step = 0.0
            tick = 0.0
            min_qty = 0.0
            for f in (s.get("filters") or []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize") or 0)
                    min_qty = float(f.get("minQty") or 0)
                elif f.get("filterType") == "PRICE_FILTER":
                    tick = float(f.get("tickSize") or 0)
            _filters_cache[sym] = {"step": step, "tick": tick, "min_qty": min_qty}
        log.info(f"[binance] exchangeInfo carregado: {len(_filters_cache)} símbolos")
    except Exception as e:
        log.warning(f"[binance] exchangeInfo falhou (segue sem precisão): {e}")
    return _filters_cache


async def _get_symbol_filters(sym: str) -> dict:
    if sym in _filters_cache:
        return _filters_cache[sym]
    import asyncio as _aio
    global _filters_lock
    if _filters_lock is None:
        _filters_lock = _aio.Lock()
    async with _filters_lock:
        if sym in _filters_cache:
            return _filters_cache[sym]
        if not _filters_cache:
            await _load_exchange_info()
    return _filters_cache.get(sym, {"step": 0.0, "tick": 0.0, "min_qty": 0.0})


def _floor_to_step(value: float, step: float) -> float:
    """Trunca (não arredonda) pro múltiplo de step mais próximo abaixo.
    Ex: floor(61651.676, 1) = 61651; floor(0.123456, 0.001) = 0.123.
    Usa string formatting pra evitar drift de float."""
    if step <= 0:
        return value
    try:
        value_d = Decimal(str(value))
        step_d = Decimal(str(step))
        units = (value_d / step_d).to_integral_value(rounding=ROUND_FLOOR)
        return float(units * step_d)
    except (InvalidOperation, ValueError):
        return value


async def _round_qty(sym: str, qty: float) -> float:
    f = await _get_symbol_filters(sym)
    step = f.get("step", 0.0)
    if step <= 0:
        return qty
    return _floor_to_step(qty, step)


async def _round_price(sym: str, price: float) -> float:
    f = await _get_symbol_filters(sym)
    tick = f.get("tick", 0.0)
    if tick <= 0:
        return price
    return _floor_to_step(price, tick)


# ─── High-level endpoints (mesma interface do bybit_signed_service) ───────────


async def get_wallet_balance(account_type: str = "UNIFIED") -> dict:
    """
    Saldo Futures USDT-M. Binance não tem o conceito 'UNIFIED' como Bybit —
    parâmetro é aceito por compat mas ignorado. Retorna mesmo shape.
    """
    _ = account_type
    now = time.time()
    # Cache curto: saldo/conta também consome peso assinado e era chamado sem
    # throttle. Durante cooldown de ban, serve o último saldo conhecido (stale).
    if (_account_cache["data"] is not None
            and (now - _account_cache["ts"]) < _ACCOUNT_CACHE_TTL):
        return dict(_account_cache["data"])
    res = await _signed_request("GET", "/fapi/v2/account")
    if not res.get("ok"):
        if res.get("_cooldown") and _account_cache["data"] is not None:
            stale = dict(_account_cache["data"]); stale["stale"] = True
            return stale
        return res
    acc = res["result"] or {}
    out = {
        "ok": True,
        "equity_usd": float(acc.get("totalMarginBalance") or 0),
        "available_usd": float(acc.get("availableBalance") or 0),
        "wallet_balance_usd": float(acc.get("totalWalletBalance") or 0),
        "margin_used_usd": float(acc.get("totalInitialMargin") or 0),
        "coins": [
            {
                "coin": a.get("asset"),
                "balance": float(a.get("walletBalance") or 0),
                "equity": float(a.get("marginBalance") or 0),
                "usd_value": float(a.get("walletBalance") or 0)
                if a.get("asset") in ("USDT", "BUSD", "USDC") else None,
            }
            for a in (acc.get("assets") or [])
            if float(a.get("walletBalance") or 0) > 0
        ],
        "testnet": _TESTNET,
        "exchange": "binance",
    }
    _account_cache["data"] = out
    _account_cache["ts"] = now
    return dict(out)


# ── Cache + anti-ban da leitura de posições (positionRisk) ──────────────────
# positionRisk não tem cache nem backoff: cada poll do painel + trade-manager +
# reconcile batia CRU na API assinada. Sob carga, a Binance devolve -1003 e
# BANE o IP por minutos→horas; pior, sem detectar o ban o código continuava
# chamando e a Binance ESCALAVA a duração. Solução:
#   1. Snapshot único de TODAS as posições, cacheado por TTL curto. Toda chamada
#      (com ou sem symbol) compartilha o mesmo fetch e filtra em memória —
#      derruba N chamadas/ciclo pra no máx 1 a cada TTL.
#   2. Detecção de -1003/"banned until <ms>" → cooldown: enquanto banido,
#      devolve o último snapshot conhecido SEM chamar a API (não escala o ban).
_POSITIONS_CACHE_TTL = float(os.getenv("BINANCE_POSITIONS_CACHE_TTL", "5.0"))  # s
_ACCOUNT_CACHE_TTL = float(os.getenv("BINANCE_ACCOUNT_CACHE_TTL", "5.0"))  # s (saldo/conta)
_RATE_LIMIT_COOLDOWN_MS = int(os.getenv("BINANCE_RATELIMIT_COOLDOWN_MS", "30000"))  # -1003 sem 'banned until'
_positions_cache: dict = {"ts": 0.0, "data": None}  # data = lista normalizada (size>0)
_account_cache: dict = {"ts": 0.0, "data": None}    # data = dict de saldo já formatado
_ban_until_ms: float = 0.0
# ── Throttle proativo por peso (Binance fapi: hard limit ~2400/min por IP) ──
# Postmortem 26/06: used-weight chegou a 2905 (> 2400) → -1003 / IP-ban de ~40min.
# O espaçamento macio de 2s não segurou um burst. Defesa em 2 estágios:
#   soft  (1500): começa a espaçar as chamadas (2s entre elas)
#   hard  (2200): PAUSA dura — segura ~15s pra a janela rolante de 1min drenar
#                 ANTES de chegar nos 2400 que disparam o ban. Melhor atrasar uma
#                 ordem 15s do que ficar 40min cego.
_WEIGHT_SOFT_LIMIT = int(os.getenv("BINANCE_WEIGHT_SOFT_LIMIT", "1500"))  # ao passar, espaça chamadas
_WEIGHT_HARD_LIMIT = int(os.getenv("BINANCE_WEIGHT_HARD_LIMIT", "2200"))  # ao passar, PAUSA dura
_THROTTLE_MS = int(os.getenv("BINANCE_THROTTLE_MS", "2000"))               # janela de espaçamento (soft)
_HARD_PAUSE_MS = int(os.getenv("BINANCE_HARD_PAUSE_MS", "15000"))          # pausa dura (hard)
_MAX_THROTTLE_SLEEP_S = _HARD_PAUSE_MS / 1000.0                            # teto do sleep proativo
_throttle_until_ms: float = 0.0
_used_weight_1m: int = 0
# Alerta Telegram "bot cego": dispara 1x por janela de ban (não a cada chamada
# bloqueada) + alerta de recuperação quando a 1ª chamada assinada volta a passar.
_ban_alert_sent_for_ms: float = 0.0
_ban_recovery_pending: bool = False


def _parse_ban_until_ms(res: dict) -> float:
    """Extrai o epoch-ms do ban de uma resposta -1003. 0 se não houver ban."""
    try:
        code = res.get("code")
        try:
            code = int(code)
        except (ValueError, TypeError):
            code = None
        msg = str(res.get("msg") or res.get("error") or "")
        if code == -1003 or "too many request" in msg.lower() or "banned" in msg.lower():
            import re
            m = re.search(r"banned until (\d+)", msg)
            if m:
                return float(m.group(1))
            # -1003 sem ban explícito (só rate-limit) → cooldown curto preventivo
            return (time.time() * 1000.0) + _RATE_LIMIT_COOLDOWN_MS
    except Exception:
        pass
    return 0.0


def get_positions_ban_status() -> dict:
    """Diagnóstico: estado do cooldown anti-ban + throttle por peso."""
    now_ms = time.time() * 1000.0
    return {
        "banned": now_ms < _ban_until_ms,
        "ban_until_ms": _ban_until_ms,
        "seconds_left": max(0.0, round((_ban_until_ms - now_ms) / 1000.0, 1)),
        "cache_age_s": round(time.time() - _positions_cache["ts"], 1) if _positions_cache["data"] is not None else None,
        "cache_ttl_s": _POSITIONS_CACHE_TTL,
        "used_weight_1m": _used_weight_1m,
        "weight_soft_limit": _WEIGHT_SOFT_LIMIT,
        "throttling": now_ms < _throttle_until_ms,
        "throttle_until_ms": _throttle_until_ms,
    }


# ─── Rate-gate COMPARTILHADO (p/ o caminho de dados PÚBLICOS no mesmo IP) ──────
# O scan busca klines/ticker via binance_futures_service pelo MESMO proxy/IP das
# chamadas assinadas → soma no mesmo x-mbx-used-weight-1m. Antes, esse caminho
# público era 100% invisível ao freio: gerava a maior parte do peso E continuava
# batendo no IP DURANTE o ban → a Binance escalava o ban (loop de horas). Estes
# helpers deixam o caminho público: (a) RESPEITAR o ban (parar de chamar), (b)
# compartilhar o MESMO freio de peso, (c) ARMAR o ban num 418/429.

def is_banned() -> bool:
    """True se o IP está em cooldown de ban — não deve bater na Binance."""
    return (time.time() * 1000.0) < _ban_until_ms


async def await_rate_gate() -> bool:
    """Chamar ANTES de uma request pública pelo IP compartilhado.
    Retorna False se estamos banidos (caller PULA a chamada → não escala o ban).
    Se só throttling, dorme o necessário e retorna True."""
    now_ms = time.time() * 1000.0
    if now_ms < _ban_until_ms:
        return False
    if now_ms < _throttle_until_ms:
        await asyncio.sleep(min(_MAX_THROTTLE_SLEEP_S,
                                max(0.0, (_throttle_until_ms - now_ms) / 1000.0)))
    return True


def record_external_weight(used_weight_1m) -> None:
    """Registra o x-mbx-used-weight-1m de uma resposta PÚBLICA (klines/ticker) e
    arma soft/hard throttle igual ao caminho assinado — o peso público deixa de
    ser invisível ao controle."""
    global _used_weight_1m, _throttle_until_ms
    if used_weight_1m is None:
        return
    try:
        _used_weight_1m = int(used_weight_1m)
    except (TypeError, ValueError):
        return
    now_ms = time.time() * 1000.0
    if _used_weight_1m >= _WEIGHT_HARD_LIMIT:
        _throttle_until_ms = now_ms + _HARD_PAUSE_MS
        log.warning(f"[binance] (público) used-weight-1m={_used_weight_1m} >= HARD "
                    f"{_WEIGHT_HARD_LIMIT} — PAUSA dura {_HARD_PAUSE_MS}ms")
    elif _used_weight_1m >= _WEIGHT_SOFT_LIMIT:
        _throttle_until_ms = now_ms + _THROTTLE_MS


def arm_ban_external(status_code: int, retry_after_s=None, origin: str = "público") -> None:
    """O caminho público recebeu 418/429 → arma o MESMO cooldown de ban (+ alerta
    Telegram), pra todo o app parar de bater no IP."""
    now_ms = time.time() * 1000.0
    ban_ms = 0.0
    try:
        if retry_after_s and float(retry_after_s) > 0:
            ban_ms = now_ms + float(retry_after_s) * 1000.0
    except (TypeError, ValueError):
        ban_ms = 0.0
    if ban_ms <= 0 and status_code in (418, 429):
        ban_ms = now_ms + _RATE_LIMIT_COOLDOWN_MS
    if ban_ms > 0:
        _arm_ban(ban_ms, origin)


def _filter_positions(data, symbol: Optional[str]):
    if not symbol:
        return list(data)
    norm = to_binance(symbol) if "/" in symbol else symbol
    return [p for p in data if p.get("symbol") == norm]


async def get_positions(symbol: Optional[str] = None, *, force: bool = False) -> dict:
    """Leitura de posições com cache curto + cooldown anti-ban.

    Sempre busca TODAS as posições (mesmo weight que uma só no positionRisk) e
    filtra por symbol em memória, pra todos os chamadores compartilharem o cache.
    force=True ignora o cache (use só quando precisar de leitura garantidamente
    fresca, ex.: logo após abrir/fechar ordem).
    """
    global _ban_until_ms
    now = time.time()
    now_ms = now * 1000.0

    def _ok(data, *, stale: bool = False, banned: bool = False) -> dict:
        out = {"ok": True, "positions": _filter_positions(data, symbol),
               "count": len(_filter_positions(data, symbol)),
               "testnet": _TESTNET, "exchange": "binance"}
        if stale:
            out["stale"] = True
        if banned:
            out["rate_limited"] = True
            out["ban_until_ms"] = _ban_until_ms
        return out

    # 1. Em cooldown de ban: nunca chama a API (senão escala o ban). Devolve o
    #    último snapshot conhecido marcado como stale; se não há cache, erro claro.
    if now_ms < _ban_until_ms:
        if _positions_cache["data"] is not None:
            return _ok(_positions_cache["data"], stale=True, banned=True)
        return {"ok": False, "code": -1003,
                "msg": f"rate-limit cooldown ativo até {int(_ban_until_ms)} (sem cache p/ servir)",
                "ban_until_ms": _ban_until_ms, "testnet": _TESTNET, "exchange": "binance"}

    # 2. Cache fresco → serve sem chamar a API.
    if (not force and _positions_cache["data"] is not None
            and (now - _positions_cache["ts"]) < _POSITIONS_CACHE_TTL):
        return _ok(_positions_cache["data"])

    # 3. Fetch real (TODAS as posições).
    res = await _signed_request("GET", "/fapi/v2/positionRisk", None)
    if not res.get("ok"):
        # _cooldown = o _signed_request já curto-circuitou (ban armado por outro
        # endpoint). NÃO re-armar (evita auto-extensão) — só servir o stale.
        if res.get("_cooldown"):
            if _positions_cache["data"] is not None:
                return _ok(_positions_cache["data"], stale=True, banned=True)
            return res
        ban_ms = _parse_ban_until_ms(res)
        if ban_ms > 0:
            _arm_ban(ban_ms, "positionRisk")  # arma + ALERTA (idempotente p/ msm janela)
            log.warning(
                f"[binance] positionRisk rate-limit/ban detectado — cooldown até "
                f"{int(ban_ms)} ({get_positions_ban_status()['seconds_left']}s). "
                f"Servindo cache e parando de chamar pra não escalar."
            )
        if _positions_cache["data"] is not None:
            return _ok(_positions_cache["data"], stale=True, banned=True)
        return res

    rows = res["result"] or []
    positions = []
    for p in rows:
        size = abs(float(p.get("positionAmt") or 0))
        if size <= 0:
            continue
        amt = float(p.get("positionAmt") or 0)
        side = "Buy" if amt > 0 else "Sell"
        positions.append({
            "symbol": p.get("symbol"),
            "side": side,
            "size": size,
            "entry_price": float(p.get("entryPrice") or 0),
            "mark_price": float(p.get("markPrice") or 0),
            "unrealized_pnl": float(p.get("unRealizedProfit") or 0),
            "leverage": float(p.get("leverage") or 0),
            "position_value": float(p.get("notional") or 0),
            "take_profit": None,  # Binance não retorna TP/SL nesse endpoint
            "stop_loss": None,
        })
    _positions_cache["data"] = positions
    _positions_cache["ts"] = now
    return _ok(positions)


async def place_protection_orders(
    symbol: str,
    entry_side: str,        # lado da ENTRADA ("Buy" | "Sell" ou "BUY" | "SELL")
    qty: float,             # qty total da posição (será dividida 45/55 se tp1+tp2)
    *,
    stop_loss: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp1_qty_pct: float = 0.45,
    client_order_id_prefix: Optional[str] = None,
    dedup_live: bool = False,
    mutation_guard: Optional[Callable[[], Awaitable[bool]]] = None,
    _progress: Optional[dict] = None,
) -> dict:
    """
    Cria as ordens condicionais (SL + TP1 parcial + TP2 restante) para uma posição
    JÁ ABERTA. Não cria entry — útil tanto pro fluxo bracket-na-entrada quanto
    pra backfill de posições já existentes sem proteção.

    Convenções:
      - entry_side = "Buy" (long) → counter_side = "SELL" (fecha)
      - SL: STOP_MARKET com closePosition=true (fecha tudo se ruir)
      - TP1: TAKE_PROFIT_MARKET com quantity = qty * 0.45 + reduceOnly=true
      - TP2: TAKE_PROFIT_MARKET com closePosition=true (fecha o restante)

    Retorno:
      {
        "sl_ok": bool, "sl_order_id": str|None, "sl_msg": str|None,
        "tp1_ok": bool, "tp1_order_id": str|None, "tp1_msg": str|None, "tp1_qty": float,
        "tp2_ok": bool, "tp2_order_id": str|None, "tp2_msg": str|None,
        "tp1_skipped": bool,  # true se qty*0.45 arredondou pra 0 → manda 100% no TP2
      }
    """
    sym = to_binance(symbol) if "/" in symbol else symbol
    binance_entry_side = entry_side.upper()
    counter_side = "SELL" if binance_entry_side == "BUY" else "BUY"

    # ── Idempotência ao vivo (anti-duplicação) ───────────────────────────────
    # Quando dedup_live=True, consulta as ordens condicionais JÁ vivas na
    # corretora e pula qualquer perna que já exista (mesmo type+side+trigger≈).
    # Mata as 3 fontes de duplicata: (1) corrida confirmação×auto-cura, (2) IDs
    # diferentes entre caminhos sem dedup nativo, (3) retry que recoloca após
    # timeout-com-sucesso. NÃO usar na transição pós-TP1 (ela cancela+recoloca
    # o SL de propósito). Leitura incerta → fail-open (coloca; melhor dup que nu).
    _existing: list[dict] = []
    if dedup_live:
        try:
            live = await get_open_algo_orders(sym)
            if live.get("ok"):
                _existing = live.get("orders") or []
            else:
                log.info(
                    f"[dedup] {sym} leitura de ordens vivas incerta "
                    f"({live.get('msg') or live.get('error')}) — segue sem dedup"
                )
        except Exception as e:
            log.warning(f"[dedup] {sym} get_open_algo_orders erro: {e} — segue sem dedup")

    def _existing_leg_id(
        otype: str,
        trigger: float,
        *,
        expected_qty: float,
        full_close: bool,
    ) -> str | None:
        """algoId de uma perna realmente equivalente, ou ``None``.

        Preço e lado iguais não bastam: uma conditional sem ``reduceOnly`` pode
        inverter a posição, e uma perna quantity-based subdimensionada deixa
        parte da exposição descoberta. Para pernas parciais, exige a quantidade
        exata; para fechamento total, aceita ``closePosition`` ou cobertura
        reduce-only de pelo menos toda a quantidade esperada.
        """
        if not trigger or trigger <= 0:
            return None
        for o in _existing:
            if (o.get("type") or "").upper() != otype:
                continue
            if (o.get("side") or "").upper() != counter_side:
                continue
            ot = o.get("trigger_price") or 0
            if ot <= 0:
                continue
            if abs(ot - trigger) / trigger <= 0.002:  # 0.2% — distingue TP1 de TP2
                algo_id = str(o.get("algo_id") or "")
                if not algo_id:
                    continue
                if o.get("close_position"):
                    if full_close:
                        return algo_id
                    # closePosition não é equivalente a uma realização parcial.
                    continue
                if not o.get("reduce_only"):
                    continue
                try:
                    covered_qty = float(o.get("quantity") or 0.0)
                except (TypeError, ValueError):
                    covered_qty = 0.0
                tolerance = max(1e-12, abs(float(expected_qty)) * 1e-9)
                if full_close:
                    if covered_qty + tolerance < float(expected_qty):
                        continue
                elif abs(covered_qty - float(expected_qty)) > tolerance:
                    continue
                return algo_id
        return None

    out = {
        "sl_ok": True, "sl_order_id": None, "sl_msg": None,
        "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0.0,
        "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
        "tp1_skipped": False,
        "tp1_requested": bool(tp1 is not None and tp2 is not None),
        "tp2_requested": bool(tp1 is not None or tp2 is not None),
        # #1 runner: preenchidos só quando o split do TP2 é aplicado (bracket de
        # abertura + RUNNER_ENABLED). runner_qty = qty que sobra correndo após o TP2.
        "is_runner": False, "runner_qty": 0.0,
        "sl_submission_unknown": False,
    }

    def _publish_progress() -> None:
        if _progress is not None:
            _progress.clear()
            _progress.update(out)

    qty_total = await _round_qty(sym, float(qty))
    if qty_total <= 0:
        out["sl_ok"] = out["tp1_ok"] = out["tp2_ok"] = False
        out["sl_msg"] = out["tp1_msg"] = out["tp2_msg"] = f"qty inválido após round: {qty}"
        return out

    # ── TP1 qty primeiro (precisamos pra calcular qty restante de SL/TP2) ─
    has_partial = tp1 is not None and tp2 is not None
    tp1_qty = 0.0
    if has_partial:
        tp1_qty_raw = float(qty_total) * float(tp1_qty_pct)
        tp1_qty = await _round_qty(sym, tp1_qty_raw)
        if tp1_qty <= 0:
            out["tp1_skipped"] = True
            log.warning(
                f"[binance] TP1 skip {sym}: qty*{tp1_qty_pct} ({tp1_qty_raw}) arredondou pra 0 "
                f"→ manda 100% no TP2"
            )
            has_partial = False

    qty_remaining = (
        float(Decimal(str(qty_total)) - Decimal(str(tp1_qty)))
        if has_partial else qty_total
    )

    # ── Helper: cria 1 conditional via Algo Order API ────────────────────
    # Desde 2025-12-09, STOP_MARKET/TAKE_PROFIT_MARKET DEVEM ir pelo endpoint
    # /fapi/v1/algoOrder (não mais /fapi/v1/order). Diferenças:
    #   - usa `triggerPrice` em vez de `stopPrice`
    #   - precisa de `algoType=CONDITIONAL`
    #   - retorna `algoId` (não `orderId`)
    async def _place_algo(
        otype: str,
        trigger_price: float,
        q: float,
        label: str,
        close_position: bool = False,
    ) -> tuple[bool, str | None, str | None, bool]:
        params = {
            "algoType": "CONDITIONAL",
            "symbol": sym,
            "side": counter_side,
            "type": otype,
            "triggerPrice": trigger_price,
            "workingType": "MARK_PRICE",  # mark price evita trigger por wick fino
        }
        if close_position:
            # closePosition=true fecha 100% da posição no trigger — imune a
            # descasamento de qty/stepSize (a poeira que sobrava com quantity
            # fixo). Não envia quantity nem reduceOnly (a API rejeita junto).
            params["closePosition"] = "true"
        else:
            params["quantity"] = q
            params["reduceOnly"] = "true"
        if client_order_id_prefix:
            params["clientAlgoId"] = f"{client_order_id_prefix}-{label}"

        last_msg: str | None = None
        submission_unknown = False
        for attempt in range(1, _ALGO_MAX_ATTEMPTS + 1):
            # Invariante #6 (P03.1E): revalida o lease/claim IMEDIATAMENTE antes de
            # CADA POST (tentativa, retry e fallback passam por aqui). Guard negando
            # — ou levantando — aborta ANTES de qualquer mutação na corretora. Como
            # nada foi enviado, submission_unknown=False (não é ambíguo: é sabidamente
            # não-enviado). Sem guard, comportamento legado preservado.
            if mutation_guard is not None:
                try:
                    _allowed = await mutation_guard()
                except Exception as _mg_exc:  # noqa: BLE001
                    log.error(f"[binance] {label.upper()} ABORTADO {sym}: mutation_guard exceção: {_mg_exc}")
                    return False, None, f"mutation_guard exceção (fail-closed): {_mg_exc}", False
                if not _allowed:
                    log.error(f"[binance] {label.upper()} ABORTADO {sym}: mutation_guard negou (lease inválido) — sem POST")
                    return False, None, (last_msg or "mutation_guard negou: lease/claim inválido"), False
            res = await _signed_request("POST", "/fapi/v1/algoOrder", params)
            if res.get("ok"):
                algo_id = str((res.get("result") or {}).get("algoId") or "")
                if algo_id:
                    tag = f" (tentativa {attempt})" if attempt > 1 else ""
                    log.info(f"[binance] {label.upper()} ok {sym} {otype} @ {trigger_price} qty={q} algoId={algo_id}{tag}")
                    return True, algo_id, None, False
                # HTTP/API ok sem identificador não é confirmação auditável.
                last_msg = "exchange respondeu ok sem algoId"
                submission_unknown = True
                log.warning(f"[binance] {label.upper()} {sym}: {last_msg}")
            else:
                last_msg = res.get("msg") or res.get("error")
                submission_unknown = submission_unknown or _is_ambiguous_order_submission(res)
            code = res.get("code")
            # Erro permanente (preço inválido, gatilho imediato, precisão) → não adianta repetir.
            if _is_permanent_algo_error(code, last_msg):
                log.error(
                    f"[binance] {label.upper()} FALHOU {sym} {otype} @ {trigger_price} qty={q}: "
                    f"{last_msg} (code={code}, permanente — sem retry)"
                )
                return False, None, last_msg, submission_unknown
            # Erro transitório (rate-limit, timeout, indisponibilidade) → backoff e tenta de novo.
            if attempt < _ALGO_MAX_ATTEMPTS:
                # Anti-dup do retry: a falha pode ser timeout-COM-sucesso (a ordem
                # chegou na corretora mas a resposta se perdeu). Antes de recolocar,
                # confere se a ordem com nosso clientAlgoId já está viva — se sim,
                # adota e não duplica.
                if client_order_id_prefix:
                    want_cid = params.get("clientAlgoId")
                    try:
                        chk = await get_open_algo_orders(sym)
                        if chk.get("ok"):
                            for o in (chk.get("orders") or []):
                                if o.get("client_algo_id") == want_cid:
                                    aid = str(o.get("algo_id") or "")
                                    if aid:
                                        log.info(
                                            f"[binance] {label.upper()} {sym} já vivo (clientAlgoId={want_cid} "
                                            f"algoId={aid}) — timeout-com-sucesso, adota sem recolocar"
                                        )
                                        return True, aid, None, False
                                    submission_unknown = True
                                    log.warning(
                                        f"[binance] {label.upper()} {sym} apareceu sem algoId "
                                        f"(clientAlgoId={want_cid}); não adota como confirmado"
                                    )
                    except Exception as e:
                        log.warning(f"[binance] {label.upper()} {sym} recheck pré-retry falhou: {e}")
                delay = _ALGO_RETRY_BASE_DELAY * attempt
                log.warning(
                    f"[binance] {label.upper()} falha transitória {sym} {otype} "
                    f"(tentativa {attempt}/{_ALGO_MAX_ATTEMPTS}): {last_msg} (code={code}) — retry em {delay:.2f}s"
                )
                await asyncio.sleep(delay)
        log.error(
            f"[binance] {label.upper()} FALHOU {sym} {otype} @ {trigger_price} qty={q}: "
            f"{last_msg} (esgotou {_ALGO_MAX_ATTEMPTS} tentativas)"
        )
        return False, None, last_msg, submission_unknown

    # ── SL ───────────────────────────────────────────────────────────────
    if stop_loss is not None:
        sl_price = await _round_price(sym, float(stop_loss))
        dup_id = _existing_leg_id(
            "STOP_MARKET", sl_price,
            expected_qty=qty_total, full_close=True,
        )
        if dup_id:
            log.info(f"[dedup] SL {sym} @ {sl_price} já vivo algoId={dup_id} — pula recolocação")
            out["sl_ok"] = True
            out["sl_order_id"] = dup_id
            out["sl_msg"] = "dedup: já existia"
        else:
            # closePosition=true fecha tudo (sem sobrar poeira). Se a API rejeitar
            # closePosition no algoOrder, cai pra quantity+reduceOnly (nunca fica
            # sem stop).
            ok, oid, msg, close_position_unknown = await _place_algo(
                "STOP_MARKET", sl_price, qty_total, "sl", close_position=True,
            )
            fallback_unknown = False
            if not ok:
                log.warning(f"[binance] SL closePosition falhou {sym}: {msg} — fallback quantity")
                ok, oid, msg, fallback_unknown = await _place_algo(
                    "STOP_MARKET", sl_price, qty_total, "sl",
                )
            out["sl_ok"] = ok
            out["sl_order_id"] = oid
            out["sl_msg"] = msg
            out["sl_submission_unknown"] = bool(
                not ok and (close_position_unknown or fallback_unknown)
            )

        # Regra cardinal: se o SL solicitado não foi confirmado, não cria TPs.
        # O caller pós-fill executará o rollback emergencial imediatamente.
        if not out["sl_ok"] or not out["sl_order_id"]:
            out["sl_ok"] = False
            out["tp1_ok"] = out["tp2_ok"] = False
            out["tp1_msg"] = out["tp2_msg"] = "não enviado: SL não confirmado"
            return out

    # ── TP1 parcial 45% ──────────────────────────────────────────────────
    if has_partial:
        tp1_price = await _round_price(sym, float(tp1))
        dup_id = _existing_leg_id(
            "TAKE_PROFIT_MARKET", tp1_price,
            expected_qty=tp1_qty, full_close=False,
        )
        if dup_id:
            log.info(f"[dedup] TP1 {sym} @ {tp1_price} já vivo algoId={dup_id} — pula recolocação")
            out["tp1_ok"] = True
            out["tp1_order_id"] = dup_id
            out["tp1_msg"] = "dedup: já existia"
            out["tp1_qty"] = tp1_qty
        else:
            ok, oid, msg, _ = await _place_algo(
                "TAKE_PROFIT_MARKET", tp1_price, tp1_qty, "tp1",
            )
            out["tp1_ok"] = ok
            out["tp1_order_id"] = oid
            out["tp1_msg"] = msg
            out["tp1_qty"] = tp1_qty if ok else 0.0
        _publish_progress()

    # ── #1 Runner: decide se o TP2 fecha 100% (closePosition) ou deixa cauda ──
    # Só divide no BRACKET DE ABERTURA (has_partial = tp1 e tp2 juntos) e com
    # RUNNER_ENABLED. runner_qty = fração da posição TOTAL que segue correndo
    # após o TP2; o TP2 fecha (qty_remaining − runner_qty) como reduce-only
    # parcial. Fallback seguro pro closePosition se o split arredondar pra 0.
    runner_qty = 0.0
    tp2_close_qty = qty_remaining
    tp2_partial = False  # True → TP2 vira reduce-only parcial (deixa runner)
    if RUNNER_ENABLED and has_partial and RUNNER_QTY_PCT > 0:
        rq = await _round_qty(sym, float(qty_total) * float(RUNNER_QTY_PCT))
        close_q = await _round_qty(sym, float(qty_remaining) - float(rq))
        if rq > 0 and close_q > 0:
            runner_qty = rq
            tp2_close_qty = close_q
            tp2_partial = True
            out["is_runner"] = True
            out["runner_qty"] = runner_qty
            log.info(
                f"[binance] runner {sym}: TP2 parcial reduce-only qty={tp2_close_qty} "
                f"(fecha), runner={runner_qty} segue correndo (de {qty_remaining} restante)"
            )
        else:
            log.info(
                f"[binance] runner {sym}: split arredondou pra 0 "
                f"(runner={rq}, tp2={close_q}) — mantém TP2 closePosition"
            )

    # ── TP2 / TP único ──────────────────────────────────────────────────────
    # Sem runner: fecha o restante (closePosition, sem poeira).
    # Com runner: fecha só tp2_close_qty (reduce-only), deixando runner_qty vivo.
    tp_final = tp2 if tp2 is not None else tp1
    if tp_final is not None:
        tp_price = await _round_price(sym, float(tp_final))
        dup_id = _existing_leg_id(
            "TAKE_PROFIT_MARKET", tp_price,
            expected_qty=tp2_close_qty if tp2_partial else qty_remaining,
            full_close=not tp2_partial,
        )
        if dup_id:
            log.info(f"[dedup] TP2 {sym} @ {tp_price} já vivo algoId={dup_id} — pula recolocação")
            out["tp2_ok"] = True
            out["tp2_order_id"] = dup_id
            out["tp2_msg"] = "dedup: já existia"
        elif tp2_partial:
            # Runner: TP2 reduce-only parcial (NÃO closePosition — a cauda sobra).
            ok, oid, msg, _ = await _place_algo(
                "TAKE_PROFIT_MARKET", tp_price, tp2_close_qty, "tp2",
            )
            out["tp2_ok"] = ok
            out["tp2_order_id"] = oid
            out["tp2_msg"] = msg
        else:
            ok, oid, msg, _ = await _place_algo(
                "TAKE_PROFIT_MARKET", tp_price, qty_remaining, "tp2", close_position=True,
            )
            if not ok:
                log.warning(f"[binance] TP2 closePosition falhou {sym}: {msg} — fallback quantity")
                ok, oid, msg, _ = await _place_algo(
                    "TAKE_PROFIT_MARKET", tp_price, qty_remaining, "tp2",
                )
            out["tp2_ok"] = ok
            out["tp2_order_id"] = oid
            out["tp2_msg"] = msg

    _publish_progress()
    return out


async def _fresh_position_size(symbol: str) -> tuple[Optional[float], str]:
    """Retorna o tamanho vivo do símbolo ou None quando não é possível confirmar.

    Uma leitura stale/rate-limited não prova que a posição zerou e, portanto,
    nunca é tratada como sucesso do fechamento emergencial.
    """
    try:
        res = await get_positions(symbol, force=True)
    except Exception as exc:
        return None, f"positionRisk exception: {exc}"
    if not res.get("ok") or res.get("stale") or res.get("rate_limited"):
        return None, res.get("msg") or res.get("error") or "positionRisk incerto/stale"
    positions = res.get("positions") or []
    size = sum(abs(float(p.get("size") or 0.0)) for p in positions)
    return size, "posição confirmada"


async def _emergency_close_after_stop_failure(
    symbol: str,
    entry_side: str,
    qty: float,
    *,
    client_order_id_prefix: Optional[str] = None,
) -> dict:
    """Fecha a qty efetivamente aberta após falha do SL e confirma o resultado.

    Usa MARKET reduceOnly direto (não chama place_order, evitando recursão).
    Fill parcial reduz a quantidade restante e é tentado novamente. Timeout ou
    resposta ambígua só contam como sucesso se positionRisk fresco confirmar
    posição zerada.
    """
    sym = to_binance(symbol) if "/" in symbol else symbol
    close_side = "SELL" if entry_side.upper() == "BUY" else "BUY"
    requested = await _round_qty(sym, float(qty))
    if requested <= 0:
        return {
            "ok": False, "confirmed_flat": False, "attempts": 0,
            "requested_qty": requested, "closed_qty": 0.0,
            "remaining_qty": requested, "error": "qty inválida para fechamento emergencial",
        }

    remaining = requested
    closed_qty = 0.0
    requested_d = Decimal(str(requested))
    closed_qty_d = Decimal("0")
    last_error = "fechamento não tentado"
    attempts_made = 0

    def _account_closed(value: float) -> None:
        nonlocal closed_qty, closed_qty_d
        try:
            delta = max(Decimal("0"), Decimal(str(value)))
        except InvalidOperation:
            delta = Decimal("0")
        closed_qty_d = min(requested_d, closed_qty_d + delta)
        closed_qty = float(closed_qty_d)

    def _remaining_from_fills() -> float:
        return float(max(Decimal("0"), requested_d - closed_qty_d))

    # Idempotência: um timeout anterior pode já ter fechado. Só uma leitura
    # fresca e zerada evita emitir outra ordem.
    initial_size, initial_msg = await _fresh_position_size(sym)
    if initial_size is not None and initial_size > 1e-12:
        remaining = min(remaining, initial_size)
    elif initial_size is not None:
        # Logo após um fill confirmado, positionRisk pode atrasar alguns
        # instantes. Não declarar rollback sem sequer enviar reduceOnly.
        last_error = "positionRisk inicial zerado ignorado por possível atraso pós-fill"
    else:
        last_error = initial_msg

    for attempt in range(1, _EMERGENCY_CLOSE_ATTEMPTS + 1):
        close_qty = await _round_qty(sym, remaining)
        if close_qty <= 0:
            break
        attempts_made = attempt
        # Binance limita newClientOrderId a 36 caracteres.
        base = (client_order_id_prefix or f"cw-nostop-{sym}").replace("/", "")
        cid = f"{base[:32]}-{attempt}"[-36:]
        params = {
            "symbol": sym,
            "side": close_side,
            "type": "MARKET",
            "quantity": close_qty,
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
            "newClientOrderId": cid,
        }
        res = await _signed_request("POST", "/fapi/v1/order", params)
        result = res.get("result") or {}
        status = str(result.get("status") or "").upper()
        close_terminal_statuses = {
            "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED",
        }
        raw_executed = result.get("executedQty")
        executed_confirmed = "executedQty" in result and raw_executed not in (None, "")
        try:
            executed = min(close_qty, max(0.0, float(raw_executed or 0.0)))
        except (TypeError, ValueError):
            executed = 0.0
            executed_confirmed = False
        response_terminal_confirmed = bool(
            res.get("ok")
            and status in close_terminal_statuses
            and (status in {"FILLED", "REJECTED"} or executed_confirmed)
        )

        if res.get("ok") and status == "FILLED":
            # RESULT + FILLED é confirmação da própria exchange. Em one-way
            # mode, reduceOnly garante que a ordem não inverte a posição.
            executed = executed or close_qty
            _account_closed(executed)
            remaining = _remaining_from_fills()
            last_error = "market RESULT FILLED; aguardando positionRisk zerado"
        elif res.get("ok") and executed > 0:
            _account_closed(executed)
            remaining = _remaining_from_fills()
            last_error = f"fill parcial {closed_qty:g}/{requested:g}"
        else:
            last_error = res.get("msg") or res.get("error") or f"status ambíguo {status or 'N/D'}"

        # Anti-duplicação de timeout-com-sucesso: antes do retry, uma leitura
        # fresca pode confirmar que o MARKET anterior zerou a posição.
        live_size, pos_msg = await _fresh_position_size(sym)
        if live_size is not None and live_size <= 1e-12:
            return {
                "ok": True, "confirmed_flat": True, "attempts": attempt,
                "requested_qty": requested, "closed_qty": requested,
                "remaining_qty": 0.0, "confirmation": "positionRisk zerado",
            }
        if live_size is not None:
            if live_size > 1e-12 and not response_terminal_confirmed:
                # Posição ainda viva + resposta não terminal: antes de uma nova
                # reduceOnly, prova quanto a ordem anterior executou e que não
                # continuará preenchendo. Sem isso, bloqueia o retry.
                try:
                    order_check = await get_order(sym, client_order_id=cid)
                except Exception as exc:
                    order_check = {"ok": False, "error": str(exc)}
                checked_status = str(order_check.get("status") or "").upper()
                checked_raw = order_check.get("raw")
                checked_executed_confirmed = bool(
                    (isinstance(checked_raw, dict) and "executedQty" in checked_raw)
                    or (not isinstance(checked_raw, dict) and "executed_qty" in order_check)
                )
                try:
                    checked_executed = min(
                        close_qty,
                        max(0.0, float(order_check.get("executed_qty") or 0.0)),
                    )
                except (TypeError, ValueError):
                    checked_executed = 0.0
                    checked_executed_confirmed = False
                if checked_status == "FILLED":
                    # FILLED é terminal e prova execução integral mesmo quando
                    # o payload de consulta omite executedQty.
                    checked_executed = close_qty
                if (
                    not order_check.get("ok")
                    or checked_status not in close_terminal_statuses
                    or (
                        checked_status not in {"FILLED", "REJECTED"}
                        and not checked_executed_confirmed
                    )
                ):
                    return {
                        "ok": False,
                        "confirmed_flat": False,
                        "attempts": attempts_made,
                        "requested_qty": requested,
                        "closed_qty": closed_qty,
                        "remaining_qty": remaining,
                        "retry_blocked": True,
                        "close_order_client_id": cid,
                        "order_check": order_check,
                        "error": (
                            f"{last_error}; posição viva={live_size:g}, mas ordem anterior "
                            "não foi reconciliada como terminal; retry bloqueado"
                        ),
                    }
                if checked_executed > executed:
                    _account_closed(checked_executed - executed)
                    remaining = _remaining_from_fills()
            # Nunca fecha mais que a qty originada por esta entrada. A leitura
            # serve para limitar o restante, sem consumir eventual posição manual.
            remaining = min(_remaining_from_fills(), live_size)
        else:
            # Uma resposta ambígua seguida de positionRisk UNKNOWN não autoriza
            # novo MARKET: a primeira ordem pode ter sido executada e um retry
            # cego poderia consumir exposição preexistente do mesmo símbolo.
            # Consulta o client id só para telemetria/reconciliação, mas mantém o
            # estado UNKNOWN até uma leitura fresca da posição.
            order_check = None
            try:
                order_check = await get_order(sym, client_order_id=cid)
            except Exception as exc:
                order_check = {"ok": False, "error": f"consulta da ordem falhou: {exc}"}
            if order_check.get("ok"):
                try:
                    checked_executed = min(
                        close_qty,
                        max(0.0, float(order_check.get("executed_qty") or 0.0)),
                    )
                except (TypeError, ValueError):
                    checked_executed = 0.0
                if checked_executed > executed:
                    _account_closed(checked_executed - executed)
                    remaining = _remaining_from_fills()
                checked_status = str(order_check.get("status") or "N/D").upper()
                order_msg = f"close order={checked_status} executed={checked_executed:g}"
            else:
                order_msg = (
                    order_check.get("msg") or order_check.get("error")
                    or "close order não reconciliada"
                )
            last_error = (
                f"{last_error}; {pos_msg}; {order_msg}; "
                "retry bloqueado enquanto a posição estiver UNKNOWN"
            )
            return {
                "ok": False,
                "confirmed_flat": False,
                "attempts": attempts_made,
                "requested_qty": requested,
                "closed_qty": closed_qty,
                "remaining_qty": remaining,
                "retry_blocked": True,
                "close_order_client_id": cid,
                "order_check": order_check,
                "error": last_error,
            }
        if attempt < _EMERGENCY_CLOSE_ATTEMPTS:
            await asyncio.sleep(_EMERGENCY_CLOSE_RETRY_DELAY * attempt)

    return {
        "ok": False, "confirmed_flat": False,
        "attempts": attempts_made,
        "requested_qty": requested, "closed_qty": closed_qty,
        "remaining_qty": remaining, "error": last_error,
    }


def _algo_cancel_is_terminal(result: dict) -> bool:
    """True quando o cancel confirmou sucesso ou que a conditional já não existe."""
    if result.get("ok"):
        return True
    try:
        code = int(result.get("code"))
    except (TypeError, ValueError):
        code = None
    msg = str(result.get("msg") or result.get("error") or "").lower()
    return code in {-2011, -2013} or "unknown order" in msg or "not found" in msg


async def _cleanup_entry_conditionals(
    symbol: str,
    client_order_id_prefix: Optional[str],
    protection: dict,
) -> dict:
    """Cancela e confirma a ausência das conditionals desta entrada.

    A confirmação por listagem é obrigatória quando o SL pode ter sido criado
    em um timeout-com-sucesso sem devolver algoId. O match usa clientAlgoId
    exato para que ``cw-1`` jamais alcance ``cw-10``.
    """
    known_ids = {
        str(protection.get(key))
        for key in ("sl_order_id", "tp1_order_id", "tp2_order_id")
        if protection.get(key)
    }
    wanted_client_ids = (
        {f"{client_order_id_prefix}-{leg}" for leg in ("sl", "tp1", "tp2")}
        if client_order_id_prefix else set()
    )
    cancelled_ids: set[str] = set()
    errors: list[str] = []
    ambiguous_submission = bool(protection.get("sl_submission_unknown"))
    saw_target = False

    for algo_id in sorted(known_ids):
        try:
            res = await cancel_algo_order(algo_id)
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        if _algo_cancel_is_terminal(res):
            cancelled_ids.add(algo_id)
        else:
            errors.append(
                f"cancel algoId={algo_id}: {res.get('msg') or res.get('error') or 'incerto'}"
            )

    if not wanted_client_ids and not known_ids:
        errors.append("client_order_id ausente; não é possível excluir conditional sem algoId")
        return {
            "state": "PENDING",
            "confirmed_absent": False,
            "cancelled_algo_ids": sorted(cancelled_ids),
            "remaining_algo_ids": [],
            "expected_client_algo_ids": [],
            "errors": errors,
        }

    remaining_ids: list[str] = []
    consecutive_empty_scans = 0
    cancelled_live_match = False
    # Três scans cobrem: vazio inicial -> ordem aparece atrasada -> cancela ->
    # confirma vazio. Sem nenhuma ordem visível, dois vazios consecutivos são
    # necessários para confirmar ausência.
    for scan in range(3):
        try:
            live = await get_open_algo_orders(symbol)
        except Exception as exc:
            live = {"ok": False, "error": str(exc)}
        if not live.get("ok") or live.get("stale") or live.get("rate_limited"):
            errors.append(
                "listagem de conditionals incerta: "
                f"{live.get('msg') or live.get('error') or 'stale/rate-limited'}"
            )
            return {
                "state": "PENDING",
                "confirmed_absent": False,
                "cancelled_algo_ids": sorted(cancelled_ids),
                "remaining_algo_ids": remaining_ids,
                "expected_client_algo_ids": sorted(wanted_client_ids),
                "errors": errors,
            }

        matches = [
            order for order in (live.get("orders") or [])
            if (
                str(order.get("client_algo_id") or "") in wanted_client_ids
                or str(order.get("algo_id") or "") in known_ids
            )
        ]
        saw_target = saw_target or bool(matches)
        remaining_ids = [str(order.get("algo_id")) for order in matches if order.get("algo_id")]
        if not matches:
            consecutive_empty_scans += 1
            if cancelled_live_match or (
                consecutive_empty_scans >= 2 and not ambiguous_submission
            ):
                return {
                    "state": "CONFIRMED",
                    "confirmed_absent": True,
                    "cancelled_algo_ids": sorted(cancelled_ids),
                    "remaining_algo_ids": [],
                    "expected_client_algo_ids": sorted(wanted_client_ids),
                    "errors": [],
                }
            if scan < 2:
                await asyncio.sleep(min(_ALGO_RETRY_BASE_DELAY, 0.1))
                continue
            break
        consecutive_empty_scans = 0
        all_matches_cancelled = True
        for order in matches:
            algo_id = str(order.get("algo_id") or "")
            if not algo_id:
                all_matches_cancelled = False
                errors.append(
                    f"conditional {order.get('client_algo_id')} sem algoId para cancelar"
                )
                continue
            try:
                cancel_res = await cancel_algo_order(algo_id)
            except Exception as exc:
                cancel_res = {"ok": False, "error": str(exc)}
            if _algo_cancel_is_terminal(cancel_res):
                cancelled_ids.add(algo_id)
            else:
                all_matches_cancelled = False
                errors.append(
                    f"cancel algoId={algo_id}: "
                    f"{cancel_res.get('msg') or cancel_res.get('error') or 'incerto'}"
                )
        cancelled_live_match = cancelled_live_match or all_matches_cancelled
        if scan < 2:
            await asyncio.sleep(min(_ALGO_RETRY_BASE_DELAY, 0.1))

    if ambiguous_submission and not saw_target:
        errors.append(
            "submissão do SL ficou ambígua e nenhuma conditional apareceu ainda; "
            "cleanup permanece pendente até reconciliação tardia/manual"
        )
        final_state = "PENDING"
    else:
        errors.append(f"conditionals ainda visíveis: {remaining_ids}")
        final_state = "FAILED"
    return {
        "state": final_state,
        "confirmed_absent": False,
        "cancelled_algo_ids": sorted(cancelled_ids),
        "remaining_algo_ids": remaining_ids,
        "expected_client_algo_ids": sorted(wanted_client_ids),
        "errors": errors,
    }


async def _enforce_entry_safety(
    symbol: str,
    entry_side: str,
    executed_qty: float,
    stop_loss: Optional[float],
    protection: dict,
    *,
    client_order_id_prefix: Optional[str] = None,
    entry_state: str = "FILLED",
    operation_kind: str = "ENTRY",
) -> dict:
    """Transição cardinal: ENTRY_CONFIRMED -> SL_CONFIRMED ou FLATTENED.

    TP ausente degrada o estado e pode ser auto-curado; SL ausente dispara
    fechamento imediato. O retorno é auditável por todos os callers.
    """
    if operation_kind != "ENTRY":
        return {
            "safety_state": "NOT_APPLICABLE",
            "entry_state": "NOT_APPLICABLE", "protection_state": "NOT_REQUESTED",
            "cleanup_state": "NOT_APPLICABLE",
            "entry_confirmed": None, "position_open": None,
            "emergency_close_attempted": False, "emergency_close_ok": None,
            "manual_intervention_required": False, "quarantine_required": False,
        }
    if stop_loss is None:
        return {
            "safety_state": "ENTRY_CONFIRMED_NO_SL_REQUESTED",
            "entry_state": entry_state, "protection_state": "NOT_REQUESTED",
            "cleanup_state": "NOT_APPLICABLE",
            "entry_confirmed": True, "position_open": True,
            "emergency_close_attempted": False, "emergency_close_ok": None,
            "manual_intervention_required": False, "quarantine_required": False,
        }
    if protection.get("sl_ok") and protection.get("sl_order_id"):
        tp1_requested = bool(protection.get(
            "tp1_requested",
            protection.get("tp1_ok") is False or protection.get("tp1_order_id"),
        ))
        tp2_requested = bool(protection.get(
            "tp2_requested",
            protection.get("tp2_ok") is False or protection.get("tp2_order_id"),
        ))
        tp1_confirmed = (
            not tp1_requested
            or bool(protection.get("tp1_skipped"))
            or bool(protection.get("tp1_ok") and protection.get("tp1_order_id"))
        )
        tp2_confirmed = (
            not tp2_requested
            or bool(protection.get("tp2_ok") and protection.get("tp2_order_id"))
        )
        tps_ok = tp1_confirmed and tp2_confirmed
        return {
            "safety_state": "PROTECTION_CONFIRMED" if tps_ok else "STOP_CONFIRMED_TP_DEGRADED",
            "entry_state": entry_state,
            "protection_state": "CONFIRMED" if tps_ok else "STOP_CONFIRMED_TP_DEGRADED",
            "cleanup_state": "NOT_APPLICABLE",
            "entry_confirmed": True, "position_open": True,
            "emergency_close_attempted": False, "emergency_close_ok": None,
            "manual_intervention_required": False, "quarantine_required": False,
        }

    log.critical(f"[binance] {symbol} ENTRY_CONFIRMED_WITHOUT_STOP — fechamento emergencial")
    closed = await _emergency_close_after_stop_failure(
        symbol, entry_side, executed_qty,
        client_order_id_prefix=f"{client_order_id_prefix or 'cw'}-nostop",
    )
    if closed.get("ok") and closed.get("confirmed_flat"):
        cleanup = await _cleanup_entry_conditionals(
            symbol, client_order_id_prefix, protection,
        )
        if not cleanup.get("confirmed_absent"):
            log.critical(
                f"[binance] {symbol} posição zerada, mas conditionals não foram "
                f"confirmadas como removidas: {cleanup}"
            )
            _fire_telegram(
                f"🚨 {symbol}: posição foi zerada, mas há STOP/TP possivelmente órfão. "
                "Entradas pausadas até limpeza manual.",
                "emergency_cleanup_failed",
            )
            return {
                "safety_state": "POSITION_FLAT_CONDITIONALS_UNKNOWN",
                "entry_state": entry_state, "protection_state": "FAILED",
                "cleanup_state": cleanup.get("state") or "PENDING",
                "entry_confirmed": True, "position_open": False,
                "emergency_close_attempted": True, "emergency_close_ok": True,
                "manual_intervention_required": True, "quarantine_required": True,
                "emergency_close": closed, "conditional_cleanup": cleanup,
            }
        log.critical(f"[binance] {symbol} posição sem SL fechada e confirmada")
        _fire_telegram(
            f"🛡️ {symbol}: SL falhou; posição fechada a mercado e confirmada.",
            "emergency_flatten_ok",
        )
        return {
            "safety_state": "FLATTENED_AFTER_SL_FAILURE",
            "entry_state": entry_state, "protection_state": "FAILED",
            "cleanup_state": "CONFIRMED",
            "entry_confirmed": True, "position_open": False,
            "emergency_close_attempted": True, "emergency_close_ok": True,
            "manual_intervention_required": False, "quarantine_required": False,
            "emergency_close": closed, "conditional_cleanup": cleanup,
        }

    log.critical(f"[binance] {symbol} POSIÇÃO SEM STOP; fechamento não confirmado: {closed}")
    _fire_telegram(
        f"🚨 {symbol}: SL falhou E fechamento automático não foi confirmado. AÇÃO MANUAL AGORA.",
        "emergency_flatten_failed",
    )
    return {
        "safety_state": "MANUAL_INTERVENTION_REQUIRED",
        "entry_state": entry_state, "protection_state": "FAILED",
        "cleanup_state": "NOT_ATTEMPTED",
        "entry_confirmed": True, "position_open": True,
        "emergency_close_attempted": True, "emergency_close_ok": False,
        "manual_intervention_required": True, "quarantine_required": True,
        "emergency_close": closed,
    }


def _protection_exception_result(exc: Exception) -> dict:
    """Converte exceção pós-fill em falha explícita para acionar o rollback."""
    msg = f"exception ao criar proteção: {type(exc).__name__}: {exc}"
    log.exception(f"[binance] {msg}")
    return {
        "sl_ok": False, "sl_order_id": None, "sl_msg": msg,
        "tp1_ok": False, "tp1_order_id": None, "tp1_msg": msg, "tp1_qty": 0.0,
        "tp2_ok": False, "tp2_order_id": None, "tp2_msg": msg,
        "tp1_skipped": False, "is_runner": False, "runner_qty": 0.0,
        "tp1_requested": False, "tp2_requested": False,
        "sl_submission_unknown": True,
    }


def _protection_not_requested_result() -> dict:
    return {
        "sl_ok": True, "sl_order_id": None, "sl_msg": None,
        "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0.0,
        "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
        "tp1_skipped": False, "is_runner": False, "runner_qty": 0.0,
        "tp1_requested": False, "tp2_requested": False,
        "sl_submission_unknown": False,
    }


async def _place_post_fill_protection(*args, **kwargs) -> dict:
    """Proteção pós-fill em duas fases: primeiro SL, depois TPs.

    O deadline cardinal vale isoladamente para o SL. Só depois de um ``algoId``
    confirmado os TPs são tentados com budget próprio. Assim, timeout/exception
    de TP degrada o lucro planejado sem transformar uma posição já protegida em
    falso incidente de "sem stop".
    """
    stop_loss = kwargs.get("stop_loss")
    tp1 = kwargs.get("tp1")
    tp2 = kwargs.get("tp2")

    if stop_loss is not None:
        sl_kwargs = dict(kwargs)
        sl_kwargs["tp1"] = None
        sl_kwargs["tp2"] = None
        sl_out = await asyncio.wait_for(
            place_protection_orders(*args, **sl_kwargs),
            timeout=_POST_FILL_PROTECTION_TIMEOUT_S,
        )
        if not sl_out.get("sl_ok") or not sl_out.get("sl_order_id"):
            return sl_out
    else:
        sl_out = _protection_not_requested_result()

    if tp1 is None and tp2 is None:
        return sl_out

    tp_kwargs = dict(kwargs)
    tp_kwargs["stop_loss"] = None
    tp_progress: dict = {}
    tp_kwargs["_progress"] = tp_progress
    try:
        tp_out = await asyncio.wait_for(
            place_protection_orders(*args, **tp_kwargs),
            timeout=_POST_FILL_PROTECTION_TIMEOUT_S,
        )
    except Exception as exc:
        tp_out = dict(tp_progress) if tp_progress else _protection_not_requested_result()
        tp_out["tp1_requested"] = bool(tp1 is not None and tp2 is not None)
        tp_out["tp2_requested"] = bool(tp1 is not None or tp2 is not None)
        msg = (
            f"proteção de lucro degradada após SL confirmado: "
            f"{type(exc).__name__}: {exc}"
        )
        if tp1 is not None and not tp_out.get("tp1_order_id"):
            tp_out["tp1_ok"] = False
            tp_out["tp1_msg"] = msg
            tp_out["tp1_qty"] = 0.0
        if not tp_out.get("tp2_order_id"):
            tp_out["tp2_ok"] = False
            tp_out["tp2_msg"] = msg
        tp_out["protection_timeout"] = isinstance(exc, asyncio.TimeoutError)

    merged = dict(sl_out)
    for key in (
        "tp1_ok", "tp1_order_id", "tp1_msg", "tp1_qty",
        "tp2_ok", "tp2_order_id", "tp2_msg", "tp1_skipped",
        "tp1_requested", "tp2_requested",
        "is_runner", "runner_qty", "protection_timeout",
    ):
        if key in tp_out:
            merged[key] = tp_out[key]
    return merged


async def place_order(
    symbol: str,
    side: str,           # "Buy" | "Sell" (Bybit-compat)
    qty: float,
    order_type: str = "Market",  # "Market" | "Limit"
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,   # = TP2 (alvo final). Se tp1 também vier, vira bracket 45/55.
    tp1: Optional[float] = None,           # TP1 parcial — quando setado junto com take_profit, dispara bracket
    tp1_qty_pct: float = 0.45,
    reduce_only: bool = False,
    leverage: Optional[int] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    """
    Cria ordem em futures USDT-M. Aceita "Buy/Sell" (Bybit-style) e traduz pra
    "BUY/SELL" (Binance). Para TP/SL, Binance exige ordens SEPARADAS — emitidas
    aqui em sequência após a entry.

    Modo bracket (quando `tp1` e `take_profit` ambos fornecidos):
      - Entry MARKET (100% qty)
      - SL STOP_MARKET (closePosition=true)
      - TP1 TAKE_PROFIT_MARKET qty=qty*45% (reduceOnly=true) — fecha parcial
      - TP2 TAKE_PROFIT_MARKET (closePosition=true) — fecha resto

    Modo simples (só `take_profit` ou só `stop_loss`):
      - Entry MARKET
      - 1 ordem SL e/ou 1 ordem TP com closePosition=true

    Retorno enriquecido com sl_ok/tp1_ok/tp2_ok pra caller propagar diagnóstico.
    """
    sym = to_binance(symbol) if "/" in symbol else symbol

    # Arredonda qty pro stepSize do símbolo (DOGE só aceita inteiro, etc.).
    qty_rounded = await _round_qty(sym, float(qty))
    if qty_rounded <= 0:
        f = await _get_symbol_filters(sym)
        return {"ok": False, "error": f"qty arredondado virou 0 (step={f.get('step')}, min={f.get('min_qty')}, raw={qty})"}
    if qty_rounded != qty:
        log.info(f"[binance] qty arredondado {sym}: {qty} → {qty_rounded}")

    if leverage is not None:
        _lev = await set_leverage(sym, leverage)
        if not _lev.get("ok"):
            _lev = await set_leverage(sym, leverage)  # 1 retry
            if not _lev.get("ok"):
                # Sem alavancagem confirmada NÃO abre: a moeda pode cair no
                # default da conta (até 20x) e estourar a margem. Fail-safe.
                return {"ok": False, "error":
                        f"leverage {leverage}x não confirmada p/ {sym}: "
                        f"{_lev.get('error') or _lev.get('msg')}"}

    binance_side = side.upper()  # BUY | SELL
    binance_type = "MARKET" if order_type == "Market" else "LIMIT"
    terminal_statuses = {
        "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED",
    }

    # Uma LIMIT GTC ainda não é posição confirmada. Armar SL/TP antes do fill
    # cria estados falsos e, num fill parcial, dimensiona conditionals pela qty
    # planejada. Entradas LIMIT protegidas devem usar o helper maker/polling.
    if binance_type == "LIMIT" and any(
        level is not None for level in (stop_loss, tp1, take_profit)
    ):
        return {
            "ok": False,
            "error": (
                "LIMIT com SL/TP exige place_maker_entry_then_protect; "
                "proteção antes do fill foi bloqueada"
            ),
            "safety_state": "ENTRY_NOT_SUBMITTED",
            "entry_state": "NOT_SUBMITTED",
            "manual_intervention_required": False,
        }

    # Toda submissão recebe um client id para que timeout-com-sucesso possa ser
    # reconciliado sem repetir a ordem. O caller pode fornecer um id estável;
    # nos demais caminhos geramos um id único dentro do limite de 36 caracteres.
    if not client_order_id:
        scope = "close" if reduce_only else "entry"
        nonce = hashlib.sha1(
            f"{sym}:{binance_side}:{qty_rounded}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:10]
        client_order_id = f"cw-{scope}-{sym[:8].lower()}-{nonce}"[:36]

    params = {
        "symbol": sym,
        "side": binance_side,
        "type": binance_type,
        "quantity": qty_rounded,
    }
    if binance_type == "MARKET":
        # Precisamos da qty realmente executada para que um eventual rollback
        # sem SL feche exatamente a exposição criada por esta entrada.
        params["newOrderRespType"] = "RESULT"
    if binance_type == "LIMIT":
        if price is None:
            return {"ok": False, "error": "LIMIT exige price"}
        params["price"] = await _round_price(sym, float(price))
        params["timeInForce"] = "GTC"
    if reduce_only:
        params["reduceOnly"] = "true"
    if client_order_id:
        params["newClientOrderId"] = client_order_id

    entry_res = await _signed_request("POST", "/fapi/v1/order", params)
    if not entry_res.get("ok"):
        if _is_ambiguous_order_submission(entry_res):
            try:
                query = await get_order(sym, client_order_id=client_order_id)
            except Exception as exc:
                query = {"ok": False, "error": str(exc)}
            query_status = str(query.get("status") or "").upper()
            query_raw = query.get("raw")
            query_qty_explicit = bool(
                (
                    isinstance(query_raw, dict)
                    and "executedQty" in query_raw
                    and query_raw.get("executedQty") not in (None, "")
                )
                or (
                    not isinstance(query_raw, dict)
                    and "executed_qty" in query
                    and query.get("executed_qty") not in (None, "")
                )
            )
            try:
                query_qty = float(query.get("executed_qty") or 0.0)
                if query_qty < 0:
                    raise ValueError("executed_qty negativa")
            except (TypeError, ValueError):
                query_qty = 0.0
                query_qty_explicit = False
            query_terminal_confirmed = bool(
                query.get("ok")
                and query_status in terminal_statuses
                and (
                    query_status in {"FILLED", "REJECTED"}
                    or query_qty_explicit
                )
            )
            if query_terminal_confirmed:
                if query_status == "FILLED" and query_qty <= 0:
                    query_qty = qty_rounded
                entry_res = {
                    "ok": True,
                    "result": {
                        "orderId": query.get("order_id") or "",
                        "clientOrderId": client_order_id,
                        "status": query_status,
                        "executedQty": query_qty,
                        "avgPrice": query.get("avg_fill_price") or 0.0,
                    },
                    "raw": query_raw,
                    "reconciled_after_ambiguous_submission": True,
                }
            else:
                protection = _protection_not_requested_result()
                if stop_loss is not None and not reduce_only:
                    try:
                        # A entry pode ter sido aceita e preencher depois. Arma
                        # apenas SL closePosition; TPs e um segundo MARKET ficam
                        # proibidos enquanto a qty/status forem UNKNOWN.
                        protection = await _place_post_fill_protection(
                            sym, binance_side, query_qty or qty_rounded,
                            stop_loss=stop_loss,
                            client_order_id_prefix=client_order_id,
                        )
                    except Exception as exc:
                        protection = _protection_exception_result(exc)
                _fire_telegram(
                    f"🚨 {sym}: submissão {binance_type} ficou UNKNOWN. Não repetir "
                    "a ordem até reconciliar pelo clientOrderId/posição.",
                    "order_submission_unknown",
                )
                return {
                    **entry_res,
                    "ok": False,
                    "result": query_raw or {},
                    "sl_ok": bool(
                        protection.get("sl_ok") and protection.get("sl_order_id")
                    ),
                    "sl_order_id": protection.get("sl_order_id"),
                    "tp1_ok": False, "tp1_order_id": None, "tp1_skipped": False,
                    "tp2_ok": False, "tp2_order_id": None,
                    "safety_state": "ENTRY_SUBMISSION_UNKNOWN",
                    "entry_state": "PARTIALLY_FILLED" if query_qty > 0 else "UNKNOWN",
                    "protection_state": (
                        "STOP_CONFIRMED"
                        if protection.get("sl_ok") and protection.get("sl_order_id")
                        else "UNKNOWN"
                    ),
                    "entry_confirmed": False,
                    "position_open": True if query_qty > 0 else None,
                    "executed_qty": query_qty,
                    "manual_intervention_required": True,
                    "quarantine_required": True,
                    "emergency_close_attempted": False,
                    "emergency_close_ok": None,
                    "client_order_id": client_order_id,
                    "entry_order_query": query,
                }
        if not entry_res.get("ok"):
            return entry_res

    entry_result = entry_res.get("result") or {}
    entry_status = str(entry_result.get("status") or "").upper()
    raw_executed_qty = entry_result.get("executedQty")
    try:
        executed_qty = max(0.0, float(raw_executed_qty or 0.0))
        executed_qty_confirmed = raw_executed_qty is not None and str(raw_executed_qty) != ""
    except (TypeError, ValueError):
        executed_qty = 0.0
        executed_qty_confirmed = False

    # RESULT deveria trazer fill terminal. Se vier incompleto, reconcilia pelo
    # orderId/client id antes de dimensionar proteção. Nunca usa qty planejada
    # como se fosse fill real numa entrada MARKET.
    if binance_type == "MARKET" and (
        entry_status not in terminal_statuses or executed_qty <= 0
    ):
        order_id = str(entry_result.get("orderId") or "")
        try:
            query = await get_order(
                sym,
                order_id=order_id or None,
                client_order_id=None if order_id else client_order_id,
            )
        except Exception as exc:
            query = {"ok": False, "error": str(exc)}
        if query.get("ok"):
            entry_status = str(query.get("status") or entry_status).upper()
            try:
                executed_qty = max(
                    executed_qty, float(query.get("executed_qty") or 0.0)
                )
                query_raw = query.get("raw")
                query_qty_explicit = (
                    isinstance(query_raw, dict) and "executedQty" in query_raw
                ) or (
                    not isinstance(query_raw, dict) and "executed_qty" in query
                )
                executed_qty_confirmed = executed_qty_confirmed or query_qty_explicit
            except (TypeError, ValueError):
                pass

        terminal_qty_unknown = executed_qty <= 0 and (
            entry_status == "FILLED"
            or (
                entry_status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
                and not executed_qty_confirmed
            )
        )
        if entry_status not in terminal_statuses or terminal_qty_unknown:
            protection = _protection_not_requested_result()
            if stop_loss is not None and not reduce_only:
                try:
                    # Só o SL closePosition: protege a exposição conhecida (e um
                    # eventual fill tardio) sem criar TPs de qty incerta. Quando
                    # o status sugere fill mas a qty sumiu, usa a qty planejada
                    # apenas como fallback quantity; closePosition continua sendo
                    # a primeira opção e reduceOnly impede inversão.
                    protection = await _place_post_fill_protection(
                        sym, binance_side, executed_qty or qty_rounded,
                        stop_loss=stop_loss,
                        client_order_id_prefix=client_order_id,
                    )
                except Exception as exc:
                    protection = _protection_exception_result(exc)
            _fire_telegram(
                f"🚨 {sym}: status/fill da entrada MARKET não pôde ser confirmado. "
                "Novas entradas devem permanecer pausadas até reconciliação.",
                "entry_confirmation_unknown",
            )
            return {
                "ok": False,
                "error": (
                    "CRÍTICO: entrada aceita, mas status/fill terminal não confirmado"
                ),
                "result": entry_result,
                "raw": entry_res.get("raw"),
                "sl_ok": protection["sl_ok"],
                "sl_order_id": protection["sl_order_id"],
                "tp1_ok": False, "tp1_order_id": None, "tp1_skipped": False,
                "tp2_ok": False, "tp2_order_id": None,
                "executed_qty": executed_qty,
                "safety_state": "ENTRY_CONFIRMATION_UNKNOWN",
                "entry_state": "PARTIALLY_FILLED" if executed_qty > 0 else "UNKNOWN",
                "protection_state": (
                    "STOP_CONFIRMED" if protection.get("sl_ok") and protection.get("sl_order_id")
                    else "UNKNOWN"
                ),
                "cleanup_state": "NOT_APPLICABLE",
                "entry_confirmed": False,
                "position_open": True if executed_qty > 0 else None,
                "manual_intervention_required": True,
                "quarantine_required": True,
                "emergency_close_attempted": False,
                "emergency_close_ok": None,
                "entry_order_query": query,
            }

    if binance_type == "MARKET" and executed_qty <= 0:
        return {
            "ok": False,
            "error": f"entrada MARKET terminou {entry_status or 'N/D'} sem fill",
            "result": entry_result,
            "raw": entry_res.get("raw"),
            "no_fill": True,
            "executed_qty": 0.0,
            "safety_state": "ENTRY_NOT_FILLED",
            "entry_state": "NOT_FILLED",
            "manual_intervention_required": False,
        }

    if binance_type == "MARKET":
        executed_qty = await _round_qty(sym, executed_qty)
        if executed_qty <= 0:
            return {
                "ok": False,
                "error": "fill MARKET arredondou para zero; proteção bloqueada",
                "result": entry_result,
                "raw": entry_res.get("raw"),
                "executed_qty": 0.0,
                "safety_state": "ENTRY_FILL_DUST_UNKNOWN",
                "entry_state": "UNKNOWN",
                "manual_intervention_required": True,
                "quarantine_required": True,
            }
    else:
        # LIMIT sem proteção é apenas submissão de ordem, não posição aberta.
        executed_qty = max(0.0, executed_qty)

    # ── Ordens de proteção (SL + TP1 parcial + TP2) ─────────────────────
    protection = _protection_not_requested_result()
    if not reduce_only and binance_type == "MARKET":
        try:
            protection = await _place_post_fill_protection(
                sym, binance_side, executed_qty,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=take_profit,
                tp1_qty_pct=tp1_qty_pct,
                client_order_id_prefix=client_order_id,
            )
        except Exception as exc:
            protection = _protection_exception_result(exc)
    resolved_entry_state = (
        "PARTIALLY_FILLED"
        if binance_type == "MARKET" and entry_status != "FILLED" and executed_qty > 0
        else "FILLED"
    )
    safety = await _enforce_entry_safety(
        sym, binance_side, executed_qty, stop_loss, protection,
        client_order_id_prefix=client_order_id,
        entry_state=resolved_entry_state,
        operation_kind=(
            "REDUCE_ONLY" if reduce_only
            else "ORDER_SUBMISSION" if binance_type == "LIMIT"
            else "ENTRY"
        ),
    )
    if reduce_only and binance_type == "MARKET":
        live_size, live_msg = await _fresh_position_size(sym)
        if live_size is None or live_size > 1e-12:
            safety = {
                "safety_state": "CLOSE_PARTIAL_OR_UNKNOWN",
                "entry_state": "NOT_APPLICABLE",
                "protection_state": "NOT_REQUESTED",
                "cleanup_state": "NOT_APPLICABLE",
                "entry_confirmed": None,
                "position_open": True if live_size is not None else None,
                "remaining_qty": (
                    live_size if live_size is not None
                    else max(0.0, qty_rounded - executed_qty)
                ),
                "close_verification": live_msg,
                "emergency_close_attempted": False,
                "emergency_close_ok": None,
                "manual_intervention_required": live_size is None,
                "quarantine_required": False,
            }

    # Backward-compat: monta `extras` no mesmo shape antigo
    extras = []
    if stop_loss is not None:
        extras.append({"stop_loss": {"ok": protection["sl_ok"], "order_id": protection["sl_order_id"], "msg": protection["sl_msg"]}})
    if tp1 is not None:
        extras.append({"tp1": {"ok": protection["tp1_ok"], "order_id": protection["tp1_order_id"], "qty": protection["tp1_qty"], "msg": protection["tp1_msg"], "skipped": protection["tp1_skipped"]}})
    if take_profit is not None:
        extras.append({"take_profit": {"ok": protection["tp2_ok"], "order_id": protection["tp2_order_id"], "msg": protection["tp2_msg"]}})

    trade_survived = safety["safety_state"] in {
        "PROTECTION_CONFIRMED",
        "STOP_CONFIRMED_TP_DEGRADED",
        "ENTRY_CONFIRMED_NO_SL_REQUESTED",
        "NOT_APPLICABLE",
    }
    return {
        # Uma entrada revertida por segurança não é uma operação aberta com
        # sucesso; callers devem registrar o incidente, não um RealTrade vivo.
        "ok": trade_survived,
        "error": None if trade_survived else (
            "fechamento reduce-only parcial ou não confirmado"
            if safety.get("safety_state") == "CLOSE_PARTIAL_OR_UNKNOWN"
            else "posição zerada, mas limpeza de conditionals não foi confirmada"
            if safety.get("safety_state") == "POSITION_FLAT_CONDITIONALS_UNKNOWN"
            else "SL não confirmado; entrada revertida por segurança"
            if safety.get("emergency_close_ok")
            else "CRÍTICO: SL e fechamento emergencial não confirmados"
        ),
        "result": entry_res["result"],
        "extras": extras,
        "raw": entry_res.get("raw"),
        # Novos campos pro caller decidir o que fazer
        "sl_ok": protection["sl_ok"],
        "sl_order_id": protection["sl_order_id"],
        "tp1_ok": protection["tp1_ok"],
        "tp1_order_id": protection["tp1_order_id"],
        "tp1_skipped": protection["tp1_skipped"],
        "tp2_ok": protection["tp2_ok"],
        "tp2_order_id": protection["tp2_order_id"],
        "executed_qty": executed_qty,
        **safety,
    }


async def get_order(symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> dict:
    """Consulta UMA ordem (GET /fapi/v1/order) — barato e preciso pra polling de
    fill (não puxa allOrders inteiro). Retorna status/avgPrice/executedQty."""
    sym = to_binance(symbol) if "/" in symbol else symbol
    params = {"symbol": sym}
    if order_id:
        params["orderId"] = order_id
    elif client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        return {"ok": False, "error": "informe order_id ou client_order_id"}
    res = await _signed_request("GET", "/fapi/v1/order", params)
    if not res.get("ok"):
        return res
    o = res["result"] or {}
    return {
        "ok": True,
        "order_id": str(o.get("orderId") or ""),
        "client_order_id": o.get("clientOrderId"),
        "status": o.get("status"),               # NEW|PARTIALLY_FILLED|FILLED|CANCELED|EXPIRED|REJECTED
        "orig_qty": float(o.get("origQty") or 0),
        "executed_qty": float(o.get("executedQty") or 0),
        "avg_fill_price": float(o.get("avgPrice") or 0),
        "price": float(o.get("price") or 0),
        "type": o.get("type"),
        "side": o.get("side"),
        "raw": o,
    }


# ── Entrada MAKER (post-only) com proteção desacoplada (#4) ──────────────────
# Defaults seguros lidos do ambiente. Tudo OFF/curto por default — quem liga é o
# caller (shadow_trade_service) atrás de MAKER_ENTRY_ENABLED. Mainnet dinheiro
# real: prefira fail-safe (fallback a market) a ficar pendurado sem entrada.
_MAKER_POLL_TIMEOUT_S = float(os.getenv("MAKER_ENTRY_TIMEOUT_S", "8"))
_MAKER_POLL_INTERVAL_S = float(os.getenv("MAKER_ENTRY_POLL_INTERVAL_S", "0.8"))


async def place_maker_entry_then_protect(
    symbol: str,
    side: str,                 # "Buy" | "Sell"
    qty: float,
    *,
    limit_price: float,        # preço LIMIT post-only (caller calcula do book: bid p/ long, ask p/ short)
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,   # = TP2
    tp1: Optional[float] = None,
    tp1_qty_pct: float = 0.45,
    leverage: Optional[int] = None,
    client_order_id: Optional[str] = None,
    poll_timeout_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
    fallback_market: bool = True,
) -> dict:
    """
    Entrada MAKER: coloca LIMIT post-only (timeInForce=GTX), faz polling do fill
    até `poll_timeout_s`, e SÓ DEPOIS coloca a proteção (SL/TP1/TP2) sobre a qty
    REALMENTE preenchida. Desacopla entrada de proteção (≠ place_order, que assume
    posição cheia já na emissão).

    Comportamentos:
      - GTX rejeitado de imediato (cruzaria o book → vira taker): se fallback_market,
        entra a MARKET; senão devolve {ok:False, "no_fill":True}.
      - Timeout SEM fill (status NEW): cancela; recheck anti-corrida; se realmente
        não preencheu e fallback_market → MARKET; senão {ok:False, "no_fill":True}.
      - Timeout com fill PARCIAL: cancela o restante; protege a parte preenchida
        (não completa a market — fill parcial maker já abriu posição válida).
      - Fill total: protege a qty cheia.

    Mantém o CONTRATO de place_order (mesmos campos sl_ok/tp1_ok/tp2_ok/etc.) pra
    o guard cardinal "sem stop = sem trade" do caller funcionar igual. Campos extra:
      was_maker (bool), fell_back_to_market (bool), entry_fill_price,
      executed_qty, no_fill (bool).
    """
    sym = to_binance(symbol) if "/" in symbol else symbol
    poll_timeout_s = _MAKER_POLL_TIMEOUT_S if poll_timeout_s is None else poll_timeout_s
    poll_interval_s = _MAKER_POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s

    qty_rounded = await _round_qty(sym, float(qty))
    if qty_rounded <= 0:
        f = await _get_symbol_filters(sym)
        return {"ok": False, "error": f"qty arredondado virou 0 (step={f.get('step')}, min={f.get('min_qty')}, raw={qty})"}

    if leverage is not None:
        _lev = await set_leverage(sym, leverage)
        if not _lev.get("ok"):
            _lev = await set_leverage(sym, leverage)  # 1 retry
            if not _lev.get("ok"):
                # Sem alavancagem confirmada NÃO abre (fail-safe, ver place_order).
                return {"ok": False, "error":
                        f"leverage {leverage}x não confirmada p/ {sym}: "
                        f"{_lev.get('error') or _lev.get('msg')}"}

    binance_side = side.upper()  # BUY | SELL

    async def _market_fallback(reason: str) -> dict:
        """Cai pra entrada MARKET reaproveitando place_order (entry+proteção juntos)."""
        log.warning(f"[maker] {sym} fallback MARKET ({reason})")
        res = await place_order(
            sym, side, qty_rounded, order_type="Market",
            stop_loss=stop_loss, take_profit=take_profit, tp1=tp1,
            tp1_qty_pct=tp1_qty_pct, leverage=None,  # leverage já setado acima
            client_order_id=client_order_id,
        )
        if isinstance(res, dict):
            res["was_maker"] = False
            res["fell_back_to_market"] = True
        return res

    async def _protect(
        executed_qty: float,
        fill_price: float,
        *,
        was_maker: bool,
        entry_order_id: str = "",
        entry_state: str = "FILLED",
    ) -> dict:
        """Coloca proteção sobre a posição já aberta e monta o retorno no shape de place_order."""
        try:
            prot = await _place_post_fill_protection(
                sym, binance_side, executed_qty,
                stop_loss=stop_loss, tp1=tp1, tp2=take_profit,
                tp1_qty_pct=tp1_qty_pct,
                client_order_id_prefix=client_order_id,
                dedup_live=True,   # entrada lenta → outro caminho pode já ter protegido; não duplica
            )
        except Exception as exc:
            prot = _protection_exception_result(exc)
        safety = await _enforce_entry_safety(
            sym, binance_side, executed_qty, stop_loss, prot,
            client_order_id_prefix=client_order_id,
            entry_state=entry_state,
        )
        extras = []
        if stop_loss is not None:
            extras.append({"stop_loss": {"ok": prot["sl_ok"], "order_id": prot["sl_order_id"], "msg": prot["sl_msg"]}})
        if tp1 is not None:
            extras.append({"tp1": {"ok": prot["tp1_ok"], "order_id": prot["tp1_order_id"], "qty": prot["tp1_qty"], "msg": prot["tp1_msg"], "skipped": prot["tp1_skipped"]}})
        if take_profit is not None:
            extras.append({"take_profit": {"ok": prot["tp2_ok"], "order_id": prot["tp2_order_id"], "msg": prot["tp2_msg"]}})
        trade_survived = safety["safety_state"] in {
            "PROTECTION_CONFIRMED",
            "STOP_CONFIRMED_TP_DEGRADED",
            "ENTRY_CONFIRMED_NO_SL_REQUESTED",
        }
        return {
            "ok": trade_survived,
            "error": None if trade_survived else (
                "SL não confirmado; entrada maker revertida por segurança"
                if safety.get("emergency_close_ok")
                else "CRÍTICO: SL e fechamento emergencial maker não confirmados"
            ),
            "result": {"orderId": entry_order_id, "avgPrice": fill_price, "executedQty": executed_qty, "symbol": sym, "side": binance_side},
            "extras": extras,
            "sl_ok": prot["sl_ok"], "sl_order_id": prot["sl_order_id"],
            "tp1_ok": prot["tp1_ok"], "tp1_order_id": prot["tp1_order_id"], "tp1_skipped": prot["tp1_skipped"],
            "tp2_ok": prot["tp2_ok"], "tp2_order_id": prot["tp2_order_id"],
            "was_maker": was_maker, "fell_back_to_market": False,
            "entry_fill_price": fill_price, "executed_qty": executed_qty,
            **safety,
        }

    # ── 1. Coloca LIMIT post-only (GTX) ──────────────────────────────────────
    px = await _round_price(sym, float(limit_price))
    params = {
        "symbol": sym, "side": binance_side, "type": "LIMIT",
        "quantity": qty_rounded, "price": px, "timeInForce": "GTX",  # GTX = Post Only
    }
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    entry_res = await _signed_request("POST", "/fapi/v1/order", params)
    if not entry_res.get("ok"):
        # Só uma rejeição GTX explícita prova que a LIMIT NÃO foi aceita. Timeout
        # ou erro de transporte pode ser sucesso sem resposta; cair para MARKET
        # nesse estado duplicaria a entrada.
        msg = entry_res.get("msg") or entry_res.get("error")
        try:
            code = int(entry_res.get("code"))
        except (TypeError, ValueError):
            code = None
        definite_post_only_rejection = (
            code == -5022
            or "post only order will be rejected" in str(msg or "").lower()
            or "would immediately match" in str(msg or "").lower()
        )
        if fallback_market and definite_post_only_rejection:
            return await _market_fallback(f"GTX rejeitado: {msg}")
        if _is_ambiguous_order_submission(entry_res):
            _fire_telegram(
                f"🚨 {sym}: emissão da entrada maker ficou UNKNOWN. "
                "Não houve fallback MARKET; confira/cancele a ordem manualmente.",
                "maker_submission_unknown",
            )
            return {
                "ok": False,
                "error": f"emissão GTX incerta: {msg}",
                "no_fill": False,
                "was_maker": True,
                "fell_back_to_market": False,
                "safety_state": "ENTRY_SUBMISSION_UNKNOWN",
                "entry_state": "UNKNOWN",
                "entry_confirmed": False,
                "position_open": None,
                "manual_intervention_required": True,
                "quarantine_required": True,
                "emergency_close_attempted": False,
                "emergency_close_ok": None,
                "client_order_id": client_order_id,
            }
        return {"ok": False, "error": f"GTX rejeitado: {msg}", "no_fill": True, "was_maker": True, "fell_back_to_market": False}

    order = entry_res["result"] or {}
    order_id = str(order.get("orderId") or "")
    # Alguns retornos já trazem status FILLED (preencheu na emissão).
    if (order.get("status") or "").upper() == "FILLED":
        ex_qty = await _round_qty(sym, float(order.get("executedQty") or qty_rounded))
        fill_px = float(order.get("avgPrice") or px)
        log.info(f"[maker] {sym} GTX preencheu na emissão @ {fill_px} qty={ex_qty}")
        return await _protect(ex_qty, fill_px, was_maker=True, entry_order_id=order_id)

    # ── 2. Polling do fill ───────────────────────────────────────────────────
    terminal_statuses = {
        "CANCELED", "FILLED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED",
    }
    log.info(f"[maker] {sym} GTX vivo orderId={order_id} @ {px} qty={qty_rounded} — aguardando fill até {poll_timeout_s}s")
    deadline = time.time() + poll_timeout_s
    initial_executed_raw = order.get("executedQty")
    initial_qty_confirmed = bool(
        "executedQty" in order and initial_executed_raw not in (None, "")
    )
    try:
        initial_executed = float(initial_executed_raw or 0.0)
        if initial_executed < 0:
            raise ValueError("executedQty negativa")
    except (TypeError, ValueError):
        initial_executed = 0.0
        initial_qty_confirmed = False
    try:
        initial_avg = max(0.0, float(order.get("avgPrice") or 0.0))
    except (TypeError, ValueError):
        initial_avg = 0.0
    # A resposta do próprio POST pode já confirmar fill parcial. Preservar esse
    # mínimo conhecido mesmo se polling/cancelamento ficarem indisponíveis.
    initial_status = str(order.get("status") or "NEW").upper()
    last = {
        "ok": True,
        "status": initial_status,
        "executed_qty": initial_executed,
        "avg_fill_price": initial_avg,
        "executed_qty_confirmed": initial_qty_confirmed,
        "terminal_status_confirmed": initial_status in terminal_statuses,
        "terminal_qty_confirmed": bool(
            initial_status == "FILLED"
            or (initial_status == "REJECTED" and initial_executed <= 1e-12)
            or (initial_status in terminal_statuses and initial_qty_confirmed)
        ),
    }

    def _maker_fill_qty_known(row: dict) -> bool:
        raw = row.get("raw")
        if row.get("executed_qty_confirmed"):
            return True
        if isinstance(raw, dict):
            if "executedQty" not in raw or raw.get("executedQty") in (None, ""):
                return False
            value = raw.get("executedQty")
        else:
            if "executed_qty" not in row or row.get("executed_qty") in (None, ""):
                return False
            value = row.get("executed_qty")
        try:
            return float(value) >= 0
        except (TypeError, ValueError):
            return False

    def _merge_maker_observation(previous: dict, current: dict) -> dict:
        merged = dict(current)
        try:
            previous_qty = max(0.0, float(previous.get("executed_qty") or 0.0))
        except (TypeError, ValueError):
            previous_qty = 0.0
        try:
            current_qty = max(0.0, float(current.get("executed_qty") or 0.0))
        except (TypeError, ValueError):
            current_qty = 0.0
        current_status = str(current.get("status") or "").upper()
        current_qty_confirmed = _maker_fill_qty_known(current)
        if current_status == "FILLED":
            # FILLED prova execução integral mesmo se o payload omitir qty.
            current_qty = max(current_qty, float(qty_rounded))
        merged["executed_qty"] = max(previous_qty, current_qty)
        merged["avg_fill_price"] = (
            current.get("avg_fill_price") or previous.get("avg_fill_price") or 0.0
        )
        merged["executed_qty_confirmed"] = (
            _maker_fill_qty_known(previous) or _maker_fill_qty_known(current)
            or (
                not isinstance(current.get("raw"), dict)
                and "executed_qty" in current
            )
        )
        merged["terminal_status_confirmed"] = current_status in terminal_statuses
        # A qty observada ANTES do terminal é apenas um lower-bound. CANCELED /
        # EXPIRED só fecham o snapshot final quando a MESMA observação terminal
        # traz a qty cumulativa e ela não regride o fill já visto.
        merged["terminal_qty_confirmed"] = bool(
            current_status == "FILLED"
            or (
                current_status == "REJECTED"
                and previous_qty <= 1e-12
                and current_qty <= 1e-12
            )
            or (
                current_status in terminal_statuses
                and current_qty_confirmed
                and current_qty + 1e-12 >= previous_qty
            )
        )
        return merged
    if initial_executed > 0:
        log.warning(
            f"[maker] {sym} fill parcial já confirmado no POST qty={initial_executed}; "
            "cancela o restante imediatamente antes de proteger"
        )
    while initial_executed <= 0 and time.time() < deadline:
        await asyncio.sleep(poll_interval_s)
        q = await get_order(sym, order_id=order_id)
        if not q.get("ok"):
            continue  # leitura incerta → tenta de novo até o deadline
        last = _merge_maker_observation(last, q)
        st = (q.get("status") or "").upper()
        if st == "FILLED":
            ex_qty = await _round_qty(sym, q.get("executed_qty") or qty_rounded)
            fill_px = q.get("avg_fill_price") or px
            log.info(f"[maker] {sym} fill total @ {fill_px} qty={ex_qty}")
            return await _protect(ex_qty, fill_px, was_maker=True, entry_order_id=order_id)
        if float(last.get("executed_qty") or 0.0) > 0:
            log.warning(
                f"[maker] {sym} fill parcial detectado qty={last.get('executed_qty')}; "
                "interrompe polling e cancela o restante para armar SL"
            )
            break
        if st in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
            log.warning(f"[maker] {sym} ordem sumiu (status={st}) durante polling")
            break

    # ── 3. Timeout / ordem sumiu — cancela o que restou e CONFIRMA terminal ──
    last_status = str(last.get("status") or "").upper()
    terminal_observation = last if last_status in terminal_statuses else {}
    chk = last if terminal_observation and last.get("terminal_qty_confirmed") else {}
    cancel_res: dict = {}
    if not chk:
        for attempt in range(1, _MAKER_CANCEL_CONFIRM_ATTEMPTS + 1):
            try:
                cancel_res = await cancel_order(sym, order_id=order_id)
            except Exception as exc:
                cancel_res = {"ok": False, "error": str(exc)}
            try:
                queried = await get_order(sym, order_id=order_id)
            except Exception as exc:
                queried = {"ok": False, "error": str(exc)}
            if queried.get("ok"):
                last = _merge_maker_observation(last, queried)
                queried_status = str(last.get("status") or "").upper()
                if queried_status in terminal_statuses:
                    terminal_observation = last
                    if last.get("terminal_qty_confirmed"):
                        chk = last
            # DELETE pode confirmar o estado terminal mesmo que o GET seguinte
            # esteja temporariamente indisponível.
            cancel_row = cancel_res.get("result") or {}
            cancel_status = str(cancel_row.get("status") or "").upper()
            if cancel_res.get("ok") and cancel_status in terminal_statuses:
                cancel_executed_raw = cancel_row.get("executedQty")
                cancel_qty_confirmed = bool(
                    "executedQty" in cancel_row
                    and cancel_executed_raw not in (None, "")
                )
                try:
                    cancel_executed = float(cancel_executed_raw or 0.0)
                    if cancel_executed < 0:
                        raise ValueError("executedQty negativa")
                except (TypeError, ValueError):
                    cancel_executed = 0.0
                    cancel_qty_confirmed = False
                cancel_obs = {
                    "ok": True,
                    "status": cancel_status,
                    "executed_qty": cancel_executed,
                    "avg_fill_price": cancel_row.get("avgPrice") or 0,
                    "executed_qty_confirmed": cancel_qty_confirmed,
                }
                cancel_checked = _merge_maker_observation(last, cancel_obs)
                terminal_observation = cancel_checked
                last = cancel_checked
                if cancel_checked.get("terminal_qty_confirmed"):
                    chk = cancel_checked
            # Sempre combina GET e DELETE da mesma tentativa antes de decidir:
            # o DELETE pode carregar uma qty cumulativa mais nova que o GET.
            if chk:
                break
            if attempt < _MAKER_CANCEL_CONFIRM_ATTEMPTS:
                await asyncio.sleep(_MAKER_CANCEL_CONFIRM_DELAY * attempt)

    terminal_confirmed = bool(
        terminal_observation.get("ok")
        and str(terminal_observation.get("status") or "").upper() in terminal_statuses
    )
    final_fill_qty_confirmed = bool(
        chk.get("ok")
        and str(chk.get("status") or "").upper() in terminal_statuses
        and chk.get("terminal_qty_confirmed")
    )
    try:
        ex_qty = max(
            float(last.get("executed_qty") or 0),
            float(chk.get("executed_qty") or 0),
            float(terminal_observation.get("executed_qty") or 0),
        )
    except (TypeError, ValueError):
        ex_qty = float(last.get("executed_qty") or 0)
    fill_px = (
        chk.get("avg_fill_price")
        or terminal_observation.get("avg_fill_price")
        or last.get("avg_fill_price")
        or px
    )

    if not terminal_confirmed:
        # Não protege+flattena como se o restante estivesse cancelado: a LIMIT
        # pode preencher depois. Se há fill conhecido, arma apenas SL
        # closePosition para reduzir o risco enquanto o latch bloqueia entradas.
        # Mesmo sem fill observado, tenta o SL com a qty planejada como fallback:
        # a LIMIT ainda viva pode preencher depois da última leitura.
        ex_qty_r = await _round_qty(sym, ex_qty) if ex_qty > 0 else 0.0
        prot = _protection_not_requested_result()
        if stop_loss is not None:
            try:
                prot = await _place_post_fill_protection(
                    sym, binance_side, ex_qty_r or qty_rounded,
                    stop_loss=stop_loss,
                    client_order_id_prefix=client_order_id,
                    dedup_live=True,
                )
            except Exception as exc:
                prot = _protection_exception_result(exc)
        _fire_telegram(
            f"🚨 {sym}: cancelamento da entrada maker não foi confirmado. "
            "A ordem pode continuar viva; novas entradas devem ficar pausadas.",
            "maker_cancel_unknown",
        )
        return {
            "ok": False,
            "error": "CRÍTICO: ordem maker pode continuar ativa após cancelamento incerto",
            "result": {
                "orderId": order_id,
                "avgPrice": fill_px,
                "executedQty": ex_qty_r,
                "symbol": sym,
                "side": binance_side,
            },
            "sl_ok": bool(prot.get("sl_ok") and prot.get("sl_order_id")),
            "sl_order_id": prot.get("sl_order_id"),
            "tp1_ok": False, "tp1_order_id": None, "tp1_skipped": False,
            "tp2_ok": False, "tp2_order_id": None,
            "was_maker": True, "fell_back_to_market": False,
            "no_fill": False,
            "entry_fill_price": fill_px,
            "executed_qty": ex_qty_r,
            "entry_order_status": str(last.get("status") or "UNKNOWN").upper(),
            "entry_order_terminal": False,
            "pending_entry_order": True,
            "safety_state": "ENTRY_ORDER_STILL_ACTIVE_OR_UNKNOWN",
            "entry_state": "PARTIALLY_FILLED" if ex_qty_r > 0 else "UNKNOWN",
            "protection_state": (
                "STOP_CONFIRMED" if prot.get("sl_ok") and prot.get("sl_order_id")
                else "UNKNOWN"
            ),
            "cleanup_state": "NOT_APPLICABLE",
            "entry_confirmed": False,
            "position_open": True if ex_qty_r > 0 else None,
            "manual_intervention_required": True,
            "quarantine_required": True,
            "emergency_close_attempted": False,
            "emergency_close_ok": None,
            "maker_cancel_result": cancel_res,
            "maker_order_check": terminal_observation or chk,
        }

    if not final_fill_qty_confirmed:
        # A LIMIT acabou, portanto não há late-fill, mas a quantidade cumulativa
        # final não foi provada. Um fill visto antes do cancel é só lower-bound:
        # não dimensiona TP nem rollback por ele. Mantém apenas SL closePosition
        # e exige reconciliação humana da posição final.
        ex_qty_r = await _round_qty(sym, ex_qty) if ex_qty > 0 else 0.0
        prot = _protection_not_requested_result()
        if stop_loss is not None:
            try:
                prot = await _place_post_fill_protection(
                    sym, binance_side, qty_rounded,
                    stop_loss=stop_loss,
                    client_order_id_prefix=client_order_id,
                    dedup_live=True,
                )
            except Exception as exc:
                prot = _protection_exception_result(exc)
        terminal_status = str(
            terminal_observation.get("status") or "UNKNOWN"
        ).upper()
        _fire_telegram(
            f"🚨 {sym}: entrada maker terminou em {terminal_status}, mas a qty final "
            "não foi confirmada. SL total foi tentado; reconcilie a posição manualmente.",
            "maker_final_qty_unknown",
        )
        return {
            "ok": False,
            "error": "CRÍTICO: qty final da entrada maker não confirmada",
            "result": {
                "orderId": order_id,
                "avgPrice": fill_px,
                "executedQty": ex_qty_r,
                "symbol": sym,
                "side": binance_side,
            },
            "sl_ok": bool(prot.get("sl_ok") and prot.get("sl_order_id")),
            "sl_order_id": prot.get("sl_order_id"),
            "tp1_ok": False, "tp1_order_id": None, "tp1_skipped": False,
            "tp2_ok": False, "tp2_order_id": None,
            "was_maker": True, "fell_back_to_market": False,
            "no_fill": False,
            "entry_fill_price": fill_px,
            "executed_qty": ex_qty_r,
            "executed_qty_is_lower_bound": True,
            "final_fill_qty_unknown": True,
            "entry_order_status": terminal_status,
            "entry_order_terminal": True,
            "pending_entry_order": False,
            "safety_state": "FINAL_FILL_QTY_UNKNOWN",
            "entry_state": "PARTIALLY_FILLED" if ex_qty_r > 0 else "UNKNOWN",
            "protection_state": (
                "STOP_CONFIRMED" if prot.get("sl_ok") and prot.get("sl_order_id")
                else "UNKNOWN"
            ),
            "cleanup_state": "NOT_APPLICABLE",
            "entry_confirmed": False,
            "position_open": True if ex_qty_r > 0 else None,
            "manual_intervention_required": True,
            "quarantine_required": True,
            "emergency_close_attempted": False,
            "emergency_close_ok": None,
            "maker_cancel_result": cancel_res,
            "maker_order_check": terminal_observation,
        }

    ex_qty_r = await _round_qty(sym, ex_qty) if ex_qty > 0 else 0.0
    if ex_qty_r > 0:
        # Fill PARCIAL (ou total que chegou junto do cancel) → protege o preenchido.
        log.info(f"[maker] {sym} fill parcial {ex_qty_r}/{qty_rounded} @ {fill_px} — protegendo o preenchido (resto cancelado)")
        return await _protect(
            ex_qty_r,
            fill_px,
            was_maker=True,
            entry_order_id=order_id,
            entry_state=(
                "FILLED" if ex_qty_r >= qty_rounded else "PARTIALLY_FILLED"
            ),
        )

    # Sem fill nenhum.
    if fallback_market:
        return await _market_fallback("timeout sem fill")
    log.info(f"[maker] {sym} timeout sem fill e sem fallback — desiste da entrada")
    return {"ok": False, "error": "maker timeout sem fill", "no_fill": True, "was_maker": True, "fell_back_to_market": False}


async def cancel_order(symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> dict:
    sym = to_binance(symbol) if "/" in symbol else symbol
    params = {"symbol": sym}
    if order_id:
        params["orderId"] = order_id
    elif client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        return {"ok": False, "error": "informe order_id ou client_order_id"}
    return await _signed_request("DELETE", "/fapi/v1/order", params)


async def cancel_algo_order(algo_id: str) -> dict:
    """Cancela uma ordem CONDITIONAL (SL/TP) criada via /fapi/v1/algoOrder."""
    if not algo_id:
        return {"ok": False, "error": "algo_id vazio"}
    return await _signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})


async def get_open_algo_orders(symbol: Optional[str] = None) -> dict:
    """
    Lista as ordens CONDITIONAL (SL/TP) ABERTAS na corretora via
    GET /fapi/v1/openAlgoOrders. `symbol` opcional (omitido = todos).

    Usado pelo trade_manager pra VERIFICAR que SL/TP2 estão realmente vivos
    na Binance — não só não-nulos no DB. Pega ordens que foram criadas e
    depois sumiram (canceladas/expiradas/disparadas externamente).

    Retorno:
      {"ok": True, "orders": [{algo_id, client_algo_id, symbol, side, type,
       trigger_price, quantity, close_position, reduce_only, status,
       working_type}], "count": int}
    Em falha: {"ok": False, ...} — o caller DEVE tratar como "incerto" e
    NÃO recriar ordens (fail-safe contra duplicação).
    """
    params = {}
    if symbol:
        params["symbol"] = to_binance(symbol) if "/" in symbol else symbol
    res = await _signed_request("GET", "/fapi/v1/openAlgoOrders", params or None)
    if not res.get("ok"):
        return res
    rows = res["result"] or []

    def _api_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    orders = [
        {
            "algo_id": str(o.get("algoId") or ""),
            "client_algo_id": o.get("clientAlgoId"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "type": o.get("orderType") or o.get("algoType"),
            "trigger_price": float(o.get("triggerPrice") or 0),
            "quantity": float(o.get("quantity") or 0),
            "close_position": _api_bool(o.get("closePosition")),
            "reduce_only": _api_bool(o.get("reduceOnly")),
            "status": o.get("algoStatus"),
            "working_type": o.get("workingType"),
        }
        for o in rows
    ]
    return {"ok": True, "orders": orders, "count": len(orders)}


async def set_leverage(symbol: str, leverage: int) -> dict:
    res = await _signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol, "leverage": leverage,
    })
    return res


async def get_order_history(symbol: Optional[str] = None, limit: int = 50) -> dict:
    if not symbol:
        return {"ok": False, "error": "Binance allOrders exige symbol"}
    sym = to_binance(symbol) if "/" in symbol else symbol
    res = await _signed_request("GET", "/fapi/v1/allOrders", {"symbol": sym, "limit": limit})
    if not res.get("ok"):
        return res
    rows = res["result"] or []
    orders = [
        {
            "order_id": str(o.get("orderId")),
            "client_order_id": o.get("clientOrderId"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "order_type": o.get("type"),
            "qty": float(o.get("origQty") or 0),
            "price": float(o.get("price") or 0),
            "avg_fill_price": float(o.get("avgPrice") or 0),
            "status": o.get("status"),
            "created_at": str(o.get("time")),
            "updated_at": str(o.get("updateTime")),
        }
        for o in rows
    ]
    return {"ok": True, "orders": orders, "count": len(orders)}


async def get_executions(symbol: Optional[str] = None, limit: int = 50) -> dict:
    if not symbol:
        return {"ok": False, "error": "Binance userTrades exige symbol"}
    sym = to_binance(symbol) if "/" in symbol else symbol
    res = await _signed_request("GET", "/fapi/v1/userTrades", {"symbol": sym, "limit": limit})
    if not res.get("ok"):
        return res
    rows = res["result"] or []
    fills = [
        {
            "exec_id": str(e.get("id")),
            "order_id": str(e.get("orderId")),
            "symbol": e.get("symbol"),
            "side": e.get("side"),
            "qty": float(e.get("qty") or 0),
            "price": float(e.get("price") or 0),
            "fee": float(e.get("commission") or 0),
            "is_maker": e.get("maker"),
            "time": str(e.get("time")),
        }
        for e in rows
    ]
    return {"ok": True, "fills": fills, "count": len(fills)}


async def close_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ─── Diagnostic (debug auth issues) ────────────────────────────────────────────


async def diagnostic() -> dict:
    """
    Diagnóstico verboso pra debug de auth Binance — não vaza secret.
    Inclui lengths e SHA1 de key/secret pra comparar bit-a-bit com painel.
    Bybit keys são 18/36 chars; Binance Futures testnet keys são 64/64 chars.
    """
    if not is_configured():
        return {"ok": False, "error": "BINANCE_API_KEY/SECRET não configurados"}
    key_has_nonascii = any(ord(c) > 127 or ord(c) < 32 for c in _API_KEY)
    secret_has_nonascii = any(ord(c) > 127 or ord(c) < 32 for c in _API_SECRET)
    key_sha1 = hashlib.sha1(_API_KEY.encode("utf-8")).hexdigest()[:12]
    secret_sha1 = hashlib.sha1(_API_SECRET.encode("utf-8")).hexdigest()[:12]
    out = {
        "exchange": "binance",
        "mode": _MODE,
        "base_url": BASE,
        "testnet": _TESTNET,
        "key_prefix": _API_KEY[:4] + "...",
        "key_len": len(_API_KEY),
        "secret_len": len(_API_SECRET),
        "key_has_nonascii": key_has_nonascii,
        "secret_has_nonascii": secret_has_nonascii,
        "key_sha1_12": key_sha1,
        "secret_sha1_12": secret_sha1,
        "_hint": "Binance Futures testnet keys = 64 chars cada. Compare local: echo -n 'X' | shasum | cut -c1-12",
        "tests": [],
    }
    # Test 1: public ping (network)
    try:
        r = await _get_client().get(f"{BASE}/fapi/v1/ping")
        out["tests"].append({"name": "public_ping", "status": r.status_code, "body": r.text[:200]})
    except Exception as e:
        out["tests"].append({"name": "public_ping", "error": str(e)})
    # Test 2: server time (clock drift)
    try:
        r = await _get_client().get(f"{BASE}/fapi/v1/time")
        try:
            data = r.json()
            server_ms = int(data.get("serverTime") or 0)
            local_ms = int(time.time() * 1000)
            drift = local_ms - server_ms
            out["tests"].append({"name": "server_time", "status": r.status_code,
                                 "server_ms": server_ms, "local_ms": local_ms, "drift_ms": drift})
        except Exception:
            out["tests"].append({"name": "server_time", "status": r.status_code, "body": r.text[:200]})
    except Exception as e:
        out["tests"].append({"name": "server_time", "error": str(e)})
    # Test 3: signed — account (auth)
    res = await _signed_request("GET", "/fapi/v2/account")
    out["tests"].append({"name": "signed_account", "ok": res.get("ok"),
                         "code": res.get("code"), "msg": res.get("msg")})
    # Test 4: signed — balance (alt endpoint, sometimes auth differs)
    res2 = await _signed_request("GET", "/fapi/v2/balance")
    out["tests"].append({"name": "signed_balance", "ok": res2.get("ok"),
                         "code": res2.get("code"), "msg": res2.get("msg")})
    return out
