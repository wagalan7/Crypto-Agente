"""Notificacoes Telegram. Desacoplado - sem credenciais = no-op silencioso."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "5"))

# Flag pra silenciar tipos especificos (CSV: "open,tp1" desativa essas)
TELEGRAM_MUTE_EVENTS = {
    e.strip() for e in os.getenv("TELEGRAM_MUTE_EVENTS", "").split(",") if e.strip()
}


def _get(obj: Any, key: str, default=None):
    """Helper: pega atributo de objeto OU chave de dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fmt_num(v, fmt: str = ".6g") -> str:
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return str(v) if v is not None else "?"


async def send_telegram(
    text: str,
    event_type: str = "info",
    parse_mode: str = "Markdown",
) -> bool:
    """Envia mensagem ao Telegram. Retorna True se sucesso, False caso contrario.

    Se TELEGRAM_ENABLED=False ou event_type estiver em mute, no-op silencioso.
    """
    if not TELEGRAM_ENABLED:
        return False
    if event_type in TELEGRAM_MUTE_EVENTS:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                return True
            log.warning(f"[telegram] send failed {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log.warning(f"[telegram] send exception: {e}")
        return False


# === Formatadores por tipo de evento ===

def _allowlist_line(symbol: Any) -> str:
    """Linha DENTRO/FORA da allowlist de execução. Vazia se não der pra inferir."""
    try:
        from services.shadow_trade_service import get_exec_allowlist
        base = str(symbol).split("/")[0].split(":")[0].strip().upper()
        if not base or base == "?":
            return ""
        if base in get_exec_allowlist():
            return "\U0001F4CD *DENTRO* da allowlist\n"
        return "\U0001F310 *FORA* da allowlist (filler)\n"
    except Exception:
        return ""


def fmt_trade_opened(trade: Any, rec: Optional[Any] = None) -> str:
    """Formata mensagem de trade aberto. Aceita trade dict ou objeto."""
    symbol = _get(trade, "symbol", "?")
    side = str(_get(trade, "side", "?")).upper()
    entry = _get(trade, "entry_price", 0) or 0
    sl = _get(trade, "planned_stop", 0) or 0
    tp1 = _get(trade, "planned_tp1", 0) or 0
    tp2 = _get(trade, "planned_tp2", 0) or 0
    qty = _get(trade, "qty", 0) or 0
    lev = _get(trade, "leverage", "?")

    tier = _get(rec, "tier", "?")
    score = _get(rec, "score", "?")
    tf = _get(rec, "timeframe", "?")

    emoji = "\U0001F7E2" if side == "LONG" else "\U0001F534"
    return (
        f"{emoji} *Trade Aberto* \u2014 `{symbol}`\n"
        f"{_allowlist_line(symbol)}"
        f"`{side} {lev}x` \u00B7 TF `{tf}` \u00B7 Tier `{tier}` \u00B7 Score `{score}`\n"
        f"Entry: `{_fmt_num(entry)}`\n"
        f"SL: `{_fmt_num(sl)}` \u00B7 TP1: `{_fmt_num(tp1)}` \u00B7 TP2: `{_fmt_num(tp2)}`\n"
        f"Qty: `{qty}`"
    )


def fmt_tp1_hit(trade: Any, pnl_partial: Optional[float] = None) -> str:
    symbol = _get(trade, "symbol", "?")
    side = str(_get(trade, "side", "?")).upper()
    msg = f"\u2705 *TP1 batido* \u2014 `{symbol}` ({side})\n"
    if pnl_partial is not None:
        msg += f"PnL parcial: `${pnl_partial:.2f}`\nSL movido pro breakeven."
    else:
        msg += "Posicao parcial fechada. SL movido pro BE."
    return msg


def _human_motivo(trade: Any, reason: str, pnl_f: float) -> str:
    """
    Motivo legível pro usuário, refletindo o PERCURSO do trade e não só o gatilho
    final. Quando o TP1 já bateu (phase=post_tp1 ou tp1_realized_usd presente), o
    SL sobe pra BE e o fechamento da sobra por "stop"/"be" NÃO é uma perda crua —
    o TP1 ficou embolsado. Sem isso, um Win virava "Motivo: stop", escondendo o TP1.
      • TP1 batido + tp2  → "TP1 + TP2"
      • TP1 batido + be/stop (PnL≥0) → "TP1 + BE"   (sobra fechou no breakeven)
      • TP1 batido + stop (PnL<0, raro) → "TP1 + Stop"
      • TP1 NÃO batido    → "TP2" / "BE" / "Stop" diretos
    """
    phase = str(_get(trade, "phase", "pre_tp1") or "pre_tp1")
    tp1_hit = phase == "post_tp1" or bool(_get(trade, "tp1_realized_usd", None))
    if tp1_hit:
        if reason == "tp2":
            return "TP1 + TP2"
        return "TP1 + BE" if pnl_f >= 0 else "TP1 + Stop"
    return {"tp2": "TP2", "be": "BE", "stop": "Stop", "tp1": "TP1"}.get(
        reason, str(reason)
    )


def fmt_trade_closed(trade: Any, reason: str = "?", pnl: Optional[float] = None) -> str:
    symbol = _get(trade, "symbol", "?")
    side = str(_get(trade, "side", "?")).upper()

    if pnl is None:
        pnl = _get(trade, "pnl_usd", 0) or 0

    try:
        pnl_f = float(pnl)
    except (TypeError, ValueError):
        pnl_f = 0.0

    if pnl_f > 0:
        emoji = "\U0001F3C6"
        label = "Win"
    elif pnl_f < 0:
        emoji = "\U0001F6D1"
        label = "Loss"
    else:
        emoji = "\u26AA"
        label = "BE"

    motivo = _human_motivo(trade, reason, pnl_f)

    return (
        f"{emoji} *{label}* \u2014 `{symbol}` ({side})\n"
        f"{_allowlist_line(symbol)}"
        f"Motivo: `{motivo}`\n"
        f"PnL: `${pnl_f:.2f}`"
    )


def fmt_time_stop(
    trade: Any,
    age_min: float,
    threshold_min: int,
    category: str = "?",
    tf: Optional[str] = None,
) -> str:
    """Trade fechado por time stop: explica TF, idade, threshold e motivo."""
    symbol = _get(trade, "symbol", "?")
    side = str(_get(trade, "side", "?")).upper()
    entry = _get(trade, "entry_price", 0) or 0
    tp1 = _get(trade, "planned_tp1", 0) or 0
    tf = tf or _get(trade, "timeframe", "?")
    if age_min < 60:
        age_str = f"{age_min:.0f}min"
    elif age_min < 1440:
        age_str = f"{age_min/60:.1f}h"
    else:
        age_str = f"{age_min/1440:.1f}d"
    if threshold_min < 60:
        thr_str = f"{threshold_min}min"
    elif threshold_min < 1440:
        thr_str = f"{threshold_min//60}h"
    else:
        thr_str = f"{threshold_min//1440}d"
    return (
        f"\u23F1\uFE0F *Time Stop* \u2014 `{symbol}` ({side})\n"
        f"Categoria: `{category.upper()}` (TF `{tf}`)\n"
        f"Idade: `{age_str}` \u2265 limite `{thr_str}`\n"
        f"Trade fechado SEM atingir TP1.\n"
        f"Entry: `{_fmt_num(entry)}` \u00B7 TP1 alvo: `{_fmt_num(tp1)}`\n"
        f"_Motivo: posicao sem progresso no prazo limite. Liberar capital pra setups mais frescos._"
    )


def fmt_kill_switch(daily_pnl: float, threshold: float) -> str:
    return (
        f"\U0001F6A8 *KILL SWITCH ATIVADO*\n"
        f"PnL diario: `${daily_pnl:.2f}`\n"
        f"Threshold: `${threshold:.2f}`\n"
        f"Bot pausou novas entradas."
    )


def fmt_error(context: str, detail: str) -> str:
    return f"\u26A0\uFE0F *Erro critico* \u2014 `{context}`\n```\n{detail[:500]}\n```"
