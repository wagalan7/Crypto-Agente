"""
Motor de rotação do universo de execução (champion/challenger).

FASE 1 — DRY-RUN / PROPOSTA: `compute_rotation_plan()` calcula o que MUDARIA
(promoções aditivas + ejeções de "maçã podre") comparando as stats por símbolo
(`learning_service.compute_symbol_stats`) com o universo de execução atual.
**NÃO muta nada e NÃO opera dinheiro** — só mede e (opcionalmente) notifica.

A aplicação automática (escrever a allowlist no DB) + piso de liquidez + histerese
multi-ciclo vêm na FASE 2, atrás de flag, depois de validar no DEV com dados reais.

Regras (CLAUDE.md "Regra de execução: universo do bot 60 → ~300"):
  • Promoção ADITIVA: base FORA do universo com verdict=promote
    (≥ROTATION_MIN_SAMPLE trades, avg_r>ROTATION_PROMOTE_MIN_R) → entra. Não tira ninguém.
  • Demoção RARA ("maçã podre"): base NO universo com verdict=demote
    (avg_r<ROTATION_DEMOTE_MAX_R) → sai.
  • Histerese (sustentado por N ciclos) e piso de liquidez: FASE 2 (TODO).

Notificação: a cada mudança, push/telegram com quais entraram/saíram, quantas, o
total operando agora e a lista completa de símbolos (pedido do usuário).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

# Baseline do universo atual quando não há allowlist explícita (= top-N por volume,
# as "60" implícitas de hoje). Mesma env que o scan usa.
ROTATION_BASE_TOP_N = int(os.getenv("SERVER_SCAN_TOP_N", "60"))


async def _resolve_current_universe() -> Tuple[set, str]:
    """
    Universo de execução atual (set de bases). Prioridade:
      1. EXEC_UNIVERSE_ALLOWLIST setada → é a fonte da verdade (pós-ativação).
      2. Senão → top-N por volume (universo implícito atual do bot). Fail-open.
    """
    try:
        from services.shadow_trade_service import EXEC_UNIVERSE_ALLOWLIST
        if EXEC_UNIVERSE_ALLOWLIST:
            return set(EXEC_UNIVERSE_ALLOWLIST), "allowlist"
    except Exception:
        pass

    from services.learning_service import _base_symbol
    try:
        from services.recommendation_service import _get_server_data_source
        svc, src = _get_server_data_source()
        syms = await svc.fetch_top_volume_symbols(limit=ROTATION_BASE_TOP_N)
        if syms:
            return {_base_symbol(s) for s in syms if s}, f"top{ROTATION_BASE_TOP_N}:{src}"
    except Exception as e:
        log.warning(f"[rotation] fonte primária do universo falhou: {e}")

    # Fallback final: lista do Vision
    try:
        from services import binance_vision_service as _bvs
        syms = await _bvs.fetch_top_volume_symbols(limit=ROTATION_BASE_TOP_N)
        if syms:
            return {_base_symbol(s) for s in syms if s}, f"top{ROTATION_BASE_TOP_N}:vision"
    except Exception as e:
        log.warning(f"[rotation] fallback Vision do universo falhou: {e}")

    return set(), "indisponível"


def _pick(st: Dict[str, Any]) -> Dict[str, Any]:
    return {k: st.get(k) for k in ("trades", "win_rate", "avg_r", "total_r")}


async def compute_rotation_plan(days: int = 0) -> Dict[str, Any]:
    """
    DRY-RUN: calcula promoções/demoções propostas. NÃO aplica nada.
    """
    from services.learning_service import compute_symbol_stats

    stats = await compute_symbol_stats(days=days)
    current, current_src = await _resolve_current_universe()

    promote: List[Dict[str, Any]] = []
    demote: List[Dict[str, Any]] = []
    for base, st in stats.items():
        in_universe = base in current
        verdict = st.get("verdict")
        if verdict == "promote" and not in_universe:
            promote.append({"symbol": base, **_pick(st)})
        elif verdict == "demote" and in_universe:
            demote.append({"symbol": base, **_pick(st)})

    promote.sort(key=lambda r: (r.get("avg_r") or 0), reverse=True)
    demote.sort(key=lambda r: (r.get("avg_r") or 0))

    new_universe = (current | {p["symbol"] for p in promote}) - {d["symbol"] for d in demote}

    return {
        "dry_run": True,
        "current_source": current_src,
        "current_count": len(current),
        "current_universe": sorted(current),
        "promote": promote,
        "demote": demote,
        "new_count": len(new_universe),
        "new_universe": sorted(new_universe),
        "changed": bool(promote or demote),
        "note": "DRY-RUN: não aplica, não checa liquidez/histerese (FASE 2).",
    }


def fmt_rotation_change(plan: Dict[str, Any]) -> str:
    """Mensagem de push/telegram: quais entraram/saíram, quantas, total e a lista."""
    promo = plan.get("promote", [])
    demo = plan.get("demote", [])
    new_count = plan.get("new_count", 0)
    lines = ["\U0001F501 *Rotação do universo de execução*"]
    if promo:
        names = ", ".join(f"`{p['symbol']}`(+{(p.get('avg_r') or 0):.2f}R)" for p in promo)
        lines.append(f"\u2795 Entraram ({len(promo)}): {names}")
    if demo:
        names = ", ".join(f"`{d['symbol']}`({(d.get('avg_r') or 0):.2f}R)" for d in demo)
        lines.append(f"\u2796 Saíram ({len(demo)}): {names}")
    lines.append(f"\U0001F4CA O bot passa a operar *{new_count}* moedas.")
    syms = plan.get("new_universe", [])
    lines.append("Símbolos: " + (", ".join(f"`{s}`" for s in syms) if syms else "—"))
    return "\n".join(lines)


async def notify_rotation_plan(plan: Dict[str, Any]) -> bool:
    """Notifica a mudança (só se houve mudança). Reaproveita o canal Telegram."""
    if not plan.get("changed"):
        return False
    try:
        from services.notification_service import send_telegram
        return await send_telegram(fmt_rotation_change(plan), event_type="rotation")
    except Exception as e:
        log.warning(f"[rotation] notify falhou: {e}")
        return False
