"""Lote 02 — PostgreSQL DESCARTÁVEL: coleta v2, export real, estudo de SELEÇÃO.

`LOTE02_TEST_SOCKET` aponta para /tmp/cw-lote02-sock.* criado pelo runner.
Driver async real, socket Unix, TCP/DNS bloqueados, nenhuma exchange: a única
fronteira simulada é o MERCADO (os sinais do scanner).

O que este harness prova, no banco:
  • linha `PRE_SELECTION` v2 (com features ponto-no-tempo) persiste, volta pelo
    EXPORTADOR R10B real e alimenta a comparação de SELEÇÃO;
  • o estudo SELECTION_ONLY atravessa contrato V2 → verificador → catálogo, e
    sobrevive a RESTART (sessão nova, processo novo de leitura);
  • CONCORRÊNCIA: duas conexões publicando a mesma geração — uma vence, a outra
    é recusada por CAS, sem contar evidência duas vezes;
  • identidade: estudo de gestão não passa como estudo de seleção.
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import sys
from unittest.mock import patch

test_socket = os.environ.get("LOTE02_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-lote02-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://lote02@/lote02db?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
os.environ["R09_PRESELECTION_MODE"] = "observe"      # coleta ligada SÓ no teste
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste do Lote 02")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste do Lote 02")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste do Lote 02")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


BAR5 = 300_000


async def run():
    import db
    from sqlalchemy import func, select
    from models.decision_observation import DecisionObservation
    from models.policy_simulation_state import PolicySimulationState
    from services import decision_observation_service as obs
    from services import policy_state_service as ps
    from services import preselection_experiment_service as r12
    from services import preselection_observation_service as pre
    from services import research_dataset_scopes as scopes
    from services import research_dataset_service as ds
    from services import research_manifest_service as rm
    from services import research_selection_service as rsel
    from services import robust_policy_service as rp
    from services import score_v3_service as s3
    from services import strategy_core_service as core
    from services import strategy_evidence_service as evid

    await db.init_db()
    for _ in range(2):          # criação aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[PolicySimulationState.__table__])

    agora_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    decisao_ms = (agora_ms // BAR5) * BAR5 - 50 * BAR5

    # ══════════════════════════════════════════════════════════════════════
    #  1. Coleta v2 → banco → EXPORTADOR real → comparação de seleção
    # ══════════════════════════════════════════════════════════════════════
    def features_v3(**mudancas):
        base = {"adx": 30.0, "htf_alignment_ratio": 1.0, "structure_quality": 0.8,
                "level_distance_atr": 0.5, "trigger_body_ratio": 0.7,
                "trigger_follow_through_atr": 0.4, "rr_tp2": 2.5,
                "entry_distance_atr": 0.1, "volume_ratio": 1.1,
                "spread_pct": 0.03, "funding_pct": 0.0, "score": 73.0}
        base.update(mudancas)
        return base

    def linha_observada(indice: int, *, aceita: bool, timeframe: str = "1h",
                        com_features: bool = True) -> dict:
        etapas = [{"stage": pre.STAGE_CANDIDATE, "verdict": pre.VERDICT_PASSED},
                  {"stage": pre.STAGE_PLAYBOOK, "verdict": pre.VERDICT_PASSED},
                  {"stage": pre.STAGE_CANDLE, "verdict": pre.VERDICT_PASSED}]
        etapas.append({"stage": pre.STAGE_SELECTION,
                       "verdict": pre.VERDICT_PASSED if aceita else pre.VERDICT_REJECTED,
                       "reason_code": None if aceita else "TIER_BELOW_MINIMUM"})
        if aceita:
            etapas.append({"stage": pre.STAGE_GEOMETRY_RR,
                           "verdict": pre.VERDICT_PASSED})
        linha = {
            "setup": {"symbol": f"SYN{indice}/USDT:USDT", "timeframe": timeframe,
                      "side": "long", "playbook": "CHAMPION_LEGACY",
                      "playbook_version": "SCORE_V2",
                      "trigger_candle_ms": decisao_ms - BAR5,
                      "entry": 100.0, "stop_loss": 99.0, "tp1": 103.0,
                      "tp2": 106.0, "atr": 1.0},
            "stages": etapas, "accepted": aceita,
            "decision_ts_ms": decisao_ms + indice,
            "availability": {"score": True, "candle": True, "depth": False},
            "source": {"decision_source": "server_scan", "resolution": timeframe},
        }
        if com_features:
            from tests.test_lote02_research_manifest import champion_trace
            linha["score_trace"] = champion_trace()
            linha["observed_decision_scope"] = "FINAL_SCANNER_SELECTION"
            # v2: features ponto-no-tempo + bloco de avaliação (TFs avaliados).
            linha["features"] = features_v3()
            linha["evaluation"] = {"evaluated_timeframes": ["1h", "4h"],
                                   "selected_timeframe": "1h",
                                   "is_selected_timeframe": timeframe == "1h"}
        return linha

    linhas = [linha_observada(i, aceita=i % 2 == 0) for i in range(8)]
    linhas.append(linha_observada(99, aceita=True, com_features=False))  # v1
    class ObservationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # A captura oficial acontece no instante deste cenário, NÃO no
            # relógio da execução do harness. Nada é retrodado depois do save.
            instant = ds.ms_datetime(decisao_ms + 1000)
            return instant if tz is None else instant.astimezone(tz)

    with patch.object(obs, "datetime", ObservationClock):
        resumo = obs.observe_preselection(linhas)
    check("coleta_registra_aceitas_e_vetadas",
          resumo["accepted"] == 5 and resumo["vetoed"] == 4, str(resumo))
    gravadas = await obs.flush_pending()
    async with db.get_session() as session:
        total_pre = int((await session.execute(
            select(func.count(DecisionObservation.opportunity_key)).where(
                DecisionObservation.scope == "PRE_SELECTION"))).scalar() or 0)
    check("linhas_pre_selecao_persistem", total_pre >= 5,
          f"gravadas={gravadas} total={total_pre}")

    async with db.get_session() as session:
        versoes = (await session.execute(select(
            DecisionObservation.frozen_config).where(
            DecisionObservation.scope == "PRE_SELECTION"))).scalars().all()
    marcadas = [(v or {}).get("r09_pre_selection", {}).get("schema_version")
                for v in versoes]
    check("as_duas_versoes_do_payload_convivem",
          pre.PRE_SCHEMA_VERSION_V2 in marcadas and pre.PRE_SCHEMA_VERSION in marcadas,
          str(sorted(set(marcadas))))

    # Export REAL da população bruta: aceitas E vetadas, sem decoração externa.
    replay_cfg = {"bar_ms": BAR5, "entry_window_bars": 3,
                  "pre_tp1_time_stop_bars": 12, "max_holding_bars": 24,
                  "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
                  "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5,
                  "max_bars": 96}
    inicio = decisao_ms - 400 * BAR5
    pedido = {
        "as_of_utc": ds.ms_datetime(agora_ms).isoformat().replace("+00:00", "Z"),
        "split": {"train_start_ms": inicio,
                  "validation_start_ms": inicio + 200 * BAR5,
                  "holdout_start_ms": agora_ms - BAR5, "purge_bars": 1,
                  "embargo_bars": 0},
        "baseline_config": dict(replay_cfg),
        "scope": scopes.SCOPE_PRE_POPULATION,
        "candidate": {"candidate_id": "L02-SELECTION",
                      "registered_at_ms": inicio - BAR5,
                      "kind": "SELECTION_ONLY",
                      "replay_config": dict(replay_cfg),
                      "selection": {
                          "core_version": core.CORE_VERSION,
                          "core_config_hash": core.DEFAULT_CONFIG.config_hash(),
                          "score_version": s3.SCORE_VERSION,
                          "score_config_hash": s3.model_fingerprint(
                              playbook=core.PLAYBOOK_TREND_PULLBACK,
                              config=s3.DEFAULT_CONFIG),
                          "score_config": s3.DEFAULT_CONFIG.as_dict(),
                          "playbooks": list(core.PLAYBOOKS),
                          "selection_rule": {"kind": rm.RULE_SCORE_V3_MIN,
                                             "min_score": 60.0,
                                             "playbook": core.PLAYBOOK_TREND_PULLBACK,
                                             "source": s3.SCORE_VERSION}}},
        "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0,
                  "funding_bps_per_bar": 1.0},
        "bootstrap": {"seed": 5, "samples": 100, "block_size": 1}}
    requisicao = ds.parse_request(pedido)
    check("pedido_de_selecao_e_aceito_pelo_exportador",
          requisicao.candidate.kind == "SELECTION_ONLY"
          and requisicao.changed == (), str(requisicao.changed))
    async with db.get_session() as session:
        dataset, manifest_export = await ds.load_dataset(session, requisicao)
    exportadas = list(dataset.get("rows") or ())
    check("export_real_traz_as_features_ponto_no_tempo",
          bool(exportadas)
          and any(linha.get("features") for linha in exportadas)
          and any(linha.get("features") is None
                  and linha.get("features_reason") == ds.FEATURES_NOT_COLLECTED
                  for linha in exportadas),
          f"{manifest_export['state']} rows={len(exportadas)}")
    check("manifesto_do_export_declara_as_versoes_aceitas",
          list(manifest_export["source"]["source_schemas_accepted"])
          == list(ds.PRE_SOURCE_SCHEMAS), str(manifest_export["source"]))

    # Comparação de SELEÇÃO sobre as linhas EXPORTADAS do banco.
    def manifesto_selecao(min_score=60.0, *,
                          decision_state=rm.DECISION_TEST_ONLY) -> dict:
        from tests.test_lote02_research_manifest import manifesto as base
        corpo = base(decision_state=decision_state)
        corpo["population"].update(universe_version="SYN-L02", min_rows=1,
                                     cohort=scopes.SCOPE_PRE_POPULATION,
                                     scope_id=scopes.SCOPE_PRE_POPULATION)
        corpo["split"] = {**pedido["split"], "as_of_ms": agora_ms,
                           "embargo_bars": 0}
        corpo["candidate"]["selection_rule"] = {
            "kind": rm.RULE_SCORE_V3_MIN, "min_score": min_score,
            "playbook": core.PLAYBOOK_TREND_PULLBACK, "source": s3.SCORE_VERSION}
        return rm.parse_manifest(corpo)

    populacao = exportadas
    check("export_preserva_decisoes_aceitas_e_vetadas_sem_decoracao",
          {linha.get("observed_outcome") for linha in populacao} == {"ACCEPTED", "VETOED"})
    comparacao = rsel.compare_population(populacao, manifest=manifesto_selecao())
    check("selecao_decide_pelos_dois_lados_sobre_a_populacao_do_banco",
          comparacao["ok"] and comparacao["selected"]["baseline"]
          and comparacao["selected"]["candidate"], str(comparacao.get("coverage")))
    apertada = rsel.compare_population(populacao,
                                       manifest=manifesto_selecao(min_score=95.0))
    check("corte_mais_alto_muda_a_selecao_de_verdade",
          apertada["selected"]["candidate"] == []
          and apertada["selected"]["baseline"] == comparacao["selected"]["baseline"],
          str(apertada["counts"]))
    sem_features = [linha for linha in populacao if linha.get("features") is None]
    check("linha_v1_sem_features_fica_unknown_simetrico",
          bool(sem_features)
          and comparacao["coverage"]["excluded_symmetric"] >= len(sem_features),
          str(comparacao["coverage"]["excluded_reasons"]))

    # ══════════════════════════════════════════════════════════════════════
    #  2. Estudo SELECTION_ONLY: contrato V2 → verificador → catálogo
    # ══════════════════════════════════════════════════════════════════════
    manifesto = manifesto_selecao()
    selecao_config = rm.selection_config_of(manifesto["candidate"])
    gestao_manifest = manifesto["baseline"]["management_config"]
    from services import offline_replay_service as r10a
    custos = r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                             funding_bps_per_bar=1.0).manifest()
    contrato = r12.preselection_contract(
        population=rp.POPULATION_SHADOW, study_kind="PRE_SELECTION",
        policy_version=manifesto["candidate"]["policy_version"], universe_version="SYN-L02",
        comparison_scope=rm.SCOPE_SELECTION, baseline_config=gestao_manifest,
        candidate_config=gestao_manifest, costs_config=custos,
        bundle_hash=manifesto["hashes"]["bundle_hash"],
        dataset_fingerprint=manifest_export["fingerprints"]["dataset_sha256"],
        cutoff_ms=manifesto["split"]["as_of_ms"], manifest_hash=manifesto["manifest_hash"],
        selection_config=selecao_config, research_manifest=manifesto,
        dataset_scope=dataset["scope"], temporal_split=manifesto["split"])
    check("contrato_de_selecao_nasce_v2",
          contrato["contract_version"] == r12.PRE_SELECTION_CONTRACT_V2
          and contrato["manifest_hash"] == manifesto["manifest_hash"],
          contrato["contract_version"])
    envelope = r12.build_preselection_envelope(
        replay_config=gestao_manifest, contract_hash=contrato["contract_hash"],
        selection_config=selecao_config)
    estudo = r12.study_payload(
        contract=contrato, evidence={"total_shadow_trades": 0},
        gate={"verdict": "NO_GO"}, study={"verdict": {"state": "INSUFFICIENT_EVIDENCE"}},
        replay={"admitted": 0}, evidence_key="e" * 32)

    identidade = {"experiment_key": "lote02-selection",
                  "universe_version": "SYN-L02",
                  "population": rp.POPULATION_SHADOW}
    periodo = rp.period_key(decisao_ms, period_seconds=86_400)
    publicacao = await ps.publish_generation(
        db.get_session, period_key=periodo, evidence_key="e" * 32,
        now_ms=decisao_ms, payload={"study": estudo}, expected_generation=0,
        **identidade)
    check("estudo_de_selecao_persiste", publicacao["published"]
          and publicacao["generation"] == 1, str(publicacao))

    # RESTART: sessão nova, leitura do banco, verificação do estudo recuperado.
    await db._engine.dispose()
    leitura = await ps.read_state(db.get_session, **identidade)
    recuperado = ((leitura["state"] or {}).get("payload") or {}).get("study") or {}
    verdict = evid.verify_study_identity(
        recuperado, candidate_config=envelope,
        fingerprint=contrato["dataset_fingerprint"],
        cutoff=datetime.fromtimestamp(contrato["cutoff_ms"] / 1000, tz=timezone.utc))
    check("estudo_recuperado_apos_restart_e_verificado",
          verdict["ok"], str(verdict))
    envelope_gestao = r12.build_preselection_envelope(
        replay_config=gestao_manifest, contract_hash=contrato["contract_hash"])
    verdict_errado = evid.verify_study_identity(
        recuperado, candidate_config=envelope_gestao,
        fingerprint=contrato["dataset_fingerprint"],
        cutoff=datetime.fromtimestamp(contrato["cutoff_ms"] / 1000, tz=timezone.utc))
    check("estudo_de_selecao_nao_passa_como_estudo_de_gestao",
          not verdict_errado["ok"]
          and verdict_errado["reason_code"] == r12.SCOPE_CONTRACT_MISMATCH,
          str(verdict_errado))
    adulterado = {**recuperado,
                  "contract": {**recuperado["contract"],
                               "manifest_hash": "0" * 64}}
    verdict_adulterado = evid.verify_study_identity(
        adulterado, candidate_config=envelope,
        fingerprint=contrato["dataset_fingerprint"],
        cutoff=datetime.fromtimestamp(contrato["cutoff_ms"] / 1000, tz=timezone.utc))
    check("manifesto_adulterado_bloqueia_antes_do_outcome",
          not verdict_adulterado["ok"]
          and verdict_adulterado["reason_code"] == r12.CONTRACT_INVALID,
          str(verdict_adulterado))

    # ══════════════════════════════════════════════════════════════════════
    #  3. CONCORRÊNCIA: duas conexões, uma geração — CAS decide
    # ══════════════════════════════════════════════════════════════════════
    periodo2 = rp.period_key(decisao_ms + 86_400_000, period_seconds=86_400)
    primeira, segunda = await asyncio.gather(
        ps.publish_generation(db.get_session, period_key=periodo2,
                              evidence_key="g1" * 16, now_ms=decisao_ms + 86_400_000,
                              payload={"study": estudo, "worker": "A"},
                              expected_generation=1, **identidade),
        ps.publish_generation(db.get_session, period_key=periodo2,
                              evidence_key="g2" * 16, now_ms=decisao_ms + 86_400_000,
                              payload={"study": estudo, "worker": "B"},
                              expected_generation=1, **identidade))
    publicadas = [r for r in (primeira, segunda) if r.get("published")]
    recusadas = [r for r in (primeira, segunda) if not r.get("published")]
    check("concorrencia_publica_uma_geracao_so",
          len(publicadas) == 1 and len(recusadas) == 1,
          f"{primeira.get('reason_code')} / {segunda.get('reason_code')}")
    check("recusa_declara_o_motivo_do_cas",
          recusadas[0].get("reason_code") in ("GENERATION_STALE", "PERIOD_UNCHANGED"),
          str(recusadas[0]))
    async with db.get_session() as session:
        linhas_estado = int((await session.execute(
            select(func.count(PolicySimulationState.id)))).scalar() or 0)
    check("uma_linha_por_identidade_mesmo_com_concorrencia",
          linhas_estado == 1, str(linhas_estado))
    final = await ps.read_state(db.get_session, **identidade)
    check("geracao_final_e_dois", int((final["state"] or {}).get("generation") or 0) == 2,
          str((final["state"] or {}).get("generation")))

    print(f"LOTE02_PESQUISA_PG_OK: {len(CHECKS)} verificações")
    await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
