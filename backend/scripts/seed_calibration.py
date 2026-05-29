"""
Gera um seed JSON pra calibração score→P(TP1) a partir do backtest histórico.

Resolve a starvation de bins altos: em produção, os primeiros meses tem
pouquíssimos snapshots com score ≥75 (calibration_service retorna P_global
~83% pra tudo). Usando o backtest engine, geramos centenas de trades
virtuais com (score, outcome) que populam todos os bins.

Output: JSON em formato consumido por calibration_service._load_seed_pairs.

Uso:
  cd backend
  python -m scripts.seed_calibration --symbols BTC,ETH,SOL,XRP,BNB \\
      --tf 1h,4h --days 90 --out calibration_seed.json

  # Pra ativar em produção (Railway):
  #   CALIBRATION_SEED_PATH=/app/calibration_seed.json

⚠ Seeds são trades RAW (sem derivatives/MTF real, sem fees). Servem pra
   bootstrap quando não há dados reais suficientes — a tabela vai migrando
   pra real conforme o DB acumula histórico.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.recommendation_backtest import run_backtest  # noqa: E402
from services.calibration_service import (  # noqa: E402
    compute_calibration_from_pairs, RESOLVED_STATUSES,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")


def parse_symbols(s: str):
    out = []
    for raw in s.split(","):
        raw = raw.strip().upper()
        if not raw:
            continue
        if "/" not in raw:
            raw = f"{raw}/USDT:USDT"
        out.append(raw)
    return out


async def main():
    ap = argparse.ArgumentParser(description="Gera seed de calibração via backtest")
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--tf", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--out", type=str, default="calibration_seed.json")
    ap.add_argument("--min-score", type=float, default=55.0,
                    help="filtra trades com score abaixo (default 55)")
    args = ap.parse_args()

    symbols = parse_symbols(args.symbols)
    timeframes = [t.strip() for t in args.tf.split(",") if t.strip()]

    print(f"\n▶ Seed run: {len(symbols)} símbolo(s) × {len(timeframes)} TF "
          f"× {args.days}d (step={args.step})")
    print(f"  symbols: {', '.join(s.split('/')[0] for s in symbols)}")
    print(f"  TFs:     {', '.join(timeframes)}\n")

    t0 = datetime.now()
    report = await run_backtest(
        symbols=symbols, timeframes=timeframes,
        days_back=args.days, step_bars=args.step,
    )
    elapsed = (datetime.now() - t0).total_seconds()

    trades = report.get("_all_trades", [])
    print(f"\n✓ Backtest completo em {elapsed:.1f}s: {len(trades)} trades")

    # Filtra resolvidos + min_score
    pairs = []
    skipped_unresolved = 0
    skipped_low = 0
    for t in trades:
        status = t.get("status")
        score = t.get("score")
        if status not in RESOLVED_STATUSES:
            skipped_unresolved += 1
            continue
        if score is None or score < args.min_score:
            skipped_low += 1
            continue
        pairs.append({"score": float(score), "status": status})

    print(f"  pares válidos: {len(pairs)}  "
          f"(descartados: {skipped_unresolved} unresolved + "
          f"{skipped_low} score<{args.min_score})")

    if not pairs:
        print("✗ Nenhum par válido — abortando")
        return

    # Computa preview da calibração resultante pra mostrar pro usuário
    calib_preview = compute_calibration_from_pairs(
        [(p["score"], p["status"]) for p in pairs], source="seed_preview"
    )
    print(f"\n─ Calibração resultante ─")
    print(f"  total: {calib_preview['total_resolved']} trades  "
          f"wins: {calib_preview['wins_global']}  "
          f"P_global: {calib_preview['p_global']*100:.1f}%\n")
    print(f"  {'bin':>10} {'n':>5} {'wins':>5} {'p_obs':>7} "
          f"{'p_shrunk':>9} {'p_calib':>9}")
    for b in calib_preview["bins"]:
        print(f"  {b['label']:>10} {b['n_total']:>5} {b['n_wins']:>5} "
              f"{b['p_observed']*100:>6.1f}% {b['p_shrunk']*100:>8.1f}% "
              f"{b['p_calibrated']*100:>8.1f}%")

    out_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "backtest",
        "params": {
            "symbols": symbols, "timeframes": timeframes,
            "days_back": args.days, "step_bars": args.step,
            "min_score": args.min_score,
        },
        "n_pairs": len(pairs),
        "pairs": pairs,
        "calibration_preview": calib_preview,
    }
    Path(args.out).write_text(json.dumps(out_payload, indent=2, default=str))
    print(f"\n✓ Seed salvo em {args.out}")
    print(f"  Pra ativar em prod: CALIBRATION_SEED_PATH={args.out}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
