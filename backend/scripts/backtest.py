"""
CLI runner pro backtest_service.

Uso:
  cd backend
  python -m scripts.backtest --symbols BTC,ETH,SOL --tf 1h,4h --days 90
  python -m scripts.backtest --symbols BTC --tf 15m --days 30 --step 2 --out report.json

Saída: imprime resumo em texto + opcionalmente salva JSON completo.

⚠ Backtest é "raw signal" — sem derivatives, sem MTF, sem ticker 24h.
   Score+tier são computados localmente; trail/BE+/time-stop são idênticos
   à produção (reusa snapshot_service._classify_outcome_candles).
   Comparar resultados ABSOLUTOS com win rate real é enganoso. Use pra
   comparação RELATIVA: rodar antes e depois de mudar um parâmetro.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Permite rodar como `python -m scripts.backtest` E como
# `python scripts/backtest.py` (adiciona backend/ no sys.path).
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.recommendation_backtest import run_backtest  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def parse_symbols(s: str):
    """Aceita 'BTC,ETH' OU 'BTC/USDT:USDT,ETH/USDT:USDT' OU misturado."""
    out = []
    for raw in s.split(","):
        raw = raw.strip().upper()
        if not raw:
            continue
        if "/" not in raw:
            raw = f"{raw}/USDT:USDT"
        out.append(raw)
    return out


def print_metrics_block(label: str, m: dict):
    if not m or m.get("total_trades", 0) == 0:
        print(f"  [{label}] sem trades")
        return
    pf = m["profit_factor"]
    sh = m["sharpe_r"]
    pf_s = f"{pf:.2f}" if pf is not None else "∞"
    sh_s = f"{sh:.2f}" if sh is not None else "∞"
    print(f"  [{label}] n={m['total_trades']} "
          f"wr={m['win_rate_pct']}% "
          f"PF={pf_s} "
          f"R={m['total_r']:+.2f} "
          f"avgR={m['avg_r']:+.3f} "
          f"exp={m['expectancy_r']:+.3f} "
          f"Sharpe={sh_s} "
          f"DD={m['max_dd_r']:.2f}R")
    sd = m.get("status_dist", {})
    if sd:
        ord_keys = ["won_tp2", "won_tp1_be", "won_tp1", "expired", "lost", "no_data"]
        parts = [f"{k}={sd[k]}" for k in ord_keys if k in sd]
        print(f"          status: {' · '.join(parts)}")


async def main():
    ap = argparse.ArgumentParser(description="Crypto Win backtest runner")
    ap.add_argument("--symbols", required=True,
                    help="lista CSV (BTC,ETH ou BTC/USDT:USDT,...)")
    ap.add_argument("--tf", required=True,
                    help="timeframes CSV (ex: 15m,1h,4h)")
    ap.add_argument("--days", type=int, default=90,
                    help="dias retroativos (default 90)")
    ap.add_argument("--step", type=int, default=1,
                    help="step entre barras (1=todas, 4=4×4 mais rápido)")
    ap.add_argument("--out", type=str, default=None,
                    help="path pra salvar JSON completo (opcional)")
    ap.add_argument("--end", type=str, default=None,
                    help="data final ISO (default: agora). útil pra walk-forward manual")
    args = ap.parse_args()

    symbols = parse_symbols(args.symbols)
    timeframes = [t.strip() for t in args.tf.split(",") if t.strip()]
    end_dt = None
    if args.end:
        end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    print(f"\n▶ Backtest: {len(symbols)} símbolo(s) × {len(timeframes)} TF "
          f"× {args.days}d (step={args.step})")
    print(f"  symbols: {', '.join(s.split('/')[0] for s in symbols)}")
    print(f"  TFs:     {', '.join(timeframes)}\n")

    t0 = datetime.now()
    report = await run_backtest(
        symbols=symbols, timeframes=timeframes,
        days_back=args.days, step_bars=args.step,
        end_dt=end_dt,
    )
    elapsed = (datetime.now() - t0).total_seconds()

    # ── Imprime resumo ──────────────────────────────────────────────────────
    print(f"\n══════════ RESULTADO ({elapsed:.1f}s) ══════════")
    print(f"\nTrades totais: {report['trades_count']}\n")

    print("─ Global ─")
    print_metrics_block("ALL", report["summary"])

    print("\n─ Por tier ─")
    for tier in ["A+", "A", "B"]:
        if tier in report["by_tier"]:
            print_metrics_block(tier, report["by_tier"][tier])

    print("\n─ Por símbolo ─")
    for sym, m in sorted(report["by_symbol"].items()):
        print_metrics_block(sym.split("/")[0], m)

    print("\n─ Por (sym, tf) ─")
    for p in sorted(report["per_pair"], key=lambda x: (x["symbol"], x["timeframe"])):
        if p.get("error"):
            print(f"  {p['symbol'].split('/')[0]} {p['timeframe']}: ERR {p['error']}")
            continue
        m = p.get("metrics") or {}
        if not m or m.get("total_trades", 0) == 0:
            print(f"  {p['symbol'].split('/')[0]} {p['timeframe']}: "
                  f"{p.get('candles', 0)} candles, 0 trades")
            continue
        pf = m["profit_factor"]
        pf_s = f"{pf:>5.2f}" if pf is not None else "   ∞ "
        print(f"  {p['symbol'].split('/')[0]:>6} {p['timeframe']:>4}: "
              f"n={m['total_trades']:>3} wr={m['win_rate_pct']:>5}% "
              f"PF={pf_s} R={m['total_r']:+7.2f} "
              f"DD={m['max_dd_r']:.2f}R")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
        print(f"\n✓ JSON completo salvo em {args.out}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
