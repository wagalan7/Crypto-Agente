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
from typing import Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

# ── Retry de ordens condicionais (algo orders) ──────────────────────────────
# As 3 ordens de proteção (SL/TP1/TP2) são emitidas em sequência. Falhas
# transitórias (rate-limit, timeout, indisponibilidade momentânea) deixavam a
# posição parcialmente desprotegida. Retry com backoff cobre esse buraco.
_ALGO_MAX_ATTEMPTS = int(os.getenv("BINANCE_ALGO_MAX_ATTEMPTS", "3"))
_ALGO_RETRY_BASE_DELAY = float(os.getenv("BINANCE_ALGO_RETRY_DELAY", "0.4"))  # s, escala com a tentativa

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
            until = _dt.datetime.fromtimestamp(ban_ms / 1000.0, tz=_dt.timezone.utc)
            mins = max(0.0, (ban_ms - time.time() * 1000.0) / 60000.0)
            _fire_telegram(
                f"\u26A0\uFE0F *Bot cego \u2014 rate-limit Binance*\n"
                f"used-weight estourou o teto (\u2248{_used_weight_1m}/min). A Binance "
                f"baniu o IP via `{origin}` at\u00E9 *{until.strftime('%H:%M')} UTC* "
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
        return {"ok": False, "error": "BINANCE_API_KEY/SECRET não configurados"}

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
            return {"ok": False, "error": f"resposta não-JSON ({r.status_code}): {r.text[:200]}"}
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
                   "msg": data.get("msg") if isinstance(data, dict) else r.text, "raw": data}
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
        return {"ok": False, "error": str(e)}


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
    n = int(value / step)  # floor implícito (truncamento)
    out = n * step
    # Acerta casas decimais — quantas tem o step
    # ex: step=0.001 → 3 casas; step=1 → 0 casas
    s = f"{step:.10f}".rstrip("0").rstrip(".")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return round(out, decimals)


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

    def _existing_leg_id(otype: str, trigger: float) -> str | None:
        """algoId de uma perna viva equivalente (type+side+trigger≈), ou None."""
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
                return o.get("algo_id")
        return None

    out = {
        "sl_ok": True, "sl_order_id": None, "sl_msg": None,
        "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0.0,
        "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
        "tp1_skipped": False,
    }

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

    qty_remaining = qty_total - tp1_qty if has_partial else qty_total

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
    ) -> tuple[bool, str | None, str | None]:
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
        for attempt in range(1, _ALGO_MAX_ATTEMPTS + 1):
            res = await _signed_request("POST", "/fapi/v1/algoOrder", params)
            if res.get("ok"):
                algo_id = str((res.get("result") or {}).get("algoId") or "")
                tag = f" (tentativa {attempt})" if attempt > 1 else ""
                log.info(f"[binance] {label.upper()} ok {sym} {otype} @ {trigger_price} qty={q} algoId={algo_id}{tag}")
                return True, algo_id, None
            last_msg = res.get("msg") or res.get("error")
            code = res.get("code")
            # Erro permanente (preço inválido, gatilho imediato, precisão) → não adianta repetir.
            if _is_permanent_algo_error(code, last_msg):
                log.error(
                    f"[binance] {label.upper()} FALHOU {sym} {otype} @ {trigger_price} qty={q}: "
                    f"{last_msg} (code={code}, permanente — sem retry)"
                )
                return False, None, last_msg
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
                                    aid = o.get("algo_id")
                                    log.info(
                                        f"[binance] {label.upper()} {sym} já vivo (clientAlgoId={want_cid} "
                                        f"algoId={aid}) — timeout-com-sucesso, adota sem recolocar"
                                    )
                                    return True, aid, None
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
        return False, None, last_msg

    # ── SL ───────────────────────────────────────────────────────────────
    if stop_loss is not None:
        sl_price = await _round_price(sym, float(stop_loss))
        dup_id = _existing_leg_id("STOP_MARKET", sl_price)
        if dup_id:
            log.info(f"[dedup] SL {sym} @ {sl_price} já vivo algoId={dup_id} — pula recolocação")
            out["sl_ok"] = True
            out["sl_order_id"] = dup_id
            out["sl_msg"] = "dedup: já existia"
        else:
            # closePosition=true fecha tudo (sem sobrar poeira). Se a API rejeitar
            # closePosition no algoOrder, cai pra quantity+reduceOnly (nunca fica
            # sem stop).
            ok, oid, msg = await _place_algo("STOP_MARKET", sl_price, qty_total, "sl", close_position=True)
            if not ok:
                log.warning(f"[binance] SL closePosition falhou {sym}: {msg} — fallback quantity")
                ok, oid, msg = await _place_algo("STOP_MARKET", sl_price, qty_total, "sl")
            out["sl_ok"] = ok
            out["sl_order_id"] = oid
            out["sl_msg"] = msg

    # ── TP1 parcial 45% ──────────────────────────────────────────────────
    if has_partial:
        tp1_price = await _round_price(sym, float(tp1))
        dup_id = _existing_leg_id("TAKE_PROFIT_MARKET", tp1_price)
        if dup_id:
            log.info(f"[dedup] TP1 {sym} @ {tp1_price} já vivo algoId={dup_id} — pula recolocação")
            out["tp1_ok"] = True
            out["tp1_order_id"] = dup_id
            out["tp1_msg"] = "dedup: já existia"
            out["tp1_qty"] = tp1_qty
        else:
            ok, oid, msg = await _place_algo("TAKE_PROFIT_MARKET", tp1_price, tp1_qty, "tp1")
            out["tp1_ok"] = ok
            out["tp1_order_id"] = oid
            out["tp1_msg"] = msg
            out["tp1_qty"] = tp1_qty if ok else 0.0

    # ── TP2 / TP único — fecha o restante (closePosition, sem poeira) ────
    tp_final = tp2 if tp2 is not None else tp1
    if tp_final is not None:
        tp_price = await _round_price(sym, float(tp_final))
        dup_id = _existing_leg_id("TAKE_PROFIT_MARKET", tp_price)
        if dup_id:
            log.info(f"[dedup] TP2 {sym} @ {tp_price} já vivo algoId={dup_id} — pula recolocação")
            out["tp2_ok"] = True
            out["tp2_order_id"] = dup_id
            out["tp2_msg"] = "dedup: já existia"
        else:
            ok, oid, msg = await _place_algo("TAKE_PROFIT_MARKET", tp_price, qty_remaining, "tp2", close_position=True)
            if not ok:
                log.warning(f"[binance] TP2 closePosition falhou {sym}: {msg} — fallback quantity")
                ok, oid, msg = await _place_algo("TAKE_PROFIT_MARKET", tp_price, qty_remaining, "tp2")
            out["tp2_ok"] = ok
            out["tp2_order_id"] = oid
            out["tp2_msg"] = msg

    return out


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
        await set_leverage(sym, leverage)

    binance_side = side.upper()  # BUY | SELL
    binance_type = "MARKET" if order_type == "Market" else "LIMIT"

    params = {
        "symbol": sym,
        "side": binance_side,
        "type": binance_type,
        "quantity": qty_rounded,
    }
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
        return entry_res

    # ── Ordens de proteção (SL + TP1 parcial + TP2) ─────────────────────
    protection = await place_protection_orders(
        sym, binance_side, qty_rounded,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=take_profit,
        tp1_qty_pct=tp1_qty_pct,
        client_order_id_prefix=client_order_id,
    )

    # Backward-compat: monta `extras` no mesmo shape antigo
    extras = []
    if stop_loss is not None:
        extras.append({"stop_loss": {"ok": protection["sl_ok"], "order_id": protection["sl_order_id"], "msg": protection["sl_msg"]}})
    if tp1 is not None:
        extras.append({"tp1": {"ok": protection["tp1_ok"], "order_id": protection["tp1_order_id"], "qty": protection["tp1_qty"], "msg": protection["tp1_msg"], "skipped": protection["tp1_skipped"]}})
    if take_profit is not None:
        extras.append({"take_profit": {"ok": protection["tp2_ok"], "order_id": protection["tp2_order_id"], "msg": protection["tp2_msg"]}})

    return {
        "ok": True,
        "result": entry_res["result"],
        "extras": extras,
        "raw": entry_res["raw"],
        # Novos campos pro caller decidir o que fazer
        "sl_ok": protection["sl_ok"],
        "sl_order_id": protection["sl_order_id"],
        "tp1_ok": protection["tp1_ok"],
        "tp1_order_id": protection["tp1_order_id"],
        "tp1_skipped": protection["tp1_skipped"],
        "tp2_ok": protection["tp2_ok"],
        "tp2_order_id": protection["tp2_order_id"],
    }


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
    orders = [
        {
            "algo_id": str(o.get("algoId")),
            "client_algo_id": o.get("clientAlgoId"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "type": o.get("orderType") or o.get("algoType"),
            "trigger_price": float(o.get("triggerPrice") or 0),
            "quantity": float(o.get("quantity") or 0),
            "close_position": bool(o.get("closePosition")),
            "reduce_only": bool(o.get("reduceOnly")),
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
