"""
Adaptive Partials Service — decide POR-TRADE a fração do TP1, o tamanho do
runner e a largura do trailing (chandelier), em vez de usar valores fixos.

Motivação
---------
Hoje três parâmetros são env FIXAS e iguais pra todo trade:
  • PROTECTION_TP1_QTY_PCT = 0.45  (fatia embolsada no TP1)
  • RUNNER_QTY_PCT         = 0.20  (fatia que corre solta após TP2)
  • RUNNER_ATR_MULT        = 3.0   (largura do trailing em ATR)

Isso não distingue um sinal A+ em tendência forte (onde vale SEGURAR pra morder
o TP2/runner) de um sinal fraco em mercado lateral (onde vale COLHER maior no
TP1). Este módulo lê características disponíveis na ABERTURA e modula os três
parâmetros dentro de bandas conservadoras.

Lógica (conservadora por padrão)
--------------------------------
  • conviction (0=fraco … 1=forte) ← tier + score + edge_score + prob_tp2
      - forte  → embolsa MENOS no TP1 (segura pro TP2) + runner MAIOR
      - fraco  → embolsa MAIS no TP1 (garante) + runner menor
  • volatilidade (atr_pct)
      - vol baixa → trailing MAIS APERTADO (captura antes do repique devolver)
      - vol alta  → trailing MAIS LARGO (não ser stopado pelo ruído)

Tudo com TETO/PISO. As bandas são env-configuráveis: pra ficar "mais agressivo"
depois, é só alargar as bandas — SEM tocar no código, SEM deploy.

Segurança
---------
  • Gate mestre ADAPTIVE_PARTIALS_ENABLED (default OFF).
  • `compute()` é PURA (recebe números, não faz I/O) → fácil de testar.
  • Guardas absolutos além das bandas (nunca retorna valor absurdo).
  • Fail-soft: qualquer erro → retorna None (chamador cai no valor fixo da env).
"""
from __future__ import annotations
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


# ── Config (lida a cada compute pra permitir mudar env sem restart no worker) ─
def _cfg() -> dict:
    return {
        "enabled": _b("ADAPTIVE_PARTIALS_ENABLED", "false"),
        "test_count": _i("ADAPTIVE_TEST_COUNT", 4),
        # Bandas (defaults conservadores, em torno do fixo de hoje 0.45/0.20/3.0)
        "tp1_min": _f("ADAPTIVE_TP1_MIN", 0.40),
        "tp1_max": _f("ADAPTIVE_TP1_MAX", 0.55),
        "rqty_min": _f("ADAPTIVE_RUNNER_QTY_MIN", 0.15),
        "rqty_max": _f("ADAPTIVE_RUNNER_QTY_MAX", 0.25),
        "atr_min": _f("ADAPTIVE_ATR_MULT_MIN", 2.2),
        "atr_max": _f("ADAPTIVE_ATR_MULT_MAX", 3.2),
        # Âncoras de volatilidade (atr_pct em %) pra normalizar 0..1
        "vol_lo": _f("ADAPTIVE_VOL_LO", 1.0),
        "vol_hi": _f("ADAPTIVE_VOL_HI", 4.0),
    }


# Guardas absolutos (nunca sair disto, independente das bandas configuradas)
_HARD_TP1 = (0.10, 0.90)
_HARD_RQTY = (0.05, 0.50)
_HARD_ATR = (1.0, 6.0)

_TIER_MAP = {"A+": 1.0, "A": 0.6, "B": 0.25}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _conviction_score(tier, score, edge_score, prob_tp2) -> float:
    """Combina sinais disponíveis num escalar 0..1 (0=fraco, 1=forte).

    Média ponderada só dos sinais presentes; se nenhum, retorna 0.5 (neutro).
    """
    parts: list[float] = []
    weights: list[float] = []

    t = _TIER_MAP.get((tier or "").strip()) if tier is not None else None
    if t is not None:
        parts.append(t)
        weights.append(1.0)

    if score is not None:
        try:
            s = _clamp((float(score) - 50.0) / (90.0 - 50.0), 0.0, 1.0)
            parts.append(s)
            weights.append(0.8)
        except Exception:
            pass

    if edge_score is not None:
        try:
            e = _clamp(float(edge_score) / 3.0, 0.0, 1.0)
            parts.append(e)
            weights.append(0.6)
        except Exception:
            pass

    if prob_tp2 is not None:
        try:
            parts.append(_clamp(float(prob_tp2), 0.0, 1.0))
            weights.append(1.0)
        except Exception:
            pass

    if not parts:
        return 0.5
    return sum(p * w for p, w in zip(parts, weights)) / sum(weights)


def is_enabled() -> bool:
    return _cfg()["enabled"]


def test_count() -> int:
    return _cfg()["test_count"]


def compute(
    *,
    tier: Optional[str] = None,
    score: Optional[float] = None,
    edge_score: Optional[float] = None,
    prob_tp2: Optional[float] = None,
    atr_pct: Optional[float] = None,
) -> Optional[dict]:
    """Decide os 3 parâmetros por-trade. PURA (sem I/O além de ler env).

    Retorna dict {tp1_qty_pct, runner_atr_mult, runner_qty_pct, conviction,
    vol_norm, reason} ou None se desligado / erro (chamador usa o fixo).
    """
    try:
        c = _cfg()
        if not c["enabled"]:
            return None

        conv = _conviction_score(tier, score, edge_score, prob_tp2)

        # Convicção alta → segura pro TP2: MENOS no TP1, runner MAIOR.
        tp1 = c["tp1_max"] - conv * (c["tp1_max"] - c["tp1_min"])
        rqty = c["rqty_min"] + conv * (c["rqty_max"] - c["rqty_min"])

        # Trailing pela volatilidade: vol baixa → apertado; vol alta → largo.
        if atr_pct is None:
            vol_norm = 0.5
        else:
            span = max(1e-6, c["vol_hi"] - c["vol_lo"])
            vol_norm = _clamp((float(atr_pct) - c["vol_lo"]) / span, 0.0, 1.0)
        atr_mult = c["atr_min"] + vol_norm * (c["atr_max"] - c["atr_min"])

        # Guardas absolutos
        tp1 = _clamp(tp1, *_HARD_TP1)
        rqty = _clamp(rqty, *_HARD_RQTY)
        atr_mult = _clamp(atr_mult, *_HARD_ATR)

        reason = (
            f"conv={conv:.2f} vol={vol_norm:.2f} → "
            f"TP1={tp1:.0%} runner={rqty:.0%} trail={atr_mult:.2f}×ATR"
        )
        return {
            "tp1_qty_pct": round(tp1, 4),
            "runner_atr_mult": round(atr_mult, 3),
            "runner_qty_pct": round(rqty, 4),
            "conviction": round(conv, 4),
            "vol_norm": round(vol_norm, 4),
            "reason": reason,
        }
    except Exception as e:  # fail-soft → chamador usa a env fixa
        log.warning(f"[adaptive-partials] compute falhou ({e}); usando fixo")
        return None
