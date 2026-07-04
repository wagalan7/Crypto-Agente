"""
Symbol Learning Service — autoaprimoramento por HISTÓRICO COMPLETO (pós-sweep).

Pedido do usuário: quando o sweep de backtest termina de cobrir TODO o universo,
o app deve "ler cada moeda e se autoaprimorar percorrendo todo o histórico" antes
de operar em tempo real. Este serviço é essa ponte.

Fluxo:
  1. `relearn_all_from_history()` — varre `symbol_backtest_stats` (a edge de todo o
     histórico de cada moeda, já computada pelo sweep), destila tunáveis por-moeda
     e faz UPSERT em `symbol_learned_params`. Chamado automaticamente ao FIM do
     sweep (hook no backtest_universe_service) e sob demanda via endpoint.
  2. `refresh_cache()` — carrega os params aprendidos num cache em memória.
  3. `get_size_mult(base, tf)` — acessor SÍNCRONO (a stack de sizing é chamada em
     contexto async mas o lookup é O(1) em memória) usado pelo shadow_trade_service.

Tunável destilado hoje: `size_quality_mult` — multiplicador de size defensivo
(<1.0 p/ edge fraca) ou amplificador LIMITADO (>1.0 p/ edge forte de histórico),
sempre clampado. Composição multiplicativa com o resto da stack; os caps duros de
_compute_qty (MAX_RISK_PCT_HARD) mandam por último.

SEGURANÇA: derivar/persistir é sempre seguro (só popula a tabela). A APLICAÇÃO ao
vivo é gated por SYMBOL_LEARNING_SIZE_ENABLED (default OFF) — o usuário revisa a
tabela aprendida e só então liga.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from db import DB_ENABLED, get_session

log = logging.getLogger(__name__)

# ── Flags/tunáveis (env) ─────────────────────────────────────────────────────
# Mestre: liga a APLICAÇÃO ao vivo do size_quality_mult na stack de sizing.
SYMBOL_LEARNING_SIZE_ENABLED = os.getenv(
    "SYMBOL_LEARNING_SIZE_ENABLED", "false"
).strip().lower() in ("1", "true", "yes")
# Aprender automaticamente quando o sweep concluir (só popula a tabela; seguro).
SYMBOL_LEARNING_LEARN_ON_SWEEP = os.getenv(
    "SYMBOL_LEARNING_LEARN_ON_SWEEP", "true"
).strip().lower() in ("1", "true", "yes")

# Amostra mínima pra confiar no histórico daquela moeda/TF.
MIN_TRADES = int(os.getenv("SYMBOL_LEARN_MIN_TRADES", "30"))
# Fator de calibração backtest→vivo (mesmo 0.70 usado no ranking/seed).
CALIB_FACTOR = float(os.getenv("SYMBOL_LEARN_CALIB_FACTOR", "0.70"))
# Clamps duros do multiplicador de size (defensivo↔amplificador).
SIZE_MULT_MIN = float(os.getenv("SYMBOL_LEARN_SIZE_MIN", "0.75"))
SIZE_MULT_MAX = float(os.getenv("SYMBOL_LEARN_SIZE_MAX", "1.15"))
# Confiança mínima pra o multiplicador AGIR ao vivo (abaixo disto → 1.0 no-op).
MIN_CONFIDENCE_APPLY = float(os.getenv("SYMBOL_LEARN_MIN_CONF", "0.25"))

# Cache em memória: base -> {tf: row_dict}. Populado por refresh_cache().
_CACHE: dict[str, dict[str, dict]] = {}
_CACHE_LOADED = False


def _base_of(symbol: str) -> str:
    """"TAG/USDT:USDT" → "TAG". Idempotente pra bases já limpas."""
    if not symbol:
        return ""
    return symbol.split("/")[0].strip().upper()


def derive_params(stats: dict) -> Optional[dict]:
    """PURA. Destila tunáveis por-moeda a partir das métricas de histórico completo
    de UMA linha de symbol_backtest_stats. Retorna dict pronto pra persistir, ou
    None se a amostra é pequena/sem edge out-of-sample (→ fallback global ao vivo).

    Racional do size_quality_mult:
      • Ancorado na edge CALIBRADA de todo o histórico (wf_avg_r × 0.70). Walk-forward
        (out-of-sample) é o número em que confiar, não o avg_r in-sample.
      • Edge forte → amplifica LEVE (teto 1.15). Edge fraca/negativa → corta (piso 0.75).
      • Penaliza expiry alto: se historicamente os alvos raramente eram atingidos a
        tempo, a moeda é menos confiável mesmo com R médio ok.
    """
    n = int(stats.get("n_trades") or 0)
    wf = stats.get("wf_avg_r")
    if n < MIN_TRADES or wf is None:
        return None
    try:
        wf = float(wf)
    except Exception:
        return None

    expiry = float(stats.get("expiry_pct") or 0.0)
    wf_n = int(stats.get("wf_n_trades") or 0)
    calib = wf * CALIB_FACTOR

    # Degraus monotônicos por edge calibrada.
    if calib >= 0.90:
        m = 1.12
    elif calib >= 0.60:
        m = 1.06
    elif calib >= 0.35:
        m = 1.00
    elif calib >= 0.15:
        m = 0.90
    else:
        m = 0.80

    # Penalidade por expiry histórico (alvos raramente batidos a tempo).
    if expiry >= 45:
        m *= 0.85
    elif expiry >= 30:
        m *= 0.92

    m = round(max(SIZE_MULT_MIN, min(SIZE_MULT_MAX, m)), 4)

    # Confiança: cresce com amostra total e com o tamanho do braço out-of-sample.
    conf = (min(1.0, n / 120.0)) * (0.5 + 0.5 * min(1.0, wf_n / 40.0))
    conf = round(max(0.0, min(1.0, conf)), 3)

    return {
        "size_quality_mult": m,
        "confidence": conf,
        "n_trades": n,
        "wf_avg_r": round(wf, 4),
        "wf_n_trades": wf_n,
        "expiry_pct": round(expiry, 2),
        "calibrated_edge": round(calib, 4),
    }


async def relearn_all_from_history() -> dict:
    """Varre TODO o symbol_backtest_stats e (re)aprende os params por-moeda.
    Escolhe, por base, a MELHOR linha (maior wf_avg_r entre os TFs elegíveis) —
    é o TF em que a moeda tem a edge de histórico mais forte. UPSERT idempotente.
    Fail-soft: retorna resumo mesmo se o DB falhar. Refresca o cache ao final."""
    from models.symbol_backtest_stats import SymbolBacktestStats
    from models.symbol_learned_params import SymbolLearnedParams

    summary = {"scanned": 0, "learned": 0, "skipped_small": 0, "bases": 0}
    if not DB_ENABLED:
        summary["error"] = "db_disabled"
        return summary

    try:
        async with get_session() as session:
            rows = (await session.execute(select(SymbolBacktestStats))).scalars().all()
            summary["scanned"] = len(rows)

            # Melhor (base, tf) por base: maior wf_avg_r elegível.
            best: dict[str, tuple[float, object, dict]] = {}
            for r in rows:
                if r.error:
                    continue
                stats = r.to_dict()
                derived = derive_params(stats)
                if derived is None:
                    summary["skipped_small"] += 1
                    continue
                base = _base_of(r.symbol)
                score = float(r.wf_avg_r or 0.0)
                if base not in best or score > best[base][0]:
                    best[base] = (score, r, derived)

            summary["bases"] = len(best)

            for base, (_score, r, derived) in best.items():
                tf = r.timeframe
                existing = (await session.execute(
                    select(SymbolLearnedParams).where(
                        SymbolLearnedParams.base == base,
                        SymbolLearnedParams.timeframe == tf,
                    )
                )).scalar_one_or_none()
                if existing is None:
                    existing = SymbolLearnedParams(base=base, timeframe=tf)
                    session.add(existing)
                existing.size_quality_mult = derived["size_quality_mult"]
                existing.confidence = derived["confidence"]
                existing.source = "backtest_history"
                existing.n_trades = derived["n_trades"]
                existing.wf_avg_r = derived["wf_avg_r"]
                existing.wf_n_trades = derived["wf_n_trades"]
                existing.expiry_pct = derived["expiry_pct"]
                existing.calibrated_edge = derived["calibrated_edge"]
                existing.params = {"size_quality_mult": derived["size_quality_mult"]}
                existing.learned_at = datetime.now(timezone.utc)
                summary["learned"] += 1

            await session.commit()
    except Exception as e:
        log.warning(f"[symbol-learning] relearn falhou: {e}")
        summary["error"] = str(e)
        return summary

    await refresh_cache()
    log.info(
        f"[symbol-learning] relearn OK: {summary['learned']} moedas aprendidas de "
        f"{summary['scanned']} linhas ({summary['skipped_small']} amostra pequena)"
    )
    return summary


async def refresh_cache() -> int:
    """Carrega symbol_learned_params → cache em memória. Retorna nº de linhas."""
    global _CACHE, _CACHE_LOADED
    if not DB_ENABLED:
        _CACHE_LOADED = True
        return 0
    try:
        from models.symbol_learned_params import SymbolLearnedParams
        async with get_session() as session:
            rows = (await session.execute(select(SymbolLearnedParams))).scalars().all()
        cache: dict[str, dict[str, dict]] = {}
        for r in rows:
            cache.setdefault(r.base, {})[r.timeframe] = r.to_dict()
        _CACHE = cache
        _CACHE_LOADED = True
        return len(rows)
    except Exception as e:
        log.warning(f"[symbol-learning] refresh_cache falhou: {e}")
        _CACHE_LOADED = True
        return 0


def get_size_mult(symbol_or_base: str, timeframe: Optional[str] = None) -> tuple[float, str]:
    """SÍNCRONO. Multiplicador de size aprendido pra (base, tf). Ordem de resolução:
    (base, tf exato) → melhor tf da base → 1.0. Respeita a flag mestre e a confiança
    mínima. NO-OP-SAFE: flag OFF, cache vazio ou confiança baixa → (1.0, motivo)."""
    if not SYMBOL_LEARNING_SIZE_ENABLED:
        return 1.0, "off"
    base = _base_of(symbol_or_base) if "/" in (symbol_or_base or "") else (symbol_or_base or "").strip().upper()
    by_tf = _CACHE.get(base)
    if not by_tf:
        return 1.0, "sem histórico aprendido"

    row = None
    if timeframe and timeframe in by_tf:
        row = by_tf[timeframe]
    else:
        # Melhor linha da base (maior confiança).
        row = max(by_tf.values(), key=lambda x: x.get("confidence") or 0.0)

    conf = float(row.get("confidence") or 0.0)
    if conf < MIN_CONFIDENCE_APPLY:
        return 1.0, f"confiança {conf:.2f} < {MIN_CONFIDENCE_APPLY:.2f}"
    mult = float(row.get("size_quality_mult") or 1.0)
    mult = round(max(SIZE_MULT_MIN, min(SIZE_MULT_MAX, mult)), 4)
    if abs(mult - 1.0) < 1e-9:
        return 1.0, "neutro"
    return mult, (
        f"hist {row.get('timeframe')} calib={row.get('calibrated_edge')} "
        f"conf={conf:.2f}→×{mult:.2f}"
    )


async def status() -> dict:
    """Snapshot pro painel: config + o que foi aprendido (top/bottom por edge)."""
    out = {
        "size_apply_enabled": SYMBOL_LEARNING_SIZE_ENABLED,
        "learn_on_sweep": SYMBOL_LEARNING_LEARN_ON_SWEEP,
        "min_trades": MIN_TRADES,
        "calib_factor": CALIB_FACTOR,
        "size_mult_range": [SIZE_MULT_MIN, SIZE_MULT_MAX],
        "min_confidence_apply": MIN_CONFIDENCE_APPLY,
        "count": 0,
        "learned": [],
    }
    if not DB_ENABLED:
        return out
    try:
        from models.symbol_learned_params import SymbolLearnedParams
        async with get_session() as session:
            rows = (await session.execute(
                select(SymbolLearnedParams).order_by(
                    SymbolLearnedParams.calibrated_edge.desc()
                )
            )).scalars().all()
        out["count"] = len(rows)
        out["learned"] = [r.to_dict() for r in rows]
    except Exception as e:
        log.warning(f"[symbol-learning] status falhou: {e}")
    return out
