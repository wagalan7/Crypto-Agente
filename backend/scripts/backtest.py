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

from services.recommendation_backtest import (  # noqa: E402
    run_backtest, run_walkforward, run_param_sweep,
)

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
    ap.add_argument("--folds", type=int, default=0,
                    help="se >=2, roda walk-forward com N janelas temporais (default 0=off)")
    ap.add_argument("--sweep", type=str, default=None,
                    help="param sweep: PARAM=v1,v2,v3 (ex: ATR_TRAIL_K=2.0,2.2,2.5)")
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
    if args.sweep:
        if "=" not in args.sweep:
            print("--sweep precisa de formato PARAM=v1,v2,v3"); return
        pname, vstr = args.sweep.split("=", 1)
        values = [v.strip() for v in vstr.split(",") if v.strip()]
        print(f"\n▶ Param sweep: {pname} = {values}\n")
        report = await run_param_sweep(
            symbols=symbols, timeframes=timeframes,
            param_name=pname.strip(), values=values,
            days_back=args.days, step_bars=args.step,
            n_folds=args.folds, end_dt=end_dt,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        print(f"\n══════════ SWEEP {report['param']} ({elapsed:.1f}s) ══════════")
        print(f"  original: {report['original_value']}\n")
        # Cabeçalho
        print(f"  {'value':>10} {'n':>4} {'wr%':>6} {'PF':>6} {'R':>8} "
              f"{'avgR':>7} {'expR':>7} {'DD':>6}", end="")
        if args.folds and args.folds >= 2:
            print(f"  {'stab%':>6} {'cons':>5}")
        else:
            print()
        for v in report["variants"]:
            m = v["summary"]
            if not m or m.get("total_trades", 0) == 0:
                print(f"  {str(v['value']):>10} sem trades")
                continue
            pf = m["profit_factor"]
            pf_s = f"{pf:>6.2f}" if pf is not None else "    ∞ "
            line = (f"  {str(v['value']):>10} {m['total_trades']:>4} "
                    f"{m['win_rate_pct']:>6.1f} {pf_s} {m['total_r']:>+8.2f} "
                    f"{m['avg_r']:>+7.3f} {m['expectancy_r']:>+7.3f} "
                    f"{m['max_dd_r']:>5.2f}R")
            if v.get("walkforward"):
                wf = v["walkforward"]
                cr = wf["consistency_ratio"]
                cr_s = f"{cr:>5.2f}" if cr is not None else "    ∞"
                line += f"  {wf['stability_pct']:>6.1f} {cr_s}"
            print(line)
        # Por tier
        print("\n  Por tier:")
        for v in report["variants"]:
            tiers = v.get("by_tier", {})
            line_bits = [f"{str(v['value']):>10}"]
            for tier in ["A+", "A", "B"]:
                if tier in tiers:
                    tm = tiers[tier]
                    pf = tm["profit_factor"]
                    pf_s = f"{pf:.1f}" if pf is not None else "∞"
                    line_bits.append(
                        f"{tier} n={tm['total_trades']} wr={tm['win_rate_pct']}% "
                        f"PF={pf_s} R={tm['total_r']:+.1f}"
                    )
                else:
                    line_bits.append(f"{tier} -")
            print("    " + "  ·  ".join(line_bits))
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2, default=str))
            print(f"\n✓ JSON completo salvo em {args.out}")
        print()
        return

    if args.folds and args.folds >= 2:
        report = await run_walkforward(
            symbols=symbols, timeframes=timeframes,
            days_back=args.days, step_bars=args.step,
            n_folds=args.folds, end_dt=end_dt,
        )
    else:
        report = await run_backtest(
            symbols=symbols, timeframes=timeframes,
            days_back=args.days, step_bars=args.step,
            end_dt=end_dt,
        )
        report.pop("_all_trades", None)
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

    if "walkforward" in report:
        wf = report["walkforward"]
        print("\n─ Walk-forward ─")
        print(f"  janelas: {wf['n_folds']} ({wf['folds_with_trades']} c/ trades, "
              f"{wf['empty_folds']} vazias)")
        print(f"  stability: {wf['stability_pct']}% folds positivos "
              f"({wf['positive_folds']}/{wf['folds_with_trades']})")
        cr = wf['consistency_ratio']
        cr_s = f"{cr:.2f}" if cr is not None else "∞"
        print(f"  R por fold:  μ={wf['fold_total_r_mean']:+.2f}  "
              f"σ={wf['fold_total_r_std']:.2f}  consistency(μ/σ)={cr_s}")
        print(f"  WR por fold: μ={wf['fold_wr_mean']:.1f}%  σ={wf['fold_wr_std']:.1f}%")
        print()
        for f in wf["folds"]:
            fm = f["metrics"]
            start_short = f["start"][:10]
            end_short = f["end"][:10]
            if not fm or fm.get("total_trades", 0) == 0:
                print(f"  fold {f['fold']:>2} ({start_short}→{end_short} {f['days']}d): "
                      f"sem trades")
                continue
            pf = fm["profit_factor"]
            pf_s = f"{pf:>5.2f}" if pf is not None else "   ∞ "
            print(f"  fold {f['fold']:>2} ({start_short}→{end_short} {f['days']}d): "
                  f"n={fm['total_trades']:>3} wr={fm['win_rate_pct']:>5}% "
                  f"PF={pf_s} R={fm['total_r']:+7.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
        print(f"\n✓ JSON completo salvo em {args.out}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
