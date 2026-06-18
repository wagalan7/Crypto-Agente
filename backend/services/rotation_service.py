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
        from services.shadow_trade_service import get_exec_allowlist
        allow = get_exec_allowlist()
        if allow:
            return set(allow), "allowlist"
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


# ════════════════════════════════════════════════════════════════════════════
# FASE 2 — APLICAÇÃO AUTOMÁTICA (atrás de flag, default OFF)
# ════════════════════════════════════════════════════════════════════════════
# Quando ROTATION_AUTO_APPLY=on: a cada ciclo o motor confronta o plano dry-run
# com o estado persistido (DB) e SÓ aplica uma mudança que:
#   1. passou o piso de liquidez (promote só de bases no top-N por volume), e
#   2. sustentou ROTATION_HYSTERESIS_CYCLES ciclos consecutivos (histerese), e
#   3. respeita o teto ROTATION_MAX_UNIVERSE (quando cheio, promove por desloca-
#      mento: só entram tantos quantos saírem por demote; o resto fica pendente).
# Promoção aditiva, demoção rara. A allowlist gerida vive no DB (env = seed).
# Com a flag OFF (default) NADA disto roda: rotação segue 100% dry-run (FASE 1).

ROTATION_AUTO_APPLY = os.getenv("ROTATION_AUTO_APPLY", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
ROTATION_MAX_UNIVERSE = int(os.getenv("ROTATION_MAX_UNIVERSE", "100"))
ROTATION_HYSTERESIS_CYCLES = max(1, int(os.getenv("ROTATION_HYSTERESIS_CYCLES", "3")))
# Piso de liquidez: só promove bases dentro do top-N por volume 24h.
ROTATION_LIQ_FLOOR_TOP_N = int(os.getenv("ROTATION_LIQ_FLOOR_TOP_N", "200"))
# Cadência do loop de aplicação (segundos). Default 6h → histerese de 3 ciclos ≈ 18h.
ROTATION_APPLY_INTERVAL_SEC = int(os.getenv("ROTATION_APPLY_INTERVAL_SEC", "21600"))


async def _seed_universe() -> List[str]:
    """Semente da allowlist gerida = env EXEC_UNIVERSE_ALLOWLIST (ou top-N fallback)."""
    current, _ = await _resolve_current_universe()
    return sorted(current)


async def _load_state():
    """Get-or-create do singleton RotationUniverseState (id=1). Semeia universo."""
    from db import get_session
    from models.rotation_state import RotationUniverseState
    from sqlalchemy import select

    async with get_session() as session:
        row = (await session.execute(
            select(RotationUniverseState).where(RotationUniverseState.id == 1)
        )).scalar_one_or_none()
        if row is None:
            seed = await _seed_universe()
            row = RotationUniverseState(id=1, universe=seed, pending={})
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return {
            "universe": list(row.universe or []),
            "pending": dict(row.pending or {}),
        }


async def _save_state(universe: List[str], pending: Dict[str, Any]) -> None:
    from datetime import datetime, timezone
    from db import get_session
    from models.rotation_state import RotationUniverseState
    from sqlalchemy import select

    async with get_session() as session:
        row = (await session.execute(
            select(RotationUniverseState).where(RotationUniverseState.id == 1)
        )).scalar_one_or_none()
        if row is None:
            row = RotationUniverseState(id=1)
            session.add(row)
        row.universe = sorted(set(universe))
        row.pending = pending
        row.applied_at = datetime.now(timezone.utc)
        await session.commit()


async def _liquidity_floor() -> set:
    """Set de bases dentro do top-N por volume (piso de liquidez pra promoção)."""
    from services.learning_service import _base_symbol
    try:
        from services.recommendation_service import _get_server_data_source
        svc, _ = _get_server_data_source()
        syms = await svc.fetch_top_volume_symbols(limit=ROTATION_LIQ_FLOOR_TOP_N)
        if syms:
            return {_base_symbol(s) for s in syms if s}
    except Exception as e:
        log.warning(f"[rotation] piso de liquidez indisponível ({e}) — promoção sem piso")
    return set()  # vazio = sem piso (fail-open, mas histerese ainda protege)


async def get_effective_allowlist() -> set:
    """
    Allowlist que a execução DEVE usar agora.
      • ROTATION_AUTO_APPLY off → env (FASE 1; DB ignorado).
      • on + DB tem universo → universo gerido pela rotação.
    Usada no startup pra primar a allowlist efetiva do shadow_trade_service.
    """
    if not ROTATION_AUTO_APPLY:
        from services.shadow_trade_service import EXEC_UNIVERSE_ALLOWLIST
        return set(EXEC_UNIVERSE_ALLOWLIST)
    try:
        st = await _load_state()
        return set(st["universe"])
    except Exception as e:
        log.warning(f"[rotation] get_effective_allowlist falhou ({e}) — usando env")
        from services.shadow_trade_service import EXEC_UNIVERSE_ALLOWLIST
        return set(EXEC_UNIVERSE_ALLOWLIST)


async def prime_effective_allowlist() -> None:
    """No boot: se auto-apply on, carrega o universo do DB na allowlist efetiva."""
    if not ROTATION_AUTO_APPLY:
        return
    try:
        allow = await get_effective_allowlist()
        from services.shadow_trade_service import set_exec_allowlist
        set_exec_allowlist(allow)
        log.info(f"[rotation] allowlist efetiva primada do DB → {len(allow)} bases")
    except Exception as e:
        log.warning(f"[rotation] prime_effective_allowlist falhou: {e}")


async def apply_rotation_plan(plan: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Aplica o plano de rotação com histerese + piso de liquidez + teto.
    SÓ muta quando ROTATION_AUTO_APPLY=on (senão devolve preview sem persistir).
    """
    if plan is None:
        plan = await compute_rotation_plan(days=0)

    preview_only = not ROTATION_AUTO_APPLY
    if preview_only:
        return {
            "applied": False,
            "reason": "ROTATION_AUTO_APPLY=off (dry-run)",
            "would_promote": [p["symbol"] for p in plan.get("promote", [])],
            "would_demote": [d["symbol"] for d in plan.get("demote", [])],
            "plan": plan,
        }

    try:
        state = await _load_state()
    except Exception as e:
        return {"applied": False, "reason": f"DB indisponível: {e}"}

    universe = set(state["universe"])
    pending: Dict[str, Any] = dict(state["pending"])
    liq = await _liquidity_floor()

    promo_avg = {p["symbol"]: (p.get("avg_r") or 0) for p in plan.get("promote", [])}
    demo_avg = {d["symbol"]: (d.get("avg_r") or 0) for d in plan.get("demote", [])}

    # Candidatos deste ciclo (promote só passa o piso de liquidez, se houver piso).
    cycle: Dict[str, str] = {}
    for b in promo_avg:
        if liq and b not in liq:
            continue  # reprovado no piso de liquidez → não conta ciclo
        if b not in universe:
            cycle[b] = "promote"
    for b in demo_avg:
        if b in universe:
            cycle[b] = "demote"

    # Histerese: incrementa quem reaparece com a mesma ação; reseta quem sumiu/trocou.
    new_pending: Dict[str, Any] = {}
    for b, action in cycle.items():
        prev = pending.get(b)
        cnt = (prev["count"] + 1) if (prev and prev.get("action") == action) else 1
        new_pending[b] = {"action": action, "count": cnt}

    ready_promote = [b for b, p in new_pending.items()
                     if p["action"] == "promote" and p["count"] >= ROTATION_HYSTERESIS_CYCLES]
    ready_demote = [b for b, p in new_pending.items()
                    if p["action"] == "demote" and p["count"] >= ROTATION_HYSTERESIS_CYCLES]

    # Demoção primeiro (libera espaço), depois promoção respeitando o teto.
    applied_demote = sorted(ready_demote, key=lambda b: demo_avg.get(b, 0))
    after_demote = universe - set(applied_demote)
    slots = max(0, ROTATION_MAX_UNIVERSE - len(after_demote))
    ranked_promote = sorted(ready_promote, key=lambda b: promo_avg.get(b, 0), reverse=True)
    applied_promote = ranked_promote[:slots]  # resto fica pendente (count mantido)

    new_universe = (after_demote | set(applied_promote))

    # Limpa do pending quem foi aplicado (zera contagem); mantém quem ainda espera
    # (inclusive promotes que ficaram sem vaga — voltam quando abrir espaço).
    for b in applied_promote + applied_demote:
        new_pending.pop(b, None)

    changed = bool(applied_promote or applied_demote)
    if changed:
        await _save_state(sorted(new_universe), new_pending)
        from services.shadow_trade_service import set_exec_allowlist
        set_exec_allowlist(new_universe)
    else:
        # Sem mudança aplicada, mas persiste os contadores de histerese atualizados.
        await _save_state(sorted(universe), new_pending)

    result = {
        "applied": changed,
        "promoted": sorted(applied_promote),
        "demoted": sorted(applied_demote),
        "new_count": len(new_universe),
        "new_universe": sorted(new_universe),
        "pending": new_pending,
        "max": ROTATION_MAX_UNIVERSE,
        "hysteresis_cycles": ROTATION_HYSTERESIS_CYCLES,
    }
    if changed:
        # Reaproveita o formato de notificação (constrói um "plan" enxuto).
        notif_plan = {
            "changed": True,
            "promote": [{"symbol": b, "avg_r": promo_avg.get(b)} for b in applied_promote],
            "demote": [{"symbol": b, "avg_r": demo_avg.get(b)} for b in applied_demote],
            "new_count": len(new_universe),
            "new_universe": sorted(new_universe),
        }
        try:
            await notify_rotation_plan(notif_plan)
        except Exception as e:
            log.warning(f"[rotation] notify do apply falhou: {e}")
        log.info(f"[rotation] APLICADO +{applied_promote} -{applied_demote} → {len(new_universe)} bases")
    return result
