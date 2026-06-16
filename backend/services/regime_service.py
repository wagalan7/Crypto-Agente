"""
Macro Regime Gate — detecta condições de mercado em que setups técnicos
historicamente performam mal e bloqueia ou downgrade certas direções.

Regimes detectados:
  - RISK_OFF: BTC -5%+ 24h. Volatilidade direcional extrema, vários stops.
              → Bloqueia TODAS as novas recs.
  - ALT_DANGER: BTC dominance alta + BTC pumpando. Capital fugindo de alts
                pra BTC. Longs em alts viram sangue.
                → Bloqueia LONGS em alts (deixa BTC longs e shorts).
  - BTC_DOMINANT: Dominance > 55% e subindo. Alts laterais/baixistas.
                  → Apenas downgrade (não bloqueia) alt longs.
  - NORMAL: nada bloqueado.

Toggle: REGIME_FILTER_ENABLED env (default "1").
Fail-open: se fetch falha, retorna NORMAL.

API:
  await get_regime_status() -> {
      "regime": str,
      "btc_24h_pct": float | None,
      "btc_dominance": float | None,
      "block_all": bool,
      "block_alt_longs": bool,
      "downgrade_alt_longs": bool,
      "reasons": [str],
  }
  is_btc_symbol(symbol) -> bool
"""
from __future__ import annotations
import logging
import os
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

REGIME_FILTER_ENABLED = os.getenv("REGIME_FILTER_ENABLED", "1").strip() not in (
    "0", "false", "False", "no", "off", "",
)

# Thresholds
RISK_OFF_BTC_24H = -5.0          # BTC caindo 5%+ em 24h → bloqueia tudo
ALT_DANGER_DOM = 56.0            # dominance acima disso
ALT_DANGER_BTC_24H = 3.0         # E BTC subindo 3%+ → alts sangram
BTC_DOMINANT_THRESHOLD = 55.0    # dominance alta — downgrade alt longs
# Fix #2 (B): só rebaixa long de alt se, ALÉM da dominância alta, o BTC estiver
# de fato puxando pra cima (rotação real pro BTC). Sem isso a regra era cega —
# rebaixava long de alt só pelo número da dominância, mesmo com alts subindo.
BTC_DOMINANT_MIN_BTC_24H = float(os.getenv("BTC_DOMINANT_MIN_BTC_24H", "1.5"))

# ── #3 ALT_RISK_OFF (USDT.D-aware) ──────────────────────────────────────────
# Os regimes acima só olham BTC% + dominância. Mas capital fugindo p/ stablecoin
# (USDT.D elevada) com dominância alta é um ambiente em que LONG de alt sangra
# MESMO sem o BTC pumpando — exatamente o cenário dos stops recentes (dominância
# ~56% + USDT.D ~8% + BTC de lado/caindo). Este regime detecta isso e, por
# default, REBAIXA (não bloqueia) long de alt; opcionalmente bloqueia se a flag
# de block estiver ligada. DEFAULT OFF (NO-OP): liga via env após revisar.
ALT_RISKOFF_ENABLED = os.getenv("ALT_RISKOFF_ENABLED", "false").strip().lower() not in (
    "0", "false", "no", "off", "",
)
ALT_RISKOFF_USDT_D = float(os.getenv("ALT_RISKOFF_USDT_D", "5.0"))   # USDT.D >= isso = risk-off
ALT_RISKOFF_DOM = float(os.getenv("ALT_RISKOFF_DOM", "55.0"))        # dominância BTC >= isso
ALT_RISKOFF_BLOCK = os.getenv("ALT_RISKOFF_BLOCK", "false").strip().lower() in ("1", "true", "yes")

_cache: Dict[str, Any] = {"ts": 0, "data": None}
CACHE_TTL = 600  # 10min: regime muda devagar


def is_btc_symbol(symbol: str) -> bool:
    """Considera BTC e ETH como 'majors' (não sofrem ALT_DANGER)."""
    s = symbol.upper()
    base = s.split("/")[0].split("-")[0]
    return base in ("BTC", "ETH")


async def _fetch_btc_24h_pct() -> Optional[float]:
    """% mudança do BTC nas últimas 24h.

    Usa fetch_ticker, que já calcula `change` a partir de open24h (preço de
    24h atrás) vs last. A versão antiga usava fetch_ohlcv, que retorna um
    pandas DataFrame — o teste `if not candles` levantava ValueError
    (truth value of a DataFrame is ambiguous) e a função sempre devolvia None,
    desligando a proteção RISK_OFF.
    """
    try:
        from services.binance_service import fetch_ticker
        t = await fetch_ticker("BTC/USDT:USDT")
        change = t.get("change") if isinstance(t, dict) else None
        if change is None:
            return None
        return round(float(change), 2)
    except Exception as e:
        log.warning(f"[regime] btc 24h falhou: {e}")
        return None


async def _fetch_btc_dominance() -> Optional[float]:
    try:
        from services.macro_service import get_btc_dominance
        return await get_btc_dominance()
    except Exception as e:
        log.warning(f"[regime] dominance falhou: {e}")
        return None


def _classify(
    btc_24h: Optional[float],
    dom: Optional[float],
    usdt_d: Optional[float] = None,
) -> Dict[str, Any]:
    reasons = []
    regime = "NORMAL"
    block_all = False
    block_alt_longs = False
    downgrade_alt_longs = False

    if btc_24h is not None and btc_24h <= RISK_OFF_BTC_24H:
        regime = "RISK_OFF"
        block_all = True
        reasons.append(f"BTC {btc_24h:+.2f}% em 24h (limiar RISK_OFF: {RISK_OFF_BTC_24H}%)")
    elif (
        dom is not None and dom >= ALT_DANGER_DOM
        and btc_24h is not None and btc_24h >= ALT_DANGER_BTC_24H
    ):
        regime = "ALT_DANGER"
        block_alt_longs = True
        reasons.append(
            f"Dominância BTC {dom:.1f}% + BTC {btc_24h:+.2f}% 24h "
            f"(capital migrando p/ BTC, alts sangram)"
        )
    elif (
        dom is not None and dom >= BTC_DOMINANT_THRESHOLD
        and btc_24h is not None and btc_24h >= BTC_DOMINANT_MIN_BTC_24H
    ):
        # Só rebaixa long de alt quando há rotação real pro BTC (dominância alta
        # E BTC subindo). Se as alts não estão de fato sangrando, não penaliza.
        regime = "BTC_DOMINANT"
        downgrade_alt_longs = True
        reasons.append(
            f"Dominância BTC {dom:.1f}% + BTC {btc_24h:+.2f}% 24h "
            f"(rotação pro BTC — long de alt rebaixado)"
        )

    # #3 ALT_RISK_OFF (USDT.D): capital fugindo p/ stable + dominância alta. Só
    # entra se NENHUM regime acima já tratou alt longs (não sobrepõe RISK_OFF /
    # ALT_DANGER). Default rebaixa; bloqueia se ALT_RISKOFF_BLOCK ligado.
    if (
        ALT_RISKOFF_ENABLED and not block_all and not block_alt_longs
        and usdt_d is not None and usdt_d >= ALT_RISKOFF_USDT_D
        and dom is not None and dom >= ALT_RISKOFF_DOM
    ):
        regime = "ALT_RISK_OFF"
        if ALT_RISKOFF_BLOCK:
            block_alt_longs = True
        else:
            downgrade_alt_longs = True
        reasons.append(
            f"USDT.D {usdt_d:.2f}% (>= {ALT_RISKOFF_USDT_D}) + dominância {dom:.1f}% "
            f"(>= {ALT_RISKOFF_DOM}) — capital em stable, long de alt "
            f"{'bloqueado' if ALT_RISKOFF_BLOCK else 'rebaixado'}"
        )

    return {
        "regime": regime,
        "btc_24h_pct": btc_24h,
        "btc_dominance": dom,
        "usdt_dominance": usdt_d,
        "block_all": block_all,
        "block_alt_longs": block_alt_longs,
        "downgrade_alt_longs": downgrade_alt_longs,
        "reasons": reasons,
    }


async def get_regime_status() -> Dict[str, Any]:
    """Status do regime macro. Cache 10min. Fail-open."""
    if not REGIME_FILTER_ENABLED:
        return {
            "regime": "NORMAL", "btc_24h_pct": None, "btc_dominance": None,
            "block_all": False, "block_alt_longs": False,
            "downgrade_alt_longs": False, "reasons": ["filter disabled"],
        }

    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    btc_24h = await _fetch_btc_24h_pct()
    dom = await _fetch_btc_dominance()

    # USDT.D só é buscada quando o regime ALT_RISK_OFF está ligado (evita custo
    # extra de rede quando o gate está OFF). get_crypto_totals já é cacheado 10min.
    usdt_d = None
    if ALT_RISKOFF_ENABLED:
        try:
            from services.macro_service import get_crypto_totals
            totals = await get_crypto_totals()
            usdt_d = totals.get("usdt_dominance") if isinstance(totals, dict) else None
        except Exception as e:
            log.warning(f"[regime] usdt.d falhou (fail-open): {e}")

    data = _classify(btc_24h, dom, usdt_d)
    if btc_24h is not None or dom is not None:
        _cache["data"] = data
        _cache["ts"] = now
    else:
        # Fail-open: tudo None → NORMAL, sem cachear (tenta de novo logo)
        log.info("[regime] dados indisponíveis — fail-open NORMAL")

    return data


def should_block_recommendation(regime_status: Dict[str, Any], symbol: str, direction: str) -> Optional[str]:
    """
    Decide se uma rec específica deve ser bloqueada.
    Retorna a razão (str) se bloquear, None se passar.
    """
    if regime_status.get("block_all"):
        return f"regime {regime_status.get('regime')}: bloqueia tudo"
    if regime_status.get("block_alt_longs"):
        if direction == "long" and not is_btc_symbol(symbol):
            return f"regime ALT_DANGER: alt long bloqueado"
    return None
