"""
Portfolio Service — risk guard de correlação + cap agregado (Issue #5).

Evita "5 longs em alts" como se fossem 5 trades independentes (na prática
é 1 trade só, com correlação alta). Aplica limites em 3 dimensões:

  1. **Hard limit total**: máx 3 posições simultâneas (`MAX_OPEN_POSITIONS`)
  2. **Hard limit por categoria**: máx 2 da mesma categoria (`MAX_PER_CATEGORY`)
  3. **Cap de exposição agregada**: soma de `risk_pct` ≤ 5% da banca
     (`MAX_AGGREGATE_RISK_PCT`)

Como não temos execução de ordens ainda (issue #11), usamos
`recommendation_snapshots` com `status='open'` como proxy de "posições
abertas" — toda rec emitida vira snapshot e é acompanhada até resolver.

Categorias derivadas do símbolo via regex (override possível depois):
  - btc         (BTC, derivados BTC)
  - eth         (ETH e LSTs)
  - l1          (SOL, ADA, AVAX, NEAR, APT, SUI, ATOM, TON, TRX, BNB)
  - defi        (UNI, AAVE, LDO, CRV, MKR, COMP, SUSHI, 1INCH, GMX, …)
  - meme        (DOGE, SHIB, PEPE, WIF, BONK, FLOKI, …)
  - ai          (FET, AGIX, OCEAN, RNDR, TAO, WLD, …)
  - other       (qualquer outro)

Direction matters: 2 longs em alts ≠ 1 long + 1 short em alts (hedge
parcial). Limite por categoria é por direção (2 longs + 2 shorts ok).
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Iterable

from sqlalchemy import select, func

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

# ── Limites (Fase 1.3) ────────────────────────────────────────────────────
import os as _os

MAX_OPEN_POSITIONS = int(_os.getenv("PORTFOLIO_MAX_OPEN_POSITIONS", "5"))
MAX_PER_CATEGORY = int(_os.getenv("PORTFOLIO_MAX_PER_CATEGORY", "2"))         # por categoria + direção
MAX_AGGREGATE_RISK_PCT = float(_os.getenv("PORTFOLIO_MAX_AGG_RISK_PCT", "5.0"))  # soma de risk_pct

# Janela max pra considerar snapshot "aberto" (segurança contra órfãos)
OPEN_WINDOW_HOURS = int(_os.getenv("PORTFOLIO_OPEN_WINDOW_HOURS", "48"))

# Modo de contagem do portfolio:
#   "real_only" (default) → conta só posições com RealTrade source="auto" ativa
#                          (= ordens realmente abertas na exchange).
#                          Snapshots-tracker que não viraram trade NÃO contam.
#   "snapshots"           → comportamento antigo: conta todo snapshot status='open'
#                          (inflavel; engole o cap com recs só sendo monitoradas).
PORTFOLIO_COUNT_MODE = _os.getenv("PORTFOLIO_COUNT_MODE", "real_only").strip().lower()


# ── Mapeamento símbolo → categoria ───────────────────────────────────────
_CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("btc",  re.compile(r"^(BTC)\b", re.I)),
    ("eth",  re.compile(r"^(ETH|STETH|WSTETH|RETH|CBETH|WBETH)\b", re.I)),
    ("l1",   re.compile(r"^(SOL|ADA|AVAX|NEAR|APT|SUI|ATOM|TON|TRX|BNB|DOT|EGLD|ALGO|HBAR|FTM|KAS|TIA|SEI|INJ)\b", re.I)),
    ("defi", re.compile(r"^(UNI|AAVE|LDO|CRV|MKR|COMP|SUSHI|1INCH|GMX|DYDX|SNX|JUP|PENDLE|CAKE|RUNE|BAL|YFI|CVX)\b", re.I)),
    ("meme", re.compile(r"^(DOGE|SHIB|PEPE|WIF|BONK|FLOKI|MEME|MEW|NEIRO|POPCAT|TURBO|BRETT|MOG|TRUMP|FARTCOIN|GOAT|PNUT)\b", re.I)),
    ("ai",   re.compile(r"^(FET|AGIX|OCEAN|RNDR|RENDER|TAO|WLD|GRT|AI|ARKM|VIRTUAL|IO|NMR|ANTHROPIC|AKT|0G)\b", re.I)),
]


def categorize(symbol: str) -> str:
    """
    Mapeia símbolo (ex: 'BTC/USDT:USDT', 'SOLUSDT') para categoria.
    Retorna 'other' se nenhum padrão bater.
    """
    # Extrai a base (parte antes de '/' ou 'USDT')
    base = symbol.split("/")[0].split(":")[0]
    # Strip 1000/1MIL prefixes (1000PEPE, 1000SHIB)
    base = re.sub(r"^1000+", "", base)
    base = re.sub(r"^1MIL", "", base)
    # Remove USDT trailing se vier sem separador
    base = re.sub(r"USDT$", "", base, flags=re.I)

    for cat, pat in _CATEGORY_PATTERNS:
        if pat.match(base):
            return cat
    return "other"


# ── Leitura de posições abertas ─────────────────────────────────────────
async def get_open_positions() -> list[dict]:
    """
    Em modo "real_only" (default): conta RealTrade com source="auto" e
    status="open" — ou seja, posições REAIS na exchange. Snapshots
    sendo só rastreados pelo tracker NÃO contam (tracker monitora todas
    as recs; só virou trade quando foi auto-executada).

    Em modo "snapshots" (legado): conta snapshots com status="open".
    Útil pra simulação conservadora — bloqueia mesmo se nada virou trade.
    """
    if not DB_ENABLED:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=OPEN_WINDOW_HOURS)

    if PORTFOLIO_COUNT_MODE == "real_only":
        try:
            from models.real_trade import RealTrade  # type: ignore
            async with get_session() as session:
                stmt = (
                    select(RealTrade)
                    .where(RealTrade.status == "open")
                    .where(RealTrade.source == "auto")
                    .where(RealTrade.opened_at >= since)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [
                    {
                        "snapshot_id": getattr(r, "recommendation_id", None),
                        "symbol": r.symbol,
                        "direction": r.side if r.side in ("long", "short") else ("long" if r.side == "Buy" else "short"),
                        "risk_pct": 1.0,  # estimativa default — RealTrade não armazena risk_pct
                        "category": categorize(r.symbol),
                        "opened_at": r.opened_at.isoformat() if getattr(r, "opened_at", None) else None,
                        "tier": None,
                        "source": r.source,
                    }
                    for r in rows
                ]
        except Exception as e:
            log.warning(f"[portfolio] real_only count falhou (fallback snapshots): {e}")

    # Modo legado / fallback
    async with get_session() as session:
        stmt = (
            select(RecommendationSnapshot)
            .where(RecommendationSnapshot.status == "open")
            .where(RecommendationSnapshot.created_at >= since)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "snapshot_id": r.id,
                "symbol": r.symbol,
                "direction": r.direction,
                "risk_pct": float(r.risk_pct or 0.0),
                "category": categorize(r.symbol),
                "opened_at": r.created_at.isoformat() if r.created_at else None,
                "tier": r.tier,
            }
            for r in rows
        ]


def _summarize(open_positions: list[dict]) -> dict:
    """Agrega métricas das posições abertas."""
    total = len(open_positions)
    by_cat: dict[str, int] = {}
    by_cat_dir: dict[str, int] = {}
    by_dir: dict[str, int] = {"long": 0, "short": 0}
    agg_risk = 0.0
    for p in open_positions:
        cat = p["category"]
        d = p["direction"]
        by_cat[cat] = by_cat.get(cat, 0) + 1
        key = f"{cat}:{d}"
        by_cat_dir[key] = by_cat_dir.get(key, 0) + 1
        if d in by_dir:
            by_dir[d] += 1
        agg_risk += p["risk_pct"]
    return {
        "total": total,
        "by_category": by_cat,
        "by_category_direction": by_cat_dir,
        "by_direction": by_dir,
        "aggregate_risk_pct": round(agg_risk, 3),
    }


async def get_exposure() -> dict:
    """Snapshot completo da exposição (pra endpoint /api/portfolio/exposure)."""
    positions = await get_open_positions()
    summary = _summarize(positions)
    return {
        "enabled": DB_ENABLED,
        "limits": {
            "max_open_positions": MAX_OPEN_POSITIONS,
            "max_per_category": MAX_PER_CATEGORY,
            "max_aggregate_risk_pct": MAX_AGGREGATE_RISK_PCT,
        },
        "positions": positions,
        **summary,
    }


# ── Gate: pode emitir uma nova rec? ──────────────────────────────────────
def _check_against_summary(
    summary: dict,
    candidate_symbol: str,
    candidate_direction: str,
    candidate_risk_pct: float,
) -> tuple[bool, str | None]:
    """
    Aplica os 3 limites contra um summary já computado. Retorna
    (allowed, motivo_se_bloqueado).
    """
    if summary["total"] >= MAX_OPEN_POSITIONS:
        return False, (
            f"Limite de {MAX_OPEN_POSITIONS} posições simultâneas atingido"
        )

    cat = categorize(candidate_symbol)
    key = f"{cat}:{candidate_direction}"
    cur = summary["by_category_direction"].get(key, 0)
    if cur >= MAX_PER_CATEGORY:
        return False, (
            f"Já há {cur} {candidate_direction}s em '{cat}' "
            f"(limite {MAX_PER_CATEGORY} por categoria/direção)"
        )

    new_agg = summary["aggregate_risk_pct"] + (candidate_risk_pct or 0.0)
    if new_agg > MAX_AGGREGATE_RISK_PCT:
        return False, (
            f"Exposição agregada {new_agg:.2f}% excederia "
            f"limite {MAX_AGGREGATE_RISK_PCT}%"
        )

    return True, None


async def check_can_open(symbol: str, direction: str, risk_pct: float) -> tuple[bool, str | None]:
    """Versão standalone (carrega exposição do DB)."""
    if not DB_ENABLED:
        return True, None
    positions = await get_open_positions()
    return _check_against_summary(_summarize(positions), symbol, direction, risk_pct)


def filter_recommendations(recommendations: list, open_summary: dict | None = None) -> tuple[list, list[dict]]:
    """
    Filtra lista de Recommendation aplicando portfolio caps incrementalmente.
    Mantém ordem de prioridade (já vem ordenado por tier+score) e drop os
    que excederem caps. Importante: simula a inclusão sequencial pra dar
    chance aos top-ranked.

    Retorna (kept_recommendations, dropped_with_reason).
    """
    if open_summary is None:
        open_summary = {
            "total": 0,
            "by_category_direction": {},
            "aggregate_risk_pct": 0.0,
        }

    # Working copy pra mutação durante o loop
    summary = {
        "total": open_summary.get("total", 0),
        "by_category_direction": dict(open_summary.get("by_category_direction", {})),
        "aggregate_risk_pct": float(open_summary.get("aggregate_risk_pct", 0.0)),
    }

    kept = []
    dropped: list[dict] = []
    for rec in recommendations:
        sym = getattr(rec, "symbol", "")
        direction = getattr(rec, "direction", "")
        risk_pct = float(getattr(rec, "risk_pct", 0.0) or 0.0)
        ok, reason = _check_against_summary(summary, sym, direction, risk_pct)
        if not ok:
            dropped.append({
                "symbol": sym,
                "direction": direction,
                "tier": getattr(rec, "tier", None),
                "reason": reason,
            })
            continue
        kept.append(rec)
        # Atualiza summary incremental
        summary["total"] += 1
        cat = categorize(sym)
        key = f"{cat}:{direction}"
        summary["by_category_direction"][key] = summary["by_category_direction"].get(key, 0) + 1
        summary["aggregate_risk_pct"] += risk_pct

    return kept, dropped
