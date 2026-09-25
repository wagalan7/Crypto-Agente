#!/usr/bin/env python3
"""Entrypoint de PESQUISA: dados → núcleo → score V3 → decisão → observação →
persistência → exportação → replay/comparação.

Roda a montagem com os COMPONENTES REAIS, não com cópias de teste:

    strategy_core_service.decide        (núcleo puro, três playbooks)
    score_v3_service.score              (features/decomposição de pesquisa)
    decision_observation_service        (coleta PRE_SELECTION + flush do R09)
    research_dataset_service            (exportação somente leitura)
    portfolio_replay_service            (carteira sobre o motor R10A)
    walk_forward_service                (janelas e veredito da política)

Fonte SINTÉTICA e determinística por padrão. Nenhuma ordem, nenhuma leitura de
produção, nenhuma ativação: o adaptador operacional continua indisponível e o
relatório diz isso com todas as letras.

Uso:
    python3 backend/scripts/research_pipeline.py --symbols 6 --seed 7
    DATABASE_URL=... python3 backend/scripts/research_pipeline.py --persist
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

HOUR = 3_600_000
BAR5 = 300_000
#: Estado do adaptador operacional: existe contrato e simulação, NÃO existe
#: rota de execução. Isto não é "pendência externa" — é código por fazer.
LIVE_ADAPTER_STATUS = "LIVE_ADAPTER_NOT_IMPLEMENTED"


def synthetic_states(core, *, symbols: int, seed: int, t0: int):
    """Séries sintéticas com gatilho de retomada em vela fechada."""
    rng = random.Random(seed)
    states = []
    for index in range(symbols):
        base = 100.0 + index
        drift = rng.uniform(-0.02, 0.02)
        bars = [core.Candle(open_time_ms=t0 + i * HOUR, open=base + 0.3 + drift * i,
                            high=base + 0.6 + drift * i, low=base + 0.2 + drift * i,
                            close=base + 0.4 + drift * i, volume=100.0)
                for i in range(79)]
        trigger_close = base + 1.0
        bars.append(core.Candle(open_time_ms=t0 + 79 * HOUR, open=base + 0.3,
                                high=trigger_close + 0.2, low=base + 0.2,
                                close=trigger_close, volume=90.0))
        states.append(core.MarketState(
            symbol=f"SYN{index}/USDT:USDT", timeframe="1h", bar_ms=HOUR,
            as_of_ms=t0 + 80 * HOUR, bars=tuple(bars), atr=1.0,
            ema_fast=base, ema_slow=base - 2.0, adx=30.0, rsi=60.0, volume_ma=100.0,
            target_levels=(base + 2.5, base + 3.5),
            higher_timeframes=(core.HigherTimeframeView(
                timeframe="4h", as_of_ms=t0 + 79 * HOUR,
                direction="long" if index % 4 else "short",
                confirmed=True, strength=0.8),)))
    return states


def rising_bars(start_ms: int, entry: float, count: int = 40):
    """Primeira barra NEGOCIA a entrada (o motor trata entrada como limite),
    as seguintes sobem sem voltar ao stop."""
    bars = [{"timestamp_ms": start_ms, "open": entry + 0.6, "high": entry + 0.8,
             "low": entry - 0.1, "close": entry + 0.2, "volume": 25.0}]
    for i in range(1, count):
        base = entry + 0.2 + i * 0.2
        bars.append({"timestamp_ms": start_ms + i * BAR5, "open": base,
                     "high": base + 0.3, "low": base - 0.1,
                     "close": base + 0.2, "volume": 25.0})
    return bars


async def run(args) -> dict:
    from services import decision_observation_service as obs
    from services import offline_replay_service as r10a
    from services import portfolio_replay_service as pf
    from services import preselection_observation_service as pre
    from services import score_v3_service as s3
    from services import strategy_core_service as core
    from services import walk_forward_service as wf

    t0 = int(args.t0)
    report = {
        "entrypoint": "research_pipeline",
        "mode": "LOCAL_RESEARCH_ONLY",
        "live_equivalent": False,
        "promotable": False,
        "live_adapter": LIVE_ADAPTER_STATUS,
        "core_version": core.CORE_VERSION,
        "score_version": s3.SCORE_VERSION,
        "config_hash": core.DEFAULT_CONFIG.config_hash(),
    }

    # 1. Dados → núcleo (decisão com motivos, não só manifesto).
    states = synthetic_states(core, symbols=args.symbols, seed=args.seed, t0=t0)
    decisions = [core.decide(state) for state in states]
    eligible = [item for item in decisions if item["state"] == core.STATE_ELIGIBLE]
    report["candidates"] = {"evaluated": len(decisions), "eligible": len(eligible),
                            "by_reason": {}}
    for item in decisions:
        if item["state"] == core.STATE_ELIGIBLE:
            continue
        for code in item["reason_codes"]:
            report["candidates"]["by_reason"][code] = \
                report["candidates"]["by_reason"].get(code, 0) + 1

    # 2. Score V3 sobre os elegíveis (pontuação técnica, nunca probabilidade).
    scores = []
    for decision in eligible:
        levels = decision["levels"]
        risk = abs(levels["entry"] - levels["stop_loss"])
        payload = s3.score({
            "adx": 30.0, "htf_alignment_ratio": 1.0, "structure_quality": 0.8,
            "level_distance_atr": 0.5, "trigger_body_ratio": 0.7,
            "trigger_follow_through_atr": 0.4,
            "rr_tp2": (abs(levels["tp2"] - levels["entry"]) / risk) if risk else None,
            "entry_distance_atr": 0.1, "volume_ratio": 1.1, "spread_pct": 0.03,
            "funding_pct": 0.0,
        }, playbook=decision["playbook"], side=decision["side"])
        scores.append({"opportunity_key": decision["opportunity_key"],
                       "state": payload["state"], "score": payload["score"],
                       "probability": payload["probability"]})
    report["score_v3"] = {
        "scored": len(scores),
        "probability_available": any(item["probability"] is not None for item in scores),
        "economic_approval": s3.economic_verdict(
            s3.score({}, playbook=core.PLAYBOOK_TREND_PULLBACK, side="long"))["economic_approval"],
    }

    # 3. Observação pré-seleção (coleta real; desligada = no-op declarado).
    rows = []
    for decision in decisions:
        accepted = decision["state"] == core.STATE_ELIGIBLE
        stages = [{"stage": pre.STAGE_CANDIDATE, "verdict": pre.VERDICT_PASSED},
                  {"stage": pre.STAGE_PLAYBOOK,
                   "verdict": pre.VERDICT_PASSED if decision["playbook"] else pre.VERDICT_REJECTED,
                   "reason_code": None if decision["playbook"] else "NO_PLAYBOOK"},
                  {"stage": pre.STAGE_CANDLE, "verdict": pre.VERDICT_PASSED}]
        if accepted:
            stages.append({"stage": pre.STAGE_GEOMETRY_RR, "verdict": pre.VERDICT_PASSED})
        levels = decision.get("levels") or {}
        rows.append({
            "setup": {"symbol": decision["symbol"], "timeframe": decision["timeframe"],
                      "side": decision.get("side") or "long",
                      "playbook": decision.get("playbook") or "UNRESOLVED",
                      "playbook_version": decision.get("playbook_version") or "NONE",
                      "trigger_candle_ms": decision.get("trigger_candle_ms"),
                      "entry": levels.get("entry"), "stop_loss": levels.get("stop_loss"),
                      "tp1": levels.get("tp1"), "tp2": levels.get("tp2"), "atr": 1.0},
            "stages": stages, "accepted": accepted,
            "decision_ts_ms": decision["decision_ts_ms"],
            "availability": {"depth": False, "funding": True},
            "source": {"decision_source": "research_pipeline", "resolution": "1h"},
        })
    collected = obs.observe_preselection(rows)
    report["observation"] = {"enabled": pre.collection_enabled(), **collected}

    # 4. Persistência + exportação (só com banco declarado pelo operador).
    report["persistence"] = {"attempted": bool(args.persist), "state": "SKIPPED"}
    if args.persist:
        try:
            import db
            if not db.DB_ENABLED:
                report["persistence"]["state"] = "DB_DISABLED"
            else:
                await db.init_db()
                await obs.flush_pending()
                report["persistence"]["state"] = "FLUSHED"
        except Exception as exc:  # noqa: BLE001
            report["persistence"] = {"attempted": True, "state": "ERROR", "error": str(exc)}

    # 5. Replay de carteira com o candidato do núcleo (motor R10A).
    candidates, bars_by_id, quotes = [], {}, {}
    for decision in eligible:
        levels = decision["levels"]
        key = decision["opportunity_key"]
        decision_ms = decision["decision_ts_ms"]
        first = ((decision_ms + BAR5 - 1) // BAR5) * BAR5
        candidates.append({"opportunity_id": key, "symbol": decision["symbol"],
                           "direction": decision["side"], "decision_ts_ms": decision_ms,
                           "entry": levels["entry"], "stop_loss": levels["stop_loss"],
                           "tp1": levels["tp1"], "tp2": levels["tp2"], "atr": 1.0})
        bars_by_id[key] = rising_bars(first, levels["entry"])
        quotes[key] = {"bid": levels["entry"] - 0.01, "ask": levels["entry"] + 0.01,
                       "ts_ms": decision_ms, "source": "synthetic"}
    replay = pf.run_portfolio(candidates, bars_by_id=bars_by_id, quotes_by_id=quotes,
                              costs=r10a.CostConfig(fee_bps_per_side=4.0,
                                                    slippage_bps_per_side=2.0,
                                                    funding_bps_per_bar=1.0))
    report["replay"] = {"admitted": replay["admitted"], "rejected": replay["rejected"],
                        "metrics": replay["metrics"],
                        "fidelity_unavailable": replay["fidelity"]["unavailable"],
                        "live_equivalent": replay["live_equivalent"]}

    # 6. Comparação da política inteira pelo runner de walk-forward.
    decision_at = {item["opportunity_id"]: item["decision_ts_ms"] for item in candidates}
    baseline = [{"opportunity_id": trade["opportunity_id"], "net_r": trade["net_r"],
                 "decision_ts_ms": decision_at.get(trade["opportunity_id"])}
                for trade in replay["trades"] if trade["admitted"]]
    candidate_side = [{"opportunity_id": item["opportunity_id"],
                       "decision_ts_ms": item["decision_ts_ms"],
                       "net_r": (item["net_r"] + 0.05) if item["net_r"] is not None else None}
                      for item in baseline]
    # As dobras cobrem o período em que as decisões realmente aconteceram.
    if baseline:
        janela_inicio = min(item["decision_ts_ms"] for item in baseline) - 40 * BAR5
        janela_fim = max(item["decision_ts_ms"] for item in baseline) + 60 * BAR5
    else:
        janela_inicio, janela_fim = t0, t0 + 200 * BAR5
    study = wf.run_walk_forward(
        baseline=baseline, candidate=candidate_side,
        folds=wf.rolling_windows(start_ms=janela_inicio, end_ms=janela_fim, bar_ms=BAR5,
                                 train_bars=20, test_bars=5, step_bars=5)["folds"],
        bar_ms=BAR5, costs_complete=True, horizon_sufficient=True, seed=args.seed)
    report["walk_forward"] = {"state": study["verdict"]["state"],
                              "winner": study["verdict"]["winner"],
                              "reason_codes": list(study["verdict"]["reason_codes"]),
                              "folds_executed": study["folds_executed"],
                              "promotable": study["verdict"]["promotable"]}
    report["next_step"] = ("Acumular amostra prospectiva pelo coletor ligado antes de "
                           "qualquer go/no-go; o adaptador operacional continua por implementar.")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pipeline de pesquisa Crypto Win")
    parser.add_argument("--symbols", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--t0", type=int, default=1_760_000_400_000)
    parser.add_argument("--persist", action="store_true",
                        help="grava a observação usando DATABASE_URL (descartável)")
    args = parser.parse_args(argv)
    if args.symbols < 1 or args.symbols > 200:
        print(json.dumps({"status": "INVALID_ARGS", "detail": "symbols fora de 1..200"}))
        return 2
    report = asyncio.run(run(args))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
