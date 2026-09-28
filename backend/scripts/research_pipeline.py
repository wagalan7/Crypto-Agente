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
#: Simulação R11: escopo de carteira (a política não é por símbolo aqui),
#: período diário e quantos períodos com evidência nova a histerese exige.
POLICY_SCOPE = "PORTFOLIO"
PERIOD_SECONDS = 86_400
REQUIRED_PERIODS = 2
#: Reprocessamento idempotente LIMITADO após conflito de geração.
MAX_RECALCULOS = 3


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
    exportadas = []
    if args.persist:
        try:
            import db
            if not db.DB_ENABLED:
                report["persistence"]["state"] = "DB_DISABLED"
            else:
                await db.init_db()
                await obs.flush_pending()
                report["persistence"]["state"] = "FLUSHED"
                # O banco não é escrito e esquecido: as MESMAS linhas voltam
                # pelo EXPORTADOR real e alimentam a entrada do replay.
                exportadas, export_info = await exportar_observadas(eligible)
                report["persistence"]["export"] = export_info
        except Exception as exc:  # noqa: BLE001
            report["persistence"] = {"attempted": True, "state": "ERROR", "error": str(exc)}

    # 5. Replay de carteira com o candidato do núcleo (motor R10A).
    # Em modo persistido a entrada vem do EXPORT (banco), não da memória.
    entrada = exportadas or [
        {"opportunity_key": decision["opportunity_key"], "symbol": decision["symbol"],
         "side": decision["side"], "decision_ts_ms": decision["decision_ts_ms"],
         **decision["levels"]} for decision in eligible]
    report["replay_input"] = {"source": "R10B_EXPORT" if exportadas else "IN_MEMORY_DECISIONS",
                              "rows": len(entrada)}
    candidates, bars_by_id, quotes = [], {}, {}
    for linha in entrada:
        key = linha["opportunity_key"]
        decision_ms = linha["decision_ts_ms"]
        first = ((decision_ms + BAR5 - 1) // BAR5) * BAR5
        candidates.append({"opportunity_id": key, "symbol": linha["symbol"],
                           "direction": linha["side"], "decision_ts_ms": decision_ms,
                           "entry": linha["entry"], "stop_loss": linha["stop_loss"],
                           "tp1": linha["tp1"], "tp2": linha["tp2"], "atr": 1.0})
        bars_by_id[key] = rising_bars(first, linha["entry"])
        quotes[key] = {"bid": linha["entry"] - 0.01, "ask": linha["entry"] + 0.01,
                       "ts_ms": decision_ms, "source": "synthetic"}
    custos = r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                             funding_bps_per_bar=1.0)
    # QUAL hipótese está sendo comparada? A identidade dos dois lados é
    # registrada ANTES de qualquer resultado, e o ESCOPO do contraste fica
    # explícito: gestão de saídas não prova núcleo/Score V3.
    baseline_config = r10a.ReplayConfig()
    candidate_config = r10a.ReplayConfig(tp1_fraction=0.60, trail_atr_multiple=1.6,
                                         be_lock_fraction=0.30)
    report["comparison"] = comparacao_declarada(
        baseline_config, candidate_config, custos=custos, core=core, score=s3)
    replay = pf.run_portfolio(candidates, bars_by_id=bars_by_id, quotes_by_id=quotes,
                              replay_config=baseline_config, costs=custos)
    candidate_replay = pf.run_portfolio(candidates, bars_by_id=bars_by_id,
                                        quotes_by_id=quotes,
                                        replay_config=candidate_config, costs=custos)
    # Linha do tempo do capital: prova de que o sizing usou o capital do
    # INSTANTE da entrada e o resultado entrou no instante da saída.
    linha_do_tempo = [{"opportunity_id": trade["opportunity_id"],
                       "effective_ts_ms": trade["effective_ts_ms"],
                       "exit_ts_ms": trade["exit_ts_ms"],
                       "risk_usd": trade["risk_usd"],
                       "capital_at_entry_usd": trade["capital_at_entry_usd"],
                       "capital_after_usd": trade["capital_after_usd"]}
                      for trade in replay["trades"] if trade["admitted"]]
    report["replay"] = {"admitted": replay["admitted"], "rejected": replay["rejected"],
                        "metrics": replay["metrics"],
                        "capital_timeline": linha_do_tempo,
                        "fidelity_unavailable": replay["fidelity"]["unavailable"],
                        "live_equivalent": replay["live_equivalent"]}
    report["candidate_replay"] = {
        "kind": "MANAGEMENT_ONLY",
        "comparison_scope": report["comparison"]["scope"],
        "admitted": candidate_replay["admitted"],
        "metrics": candidate_replay["metrics"],
        "config_diff": {campo: [getattr(baseline_config, campo), getattr(candidate_config, campo)]
                        for campo in ("tp1_fraction", "trail_atr_multiple", "be_lock_fraction")
                        if getattr(baseline_config, campo) != getattr(candidate_config, campo)},
        "executed": True}

    # 6. Comparação da política inteira pelo runner de walk-forward.
    decision_at = {item["opportunity_id"]: item["decision_ts_ms"] for item in candidates}
    def lado_do_estudo(execucao):
        """Linhas do estudo com a DISPONIBILIDADE do resultado, não só a decisão:
        o treino de cada dobra só pode consumir label que já existia no corte."""
        return [{"opportunity_id": trade["opportunity_id"], "net_r": trade["net_r"],
                 "decision_ts_ms": decision_at.get(trade["opportunity_id"]),
                 "result_available_ts_ms": trade.get("result_available_ts_ms")}
                for trade in execucao["trades"] if trade["admitted"]]

    baseline = lado_do_estudo(replay)
    # O lado CANDIDATO vem do replay do candidato — resultado EXECUTADO, não
    # baseline mais um bônus inventado. Delta positivo, negativo ou zero é o que
    # o motor produzir; nada aqui garante vantagem ao candidato.
    candidate_side = lado_do_estudo(candidate_replay)
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
                              # A seleção do treino governa a política avaliada.
                              "folds_running_candidate": study["folds_running_candidate"],
                              "selection_governs_evaluation": study["selection_governs_evaluation"],
                              "stages": study["stages"],
                              "train_labels_withheld": sum(
                                  int(item.get("train_labels_withheld") or 0)
                                  for item in study["folds"]),
                              "promotable": study["verdict"]["promotable"]}
    # 7. Go/no-go alimentado pelos RESULTADOS calculados (nunca números soltos).
    from services import preselection_experiment_service as r12
    trades_com_playbook = []
    playbook_por_chave = {item["opportunity_key"]: item["playbook"] for item in eligible}
    for trade in replay["trades"]:
        trades_com_playbook.append({**trade,
                                    "playbook": playbook_por_chave.get(trade["opportunity_id"])})
    evidencia = r12.gate_evidence_from_study(
        replay=replay, study=study, trades=trades_com_playbook,
        enabled_playbooks=sorted({item["playbook"] for item in eligible}),
        window_start_ms=min((item["decision_ts_ms"] for item in candidates), default=None),
        window_end_ms=max((item["decision_ts_ms"] for item in candidates), default=None),
        essential_gaps=["prospective_sample"])
    gate = r12.go_no_go(evidencia)
    report["gate"] = {"verdict": gate["verdict"], "live_approval": gate["live_approval"],
                      "reason_codes": list(gate["reason_codes"]),
                      "criteria_hash": gate["criteria_hash"][:12],
                      "evidence_from_computed_results":
                          evidencia["source"]["derived_from_computed_results"]}
    # 8. R11 — estado da simulação CONSUMIDO: carrega, avança a histerese real
    # com a evidência calculada acima e publica a geração. Sem banco declarado,
    # a etapa diz que foi pulada (nunca finge que rodou).
    report["policy_simulation"] = await simulate_policy_state(
        args, study=study, replay=replay, candidate_replay=candidate_replay,
        candidate_config_diff=report["candidate_replay"]["config_diff"],
        gate=gate, candidates=candidates)

    report["next_step"] = ("Acumular amostra prospectiva pelo coletor ligado antes de "
                           "qualquer go/no-go; o adaptador operacional continua por implementar.")
    return report


async def exportar_observadas(eligible) -> tuple:
    """Relê do BANCO, pelo exportador R10B real, as linhas pré-seleção aceitas.

    Sem coleta ligada o export vem vazio — e isso é DECLARADO; a entrada do
    replay então continua sendo a decisão em memória, nunca uma linha inventada.
    """
    import time
    from db import get_session
    from services import research_dataset_scopes as scopes
    from services import research_dataset_service as ds
    if not eligible:
        return [], {"state": "NO_DECISIONS", "rows": 0}
    # A coorte aceita é indexada pelo instante em que foi OBSERVADA (o acervo
    # guarda a decisão, não o caminho do preço), então a janela acompanha o
    # relógio da observação — não o t0 sintético da série.
    agora = int(time.time() * 1000)
    inicio = (agora // BAR5) * BAR5 - 400 * BAR5
    as_of_ms = agora + BAR5
    replay_cfg = {"bar_ms": BAR5, "entry_window_bars": 3, "pre_tp1_time_stop_bars": 12,
                  "max_holding_bars": 24, "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
                  "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5, "max_bars": 96}
    pedido = {
        "as_of_utc": ds.ms_datetime(as_of_ms).isoformat().replace("+00:00", "Z"),
        "split": {"train_start_ms": inicio, "validation_start_ms": inicio + 200 * BAR5,
                  "holdout_start_ms": agora + 400 * BAR5, "purge_bars": 1},
        "baseline_config": dict(replay_cfg), "scope": scopes.SCOPE_PRE_ACCEPTED,
        "candidate": {"candidate_id": "RESEARCH-PIPELINE", "registered_at_ms": inicio - BAR5,
                      "kind": "MANAGEMENT_ONLY", "replay_config": dict(replay_cfg)},
        "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0,
                  "funding_bps_per_bar": 1.0},
        "bootstrap": {"seed": 5, "samples": 100, "block_size": 1}}
    try:
        async with get_session() as session:
            dataset, manifest = await ds.load_dataset(session, ds.parse_request(pedido))
    except Exception as exc:  # noqa: BLE001
        return [], {"state": "ERROR", "rows": 0, "error": type(exc).__name__}
    linhas = []
    for row in dataset.get("rows") or ():
        if any(row.get(campo) is None for campo in
               ("symbol", "side", "entry", "stop_loss", "tp1", "tp2", "decision_ts_ms")):
            continue      # linha incompleta não vira candidato
        linhas.append({"opportunity_key": row["opportunity_key"], "symbol": row["symbol"],
                       "side": row["side"], "decision_ts_ms": row["decision_ts_ms"],
                       "entry": row["entry"], "stop_loss": row["stop_loss"],
                       "tp1": row["tp1"], "tp2": row["tp2"]})
    return linhas, {"state": manifest["state"], "rows": len(linhas),
                    "scope": manifest["source"]["cohort"],
                    "exporter_schema": dataset["exporter_schema"]}


#: Hipótese AUTORIZADA a ser comparada (baseline × candidata congeladas). Os
#: contratos do lote declaram o champion LIVE e as políticas novas como
#: INATIVAS e `approved_for_production=false`; nenhum deles declara um PAR
#: autorizado para a comparação econômica. Sem essa decisão registrada, o
#: pipeline NÃO escolhe uma hipótese por conta própria.
AUTHORIZED_PAIR_REASON = "AUTHORIZED_CANDIDATE_NOT_DECLARED"
AUTHORIZED_PAIR_DECISION = [
    "Qual baseline congelada: champion LIVE (CHAMPION_LEGACY/SCORE_V2) ou a "
    "configuração default do núcleo R07D?",
    "Qual candidata congelada: núcleo R07D + Score V3 (hoje inativos) ou outra "
    "política já versionada?",
    "Qual escopo/população e quais custos comparáveis valem para o par.",
]


def authorized_comparison() -> dict:
    """Par AUTORIZADO declarado nos contratos — ou a ausência dele, explícita.

    Enquanto nenhum contrato registrar o par, esta função devolve BLOQUEADO com
    a decisão necessária. Inventar uma hipótese aqui trocaria silenciosamente o
    objeto da comparação, que é exatamente o defeito relatado.
    """
    return {"available": False, "state": "BLOCKED_MISSING_DECISION",
            "reason_code": AUTHORIZED_PAIR_REASON,
            "decision_required": list(AUTHORIZED_PAIR_DECISION)}


def comparacao_declarada(baseline_config, candidate_config, *, custos, core, score) -> dict:
    """Identidade e ESCOPO do contraste executado, registrados antes do resultado."""
    from services import preselection_experiment_service as r12
    baseline_manifest = baseline_config.manifest()
    candidate_manifest = candidate_config.manifest()
    bundle = r12.freeze_bundle({
        "baseline": baseline_manifest["config_hash"],
        "candidate": candidate_manifest["config_hash"],
        "config": {"core_version": core.CORE_VERSION,
                   "core_config_hash": core.DEFAULT_CONFIG.config_hash(),
                   "score_version": score.SCORE_VERSION},
        "costs": custos.manifest()["config_hash"],
        "protections": {"stop": "STRUCTURAL", "tp1_fraction_from_config": True}})
    return {
        "scope": "MANAGEMENT_ONLY",
        "baseline_config_hash": baseline_manifest["config_hash"],
        "candidate_config_hash": candidate_manifest["config_hash"],
        "config_diff": {campo: [getattr(baseline_config, campo),
                                getattr(candidate_config, campo)]
                        for campo in ("tp1_fraction", "trail_atr_multiple",
                                      "be_lock_fraction")
                        if getattr(baseline_config, campo) != getattr(candidate_config, campo)},
        "bundle_hash": bundle.get("bundle_hash"),
        "identity_registered_before_results": True,
        "proves": ["efeito da GESTÃO DE SAÍDAS sobre as MESMAS oportunidades"],
        "does_not_prove": ["núcleo R07D", "Score V3", "playbooks",
                           "seleção de oportunidades"],
        "decision_source_both_sides": "strategy_core_service.decide (mesma lista)",
        "authorized_hypothesis": authorized_comparison(),
    }


def evidence_key_of(*parts) -> str:
    """Chave da EVIDÊNCIA: muda quando o resultado calculado muda, e só então."""
    import hashlib
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


async def simulate_policy_state(args, *, study, replay, candidate_replay,
                                candidate_config_diff, gate, candidates) -> dict:
    """Executa a política/histerese REAL sobre a evidência recém-calculada.

    A identidade é (experimento, versão da política, universo, população) e o
    relógio é o INSTANTE DA EVIDÊNCIA — a última decisão da janela —, nunca o
    relógio de parede: resultado futuro não entra em decisão passada. Estado
    ilegível NÃO é primeiro estado: nesse caso nada avança.
    """
    from services import policy_state_service as ps
    from services import robust_policy_service as rp

    # UNIVERSO = quais símbolos existem. A semente muda a realização de mercado
    # (a evidência), não o universo — senão cada rodada seria outro universo e a
    # histerese nunca acumularia período.
    universe_version = f"SYN-{args.symbols}"
    # O experimento é a POLÍTICA candidata, não o instante da rodada: incluir o
    # relógio na identidade criaria um experimento novo a cada execução e a
    # histerese nunca retomaria.
    experiment_key = f"research-{evidence_key_of(candidate_config_diff)[:12]}"
    evidence_key = evidence_key_of(
        study["verdict"]["state"], study["verdict"]["winner"],
        study["policy_delta"]["value"], replay["metrics"]["net_total_r"],
        candidate_replay["metrics"]["net_total_r"], gate["verdict"])
    now_ms = max((item["decision_ts_ms"] for item in candidates), default=int(args.t0))
    identidade = {"experiment_key": experiment_key, "universe_version": universe_version,
                  "population": rp.POPULATION_SHADOW}
    resumo = {"identity": {**identidade, "policy_version": rp.POLICY_VERSION},
              "evidence_key": evidence_key,
              "period_key": rp.period_key(now_ms, period_seconds=PERIOD_SECONDS),
              "persisted": False}
    if not args.persist:
        return {**resumo, "state": "SKIPPED", "reason_code": "PERSISTENCE_NOT_REQUESTED"}
    try:
        import db
        if not db.DB_ENABLED:
            return {**resumo, "state": "SKIPPED", "reason_code": "DB_DISABLED"}
        await db.init_db()
        leitura = await ps.read_state(db.get_session, **identidade)
        if not leitura["available"]:
            # Falha de leitura NUNCA vira "primeiro estado": não avança nada.
            return {**resumo, "state": "UNAVAILABLE", "reason_code": leitura["reason_code"]}
        anterior = (leitura["state"] or {}).get("payload") or {}
        guardado = anterior.get("hysteresis") if isinstance(anterior, dict) else None
        progresso = None
        if isinstance(guardado, dict) and guardado.get("symbol"):
            progresso = rp.HysteresisProgress(
                symbol=guardado.get("symbol"), action=guardado.get("action"),
                periods=int(guardado.get("periods") or 0),
                last_period=guardado.get("last_period"),
                last_evidence=guardado.get("last_evidence"),
                universe_source=guardado.get("universe_source"))
        acao = ("PROMOTE_CANDIDATE" if study["verdict"]["winner"] == "CANDIDATE"
                else "HOLD")
        avancado, veredito = rp.advance_hysteresis(
            progresso, symbol=POLICY_SCOPE, action=acao, now_ms=now_ms,
            period_seconds=PERIOD_SECONDS, evidence_key=evidence_key,
            required_periods=REQUIRED_PERIODS, universe_source=universe_version)
        payload = {"hysteresis": {"symbol": avancado.symbol, "action": avancado.action,
                                  "periods": avancado.periods,
                                  "last_period": avancado.last_period,
                                  "last_evidence": avancado.last_evidence,
                                  "universe_source": avancado.universe_source},
                   "ready": bool(veredito["ready"]),
                   "gate_verdict": gate["verdict"],
                   "study_winner": study["verdict"]["winner"]}
        # A publicação é um CAS pela geração que este cálculo LEU: se outro
        # processo avançou o estado no meio, a escrita é recusada e a rodada
        # recalcula a partir da geração nova (uma vez, sem contar evidência
        # duas vezes).
        lida = int((leitura["state"] or {}).get("generation") or 0)
        publicacao = await ps.publish_generation(
            db.get_session, period_key=rp.period_key(now_ms, period_seconds=PERIOD_SECONDS),
            evidence_key=evidence_key, now_ms=now_ms, payload=payload,
            expected_generation=lida, **identidade)
        recalculos = 0
        while (publicacao.get("reason_code") == "GENERATION_STALE"
               and recalculos < MAX_RECALCULOS):
            recalculos += 1
            releitura = await ps.read_state(db.get_session, **identidade)
            if not releitura["available"]:
                return {**resumo, "state": "UNAVAILABLE",
                        "reason_code": releitura["reason_code"],
                        "recalculations": recalculos}
            atual = releitura["state"] or {}
            guardado = (atual.get("payload") or {}).get("hysteresis")
            progresso = (rp.HysteresisProgress(
                symbol=guardado.get("symbol"), action=guardado.get("action"),
                periods=int(guardado.get("periods") or 0),
                last_period=guardado.get("last_period"),
                last_evidence=guardado.get("last_evidence"),
                universe_source=guardado.get("universe_source"))
                if isinstance(guardado, dict) and guardado.get("symbol") else None)
            avancado, veredito = rp.advance_hysteresis(
                progresso, symbol=POLICY_SCOPE, action=acao, now_ms=now_ms,
                period_seconds=PERIOD_SECONDS, evidence_key=evidence_key,
                required_periods=REQUIRED_PERIODS, universe_source=universe_version)
            payload = {**payload,
                       "hysteresis": {"symbol": avancado.symbol, "action": avancado.action,
                                      "periods": avancado.periods,
                                      "last_period": avancado.last_period,
                                      "last_evidence": avancado.last_evidence,
                                      "universe_source": avancado.universe_source},
                       "ready": bool(veredito["ready"])}
            publicacao = await ps.publish_generation(
                db.get_session,
                period_key=rp.period_key(now_ms, period_seconds=PERIOD_SECONDS),
                evidence_key=evidence_key, now_ms=now_ms, payload=payload,
                expected_generation=int(atual.get("generation") or 0), **identidade)
        return {**resumo,
                "state": "EXECUTED",
                "recalculations": recalculos,
                "resumed_from_state": bool(progresso is not None),
                "previous_generation": (leitura["state"] or {}).get("generation"),
                "periods": veredito["periods"],
                "hysteresis_reason_code": veredito["reason_code"],
                "ready": bool(veredito["ready"]),
                "required_periods": REQUIRED_PERIODS,
                "published": bool(publicacao["published"]),
                "generation": publicacao["generation"],
                "publication_reason_code": publicacao["reason_code"],
                "persisted": bool(publicacao["published"]),
                "applies_live": False}
    except Exception as exc:  # noqa: BLE001
        return {**resumo, "state": "ERROR", "reason_code": type(exc).__name__,
                "error": str(exc)[:200]}


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
