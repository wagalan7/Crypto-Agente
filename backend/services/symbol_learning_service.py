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
import bisect
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from db import DB_ENABLED, get_session

log = logging.getLogger(__name__)

# ── Flags/tunáveis (env) ─────────────────────────────────────────────────────
# Mestre: liga a APLICAÇÃO ao vivo do size_quality_mult na stack de sizing.
SYMBOL_LEARNING_SIZE_ENABLED = os.getenv(
    "SYMBOL_LEARNING_SIZE_ENABLED", "true"
).strip().lower() in ("1", "true", "yes")
# Aprender automaticamente quando o sweep concluir (só popula a tabela; seguro).
SYMBOL_LEARNING_LEARN_ON_SWEEP = os.getenv(
    "SYMBOL_LEARNING_LEARN_ON_SWEEP", "true"
).strip().lower() in ("1", "true", "yes")
# Avisar no Telegram quando o reaprendizado (pós-sweep) muda o tamanho de moedas —
# quem subiu (aposta maior) / caiu (aposta menor) / entrou novo no aprendizado.
SYMBOL_LEARNING_NOTIFY = os.getenv(
    "SYMBOL_LEARNING_NOTIFY", "true"
).strip().lower() in ("1", "true", "yes")
# Movimento MÍNIMO no multiplicador pra reportar (evita ruído de arredondamento).
SYMBOL_LEARNING_NOTIFY_MIN_DELTA = float(
    os.getenv("SYMBOL_LEARN_NOTIFY_MIN_DELTA", "0.03")
)

# Amostra mínima pra confiar no histórico daquela moeda/TF.
MIN_TRADES = int(os.getenv("SYMBOL_LEARN_MIN_TRADES", "30"))
# Fator de calibração backtest→vivo (mesmo 0.70 usado no ranking/seed).
CALIB_FACTOR = float(os.getenv("SYMBOL_LEARN_CALIB_FACTOR", "0.70"))
# Clamps duros do multiplicador de size (defensivo↔amplificador).
SIZE_MULT_MIN = float(os.getenv("SYMBOL_LEARN_SIZE_MIN", "0.75"))
SIZE_MULT_MAX = float(os.getenv("SYMBOL_LEARN_SIZE_MAX", "1.15"))
# Confiança mínima pra o multiplicador AGIR ao vivo (abaixo disto → 1.0 no-op).
MIN_CONFIDENCE_APPLY = float(os.getenv("SYMBOL_LEARN_MIN_CONF", "0.25"))
# Faixa MORTA em torno da mediana (percentil 0.5±DEADBAND) onde o mult fica 1.0.
# Evita churn de quem está no meio do pelotão; só topo/fundo do universo agem.
REL_DEADBAND = float(os.getenv("SYMBOL_LEARN_DEADBAND", "0.10"))

# Cache em memória: base -> {tf: row_dict}. Populado por refresh_cache().
_CACHE: dict[str, dict[str, dict]] = {}
_CACHE_LOADED = False


def _base_of(symbol: str) -> str:
    """"TAG/USDT:USDT" → "TAG". Idempotente pra bases já limpas."""
    if not symbol:
        return ""
    return symbol.split("/")[0].strip().upper()


# ── R11B2: contrato numérico das linhas aprendidas ──────────────────────────
# Número inválido nunca vira ranking, multiplicador ou amplificação de risco.
# O fallback é sempre 1.0 = NÃO aplicar esta camada. Isso não significa risco
# zero, não garante a segurança da operação e não desliga outras proteções.
_ELIGIBLE_SMALL, _ELIGIBLE_INVALID = "amostra_pequena", "numerico"


def _finite(value) -> Optional[float]:
    """Número real finito. bool, string (mesmo numérica), objeto e None → None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _count(value, default=None) -> Optional[int]:
    """Contagem inteira não negativa. Ausente → default legado; bool não conta."""
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    number = _finite(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _bounded(value, lo: float, hi: float, default=None) -> Optional[float]:
    """Número finito dentro de [lo, hi]. Ausente → default legado."""
    if value is None:
        return default
    number = _finite(value)
    return number if number is not None and lo <= number <= hi else None


def _eligible_with_reason(stats):
    """(métricas, None) ou (None, motivo ∈ {amostra_pequena, numerico})."""
    if not isinstance(stats, dict):
        return None, _ELIGIBLE_INVALID
    n = _count(stats.get("n_trades"), default=0)
    if n is None:
        return None, _ELIGIBLE_INVALID
    wf = _finite(stats.get("wf_avg_r"))
    if wf is None:
        # Ausência de edge out-of-sample é inelegibilidade; valor presente e
        # inválido (string, bool, NaN, ±inf) é rejeição numérica.
        return None, (_ELIGIBLE_SMALL if stats.get("wf_avg_r") is None else _ELIGIBLE_INVALID)
    if n < MIN_TRADES:
        return None, _ELIGIBLE_SMALL
    # Metadados legados AUSENTES conservam os defaults da fórmula (0), sem
    # serem apresentados como observação comprovada. Presente e inválido, não.
    wf_n = _count(stats.get("wf_n_trades"), default=0)
    expiry = _bounded(stats.get("expiry_pct"), 0.0, 100.0, default=0.0)
    if wf_n is None or expiry is None:
        return None, _ELIGIBLE_INVALID
    calib = _finite(wf * CALIB_FACTOR)
    if calib is None:
        return None, _ELIGIBLE_INVALID
    return {"n": n, "wf": wf, "wf_n": wf_n, "expiry": expiry, "calib": calib}, None


def _eligible_metrics(stats: dict) -> Optional[dict]:
    """PURA. Extrai as métricas de histórico se a moeda/TF é elegível (amostra
    suficiente + edge out-of-sample presente E numérica). None → fallback."""
    return _eligible_with_reason(stats)[0]


def _mult_from_rank(percentile: float, expiry: float) -> Optional[float]:
    """PURA. Mapeia a POSIÇÃO RELATIVA da moeda no universo → multiplicador de size.
    Mediana (percentil 0.5) → 1.0; topo → SIZE_MULT_MAX; fundo → SIZE_MULT_MIN.
    Faixa morta em torno da mediana mantém neutro. Penalidade por expiry por cima.

    Racional: qualidade é RELATIVA — o backtest só computa moedas com edge positiva,
    então thresholds absolutos só amplificariam. Ancorar na mediana do universo dá à
    camada as duas mãos: reforça o terço de cima, alivia o de baixo.

    R11B2: entrada fora do domínio ([0,1] e [0,100]) ou resultado não finito ⇒
    None — a camada não é aplicada em vez de devolver um multiplicador fictício."""
    rank = _bounded(percentile, 0.0, 1.0)
    penalty = _bounded(expiry, 0.0, 100.0)
    if rank is None or penalty is None:
        return None
    d = rank - 0.5
    span = max(1e-6, 0.5 - REL_DEADBAND)
    if abs(d) <= REL_DEADBAND:
        m = 1.0
    elif d > 0:
        frac = min(1.0, (d - REL_DEADBAND) / span)
        m = 1.0 + frac * (SIZE_MULT_MAX - 1.0)
    else:
        frac = min(1.0, (-d - REL_DEADBAND) / span)
        m = 1.0 - frac * (1.0 - SIZE_MULT_MIN)
    # Penalidade por expiry histórico (alvos raramente batidos a tempo).
    if penalty >= 45:
        m *= 0.85
    elif penalty >= 30:
        m *= 0.92
    clamped = _finite(round(max(SIZE_MULT_MIN, min(SIZE_MULT_MAX, m)), 4))
    return clamped


def derive_params(stats: dict, edge_percentile: float) -> Optional[dict]:
    """PURA. Destila tunáveis por-moeda a partir das métricas de histórico completo
    de UMA linha + a POSIÇÃO da moeda no universo (edge_percentile ∈ [0,1]). Retorna
    dict pronto pra persistir, ou None se a amostra é pequena/sem edge out-of-sample."""
    em = _eligible_metrics(stats)
    if em is None:
        return None
    m = _mult_from_rank(edge_percentile, em["expiry"])
    if m is None:
        return None
    # Confiança: cresce com amostra total e com o tamanho do braço out-of-sample.
    conf = (min(1.0, em["n"] / 120.0)) * (0.5 + 0.5 * min(1.0, em["wf_n"] / 40.0))
    conf = _bounded(round(max(0.0, min(1.0, conf)), 3), 0.0, 1.0)
    if conf is None:
        return None
    derived = {
        "size_quality_mult": m,
        "confidence": conf,
        "n_trades": em["n"],
        "wf_avg_r": round(em["wf"], 4),
        "wf_n_trades": em["wf_n"],
        "expiry_pct": round(em["expiry"], 2),
        "calibrated_edge": round(em["calib"], 4),
    }
    # Saída derivada também precisa ser finita: nunca dicionário meio válido.
    if any(_finite(derived[field]) is None for field in
           ("size_quality_mult", "confidence", "wf_avg_r", "expiry_pct", "calibrated_edge")):
        return None
    return derived


async def relearn_all_from_history() -> dict:
    """Varre TODO o symbol_backtest_stats e (re)aprende os params por-moeda.
    Escolhe, por base, a MELHOR linha (maior wf_avg_r entre os TFs elegíveis) —
    é o TF em que a moeda tem a edge de histórico mais forte. UPSERT idempotente.
    Fail-soft: retorna resumo mesmo se o DB falhar. Refresca o cache ao final."""
    from models.symbol_backtest_stats import SymbolBacktestStats
    from models.symbol_learned_params import SymbolLearnedParams

    summary = {"scanned": 0, "learned": 0, "skipped_small": 0, "skipped_invalid": 0, "bases": 0}
    # Rastreia movimento antigo→novo do size_quality_mult por base (pra avisar no
    # Telegram quem subiu/caiu/entrou). Cada base tem 1 upsert (melhor TF).
    changes: list[dict] = []
    if not DB_ENABLED:
        summary["error"] = "db_disabled"
        return summary

    try:
        async with get_session() as session:
            rows = (await session.execute(select(SymbolBacktestStats))).scalars().all()
            summary["scanned"] = len(rows)

            # 1ª passada — melhor (base, tf) por base pela edge calibrada elegível.
            best: dict[str, tuple[float, object]] = {}
            for r in rows:
                if r.error:
                    continue
                # R11B2: uma linha numericamente ruim não derruba o lote nem
                # envenena o ranking — é contada à parte e ignorada.
                try:
                    em, reason = _eligible_with_reason(r.to_dict())
                except Exception:
                    em, reason = None, _ELIGIBLE_INVALID
                if em is None:
                    summary["skipped_invalid" if reason == _ELIGIBLE_INVALID
                            else "skipped_small"] += 1
                    continue
                base = _base_of(r.symbol)
                if base not in best or em["calib"] > best[base][0]:
                    best[base] = (em["calib"], r)

            summary["bases"] = len(best)

            # Distribuição do universo: percentil da edge calibrada por base.
            calibs = sorted(v[0] for v in best.values())
            n_uni = len(calibs)

            def _pct_rank(x: float) -> float:
                if n_uni <= 1:
                    return 0.5
                lo = bisect.bisect_left(calibs, x)
                hi = bisect.bisect_right(calibs, x)
                return ((lo + hi) / 2.0) / n_uni

            # 2ª passada — deriva e persiste com a posição relativa no universo.
            for base, (calib, r) in best.items():
                derived = derive_params(r.to_dict(), _pct_rank(calib))
                if derived is None:
                    continue
                tf = r.timeframe
                existing = (await session.execute(
                    select(SymbolLearnedParams).where(
                        SymbolLearnedParams.base == base,
                        SymbolLearnedParams.timeframe == tf,
                    )
                )).scalar_one_or_none()
                old_mult = None
                if existing is None:
                    existing = SymbolLearnedParams(base=base, timeframe=tf)
                    session.add(existing)
                else:
                    # Valor anterior inválido não vira delta financeiro inventado.
                    old_mult = _finite(existing.size_quality_mult)
                changes.append({
                    "base": base,
                    "old": (round(old_mult, 4) if old_mult is not None else None),
                    "new": derived["size_quality_mult"],
                })
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
        f"{summary['scanned']} linhas ({summary['skipped_small']} amostra pequena, "
        f"{summary['skipped_invalid']} numérico inválido)"
    )

    # Aviso Telegram: classifica o movimento e manda resumo (no-op se desligado
    # ou sem credenciais). Falha aqui NUNCA quebra o relearn.
    if SYMBOL_LEARNING_NOTIFY and changes:
        try:
            from services.notification_service import send_telegram, fmt_symbol_rerank
            ups: list[dict] = []
            downs: list[dict] = []
            news: list[dict] = []
            for c in changes:
                if c["old"] is None:
                    news.append(c)
                    continue
                delta = c["new"] - c["old"]
                if delta >= SYMBOL_LEARNING_NOTIFY_MIN_DELTA:
                    ups.append(c)
                elif delta <= -SYMBOL_LEARNING_NOTIFY_MIN_DELTA:
                    downs.append(c)
            ups.sort(key=lambda c: c["new"] - c["old"], reverse=True)
            downs.sort(key=lambda c: c["new"] - c["old"])
            if ups or downs or news:
                await send_telegram(
                    fmt_symbol_rerank(summary, ups, downs, news),
                    event_type="symbol_rerank",
                )
        except Exception as _e:
            log.warning(f"[symbol-learning] notify rerank falhou: {_e}")

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


def _size_limits():
    """Limites desta camada. Não finitos ou incoerentes ⇒ no-op (sem consertar ENV)."""
    lo, hi = _finite(SIZE_MULT_MIN), _finite(SIZE_MULT_MAX)
    min_conf = _bounded(MIN_CONFIDENCE_APPLY, 0.0, 1.0)
    if lo is None or hi is None or min_conf is None or not 0.0 < lo <= hi:
        return None
    return lo, hi, min_conf


#: Metadados opcionais: ausentes em linha legada é OK; presentes e inválidos não.
_OPTIONAL_METADATA = (("n_trades", "count"), ("wf_n_trades", "count"),
                      ("wf_avg_r", "finite"), ("calibrated_edge", "finite"),
                      ("expiry_pct", "percent"))


def _checked_field(value, kind: str):
    if kind == "count":
        return _count(value)
    if kind == "percent":
        return _bounded(value, 0.0, 100.0)
    if kind == "unit":
        return _bounded(value, 0.0, 1.0)
    return _finite(value)


def _row_numbers(row):
    """(mult, confiança) de uma linha aprendida íntegra; None se algo não fecha."""
    if not isinstance(row, dict):
        return None
    mult = _finite(row.get("size_quality_mult"))
    conf = _bounded(row.get("confidence"), 0.0, 1.0)
    if mult is None or mult <= 0.0 or conf is None:
        return None
    for field, kind in _OPTIONAL_METADATA:
        value = row.get(field)
        if value is not None and _checked_field(value, kind) is None:
            return None
    return mult, conf


def get_size_mult(symbol_or_base: str, timeframe: Optional[str] = None) -> tuple[float, str]:
    """SÍNCRONO. Multiplicador de size aprendido pra (base, tf). Ordem de resolução:
    (base, tf exato) → melhor tf da base → 1.0. Respeita a flag mestre e a confiança
    mínima. NO-OP-SAFE: flag OFF, cache vazio ou confiança baixa → (1.0, motivo).

    R11B2: valida os números ANTES do clamp, inclusive em linhas antigas do DB
    ou injetadas no cache. Inválido ⇒ (1.0, motivo) = camada não aplicada."""
    if not SYMBOL_LEARNING_SIZE_ENABLED:
        return 1.0, "off"
    limits = _size_limits()
    if limits is None:
        return 1.0, "limites de size inválidos"
    lo, hi, min_conf = limits
    base = _base_of(symbol_or_base) if "/" in (symbol_or_base or "") else (symbol_or_base or "").strip().upper()
    by_tf = _CACHE.get(base)
    if not by_tf:
        return 1.0, "sem histórico aprendido"

    if timeframe and timeframe in by_tf:
        # TF exato inválido é no-op: não se troca de TF para contornar a invalidez.
        row = by_tf[timeframe]
        numbers = _row_numbers(row)
        if numbers is None:
            return 1.0, "linha aprendida inválida"
    else:
        # Fallback preexistente (maior confiança), agora só entre linhas válidas;
        # `max` mantém a primeira em caso de empate, como antes.
        candidates = [(candidate, _row_numbers(candidate)) for candidate in by_tf.values()]
        candidates = [item for item in candidates if item[1] is not None]
        if not candidates:
            return 1.0, "sem linha aprendida válida"
        row, numbers = max(candidates, key=lambda item: item[1][1])
    mult, conf = numbers

    if conf < min_conf:
        return 1.0, f"confiança {conf:.2f} < {min_conf:.2f}"
    mult = round(max(lo, min(hi, mult)), 4)
    if abs(mult - 1.0) < 1e-9:
        return 1.0, "neutro"
    return mult, (
        f"hist {row.get('timeframe')} calib={row.get('calibrated_edge')} "
        f"conf={conf:.2f}→×{mult:.2f}"
    )


def _safe_status_row(row: dict) -> dict:
    """Cópia SEGURA para exibição: campo inválido → None + diagnóstico.
    Não altera modelo, banco nem cache."""
    safe = dict(row) if isinstance(row, dict) else {}
    invalid = []
    for field, kind in (("size_quality_mult", "finite"), ("confidence", "unit"),
                        *_OPTIONAL_METADATA):
        value = safe.get(field)
        if value is None:
            continue
        if _checked_field(value, kind) is None:
            safe[field] = None
            invalid.append(field)
    safe["invalid_fields"] = invalid
    return safe


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
        out["learned"] = [_safe_status_row(r.to_dict()) for r in rows]
        out["invalid_rows"] = sum(1 for r in out["learned"] if r["invalid_fields"])
    except Exception as e:
        log.warning(f"[symbol-learning] status falhou: {e}")
    return out
