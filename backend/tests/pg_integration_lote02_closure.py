"""Lote 02: banco oficial → export → replay/fitting/OOS → CAS → releitura.

Somente TEST_ONLY e mercado sintético explicitamente selado. O guard Unix-only
é o mesmo do harness anterior, executado primeiro; nenhum outcome do holdout
pode sequer passar pelo decodificador JSONB do driver.
"""
import asyncio
import copy
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import pg_integration_lote02_pesquisa as previous  # installs guard

CHECKS = []
POISON = "LOTE02_HOLDOUT_JSON_MUST_NOT_MATERIALIZE"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    CHECKS.append(name)
    print(f"  ✓ {name}")


def sealed_json_decoder(raw):
    if POISON in raw:
        raise AssertionError("HOLDOUT_MATERIALIZED_BY_DRIVER")
    return json.loads(raw)


async def run():
    await previous.run()
    import db
    from sqlalchemy import func, select, text, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from models.decision_observation import DecisionObservation
    from models.policy_simulation_state import PolicySimulationState
    from services import policy_state_service as state
    from services import research_dataset_service as ds
    from services import research_study_service as study
    from services import research_manifest_service as rm
    from services import score_v3_calibration_service as calib
    from services import preselection_experiment_service as catalog
    from services import strategy_evidence_service as evidence
    from tests.test_lote02_official_study_cycle import official_fixture

    manifest, _, _, fixture_prices, export_request, source_rows = official_fixture(return_source=True)
    now_ms = manifest["split"]["as_of_ms"]
    check("manifesto_sintetico_nunca_autoriza_estudo_real",
          manifest["decision"]["state"] == rm.DECISION_TEST_ONLY
          and rm.authorized_comparison(manifest)["real_study_allowed"] is False)
    # Mesmos campos PRODUZIDOS pela captura oficial, sem decorar export/outcomes.
    async with db.get_session() as session:
        for row in source_rows:
            payload = row["frozen_config"]["r09_pre_selection"]
            session.add(DecisionObservation(
                opportunity_key=row["opportunity_key"], identity_source="SYNTHETIC_TEST_ONLY",
                scope=row["opportunity_scope"], symbol=row["symbol"],
                first_seen_at=row["decision_at"], last_seen_at=row["decision_at"],
                first_decision=payload["outcome"], first_decision_observed_at=row["decision_at"],
                first_blocker=None if payload["outcome"] == "ACCEPTED" else "SYNTHETIC_VETO",
                frozen_setup=copy.deepcopy(row["frozen_setup"]),
                frozen_config=copy.deepcopy(row["frozen_config"]),
                score_trace=copy.deepcopy(row["score_trace"])))
        holdout = copy.deepcopy(source_rows[0])
        holdout_at = ds.ms_datetime(manifest["split"]["holdout_start_ms"] + 5 * previous.BAR5)
        holdout["frozen_setup"]["never_read"] = POISON
        session.add(DecisionObservation(
            opportunity_key="pre-lote02-closure-holdout", identity_source="SYNTHETIC_TEST_ONLY",
            scope="PRE_SELECTION", symbol=holdout["symbol"],
            first_seen_at=holdout_at, last_seen_at=holdout_at,
            first_decision="ACCEPTED", first_decision_observed_at=holdout_at,
            first_blocker=None, frozen_setup=holdout["frozen_setup"],
            frozen_config=holdout["frozen_config"], score_trace=holdout["score_trace"]))
        await session.commit()
    check("semente_contem_aceitas_e_vetadas_da_mesma_captura",
          {r["frozen_config"]["r09_pre_selection"]["outcome"] for r in source_rows} == {"ACCEPTED", "VETOED"})

    read_engine = create_async_engine(previous.DB_URL, json_deserializer=sealed_json_decoder)
    read_factory = async_sessionmaker(read_engine, expire_on_commit=False)
    try:
        async with read_factory() as session:
            dataset, exported = await ds.load_dataset(session, export_request)
            rolled_back = not session.in_transaction()
        check("exportador_oficial_read_only_termina_sem_transacao", rolled_back)
        check("holdout_poison_contado_sem_materializar_JSONB",
              exported["holdout"]["details_read"] is False
              and exported["holdout"]["policy"] == "SEALED"
              and exported["counts"]["holdout_sealed"] >= 1
              and "pre-lote02-closure-holdout" not in {r["opportunity_key"] for r in dataset["rows"]})
        check("export_preserva_decisao_final_e_coortes_sem_decoracao",
              {r["observed_outcome"] for r in dataset["rows"]} == {"ACCEPTED", "VETOED"}
              and all(r["observed_decision_scope"] == "FINAL_SCANNER_SELECTION" for r in dataset["rows"]))
        check("sem_trajetoria_observada_nao_fabrica_outcome_ou_vela",
              all(r["outcome"] is None and r["candles"] is None for r in dataset["rows"]))
        prices = study.build_price_contract(
            dataset_hash=study.digest(dataset), source="SYNTHETIC_TEST_ONLY", bar_ms=previous.BAR5,
            as_of_ms=now_ms, windows=fixture_prices["windows"], quotes=fixture_prices["quotes"], test_only=True)
        request = study.calibration_request(event=calib.EVENT_NET_POSITIVE, valid_for_ms=86400000)
        report = study.run_study(manifest=manifest, dataset=dataset, export_manifest=exported,
                                 prices=prices, request=request, now_ms=now_ms)
        check("estudo_roda_motores_reais_sobre_hash_do_export_PG",
              report.get("ok") is True and report["identity"]["dataset_hash"] == study.digest(dataset), str(report.get("reason_code")))
        check("dois_replays_e_quatro_folds_reais",
              report["baseline_replay"]["admitted"] > 0 and report["candidate_replay"]["admitted"] > 0
              and len(report["calibration"]["folds"]) == 4)
        artifact = report["artifact"]
        check("fitting_produz_artefato_OOS_com_predicoes_e_evento_liquido",
              report["calibration"]["state"] == calib.STATE_OOS_VALIDATED
              and artifact["metrics"]["oos"]["predictions"] > 0
              and artifact["event"] == calib.EVENT_NET_POSITIVE)
        check("artefato_e_identidade_verificados_antes_de_consumir_probabilidade",
              calib.verify_artifact(artifact, now_ms=now_ms,
                  model_fingerprint=manifest["candidate"]["score_config_hash"],
                  score_config_hash=manifest["candidate"]["score_config_hash"],
                  dataset_hash=study.digest(dataset), event=calib.EVENT_NET_POSITIVE)["ok"])
        check("TEST_ONLY_e_OOS_nao_significam_aprovacao_economica",
              report["real_study_allowed"] is False and report["promotable"] is False
              and report["calibration"]["economically_approved"] is False)
        missing = study.run_study(manifest=manifest, dataset=dataset, export_manifest=exported,
                                  request=request, now_ms=now_ms)
        check("precos_ausentes_bloqueiam_sem_demo_substituta",
              missing.get("reason_code") == "PRICE_AND_QUOTE_WINDOWS_REQUIRED")
        empty = await study.load_latest_study(db.get_session, now_ms=now_ms)
        check("ausencia_real_de_estudo_nao_e_erro_de_banco",
              empty["available"] is True and empty["reason_code"] == "NO_CALIBRATION_STUDY")

        # Cada worker lê geração zero no banco. O CAS oficial decide o vencedor;
        # não simulamos publicação, SQL, lock ou transação.
        read_done, pid_ready = asyncio.Event(), asyncio.Event()
        read_count, backend_pids = 0, set()
        original_read = state.read_state

        @asynccontextmanager
        async def concurrent_factory():
            async with db.get_session() as session:
                backend_pids.add(await session.scalar(text("SELECT pg_backend_pid()")))
                if len(backend_pids) >= 2:
                    pid_ready.set()
                await asyncio.wait_for(pid_ready.wait(), timeout=10)
                yield session

        async def barrier_read(factory, **identity):
            nonlocal read_count
            result = await original_read(factory, **identity)
            read_count += 1
            if read_count == 2:
                read_done.set()
            await asyncio.wait_for(read_done.wait(), timeout=10)
            return result

        with patch.object(state, "read_state", side_effect=barrier_read):
            writes = await asyncio.gather(study.persist_study(concurrent_factory, report),
                                          study.persist_study(concurrent_factory, report))
        check("duas_conexoes_leem_mesma_geracao_e_so_uma_publica_CAS",
              len(backend_pids) >= 2 and sum(w["published"] is True for w in writes) == 1
              # O perdedor do CAS agora relê a linha: payload idêntico é
              # STUDY_UNCHANGED, não drift nem nova geração. A barreira e as
              # duas conexões continuam provando a disputa pelo primeiro CAS.
              and {w["reason_code"] for w in writes} == {"FIRST_GENERATION", "STUDY_UNCHANGED"}
              and all(w["generation"] == 1 for w in writes), str(writes))
        identity = dict(experiment_key="r13cal:" + report["study_key"][:32],
                        universe_version=manifest["population"]["universe_version"], population="SHADOW")
        before = await state.read_state(db.get_session, **identity)
        again = await study.persist_study(db.get_session, report)
        async with db.get_session() as session:
            count = await session.scalar(select(func.count(PolicySimulationState.id)).where(
                PolicySimulationState.experiment_key == identity["experiment_key"]))
        check("repeticao_mantem_uma_linha_e_geracao_um",
              again["reason_code"] == "STUDY_UNCHANGED" and count == 1
              and before["state"]["generation"] == 1)
        stored = copy.deepcopy(before["state"]["payload"])
        check("JSONB_preserva_corpo_artefato_e_hash_exatos",
              stored["artifact"] == artifact and stored["identity"] == report["identity"]
              and stored["study"]["contract"] == report["study"]["contract"])
        await db._engine.dispose()
        state._STATE_CACHE.clear()
        restored = await study.load_latest_study(db.get_session, now_ms=now_ms)
        check("restart_rele_artefato_sem_refitting_ou_geracao_nova",
              restored["available"] is True and restored["artifact"] == artifact
              and restored["calibration_state"] == calib.STATE_OOS_VALIDATED)
        loaded = await evidence.load_preselection_study(**identity)
        contract = stored["study"]["contract"]
        envelope = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
            contract_hash=contract["contract_hash"], selection_config=contract["selection_config"])
        verified = evidence.verify_study_identity(loaded["study"], candidate_config=envelope,
            fingerprint=study.digest(dataset), cutoff=ds.ms_datetime(now_ms))
        check("catalogo_confere_estudo_persistido_com_corpo_V2_e_TEST_ONLY",
              loaded["available"] is True and verified["ok"] is True
              and verified["real_study_allowed"] is False)
        changed = copy.deepcopy(envelope)
        changed["selection_config"]["score_config_hash"] = "0" * 64
        check("catalogo_recusa_config_diferente_do_estudo_congelado",
              evidence.verify_study_identity(loaded["study"], candidate_config=changed,
                  fingerprint=study.digest(dataset), cutoff=ds.ms_datetime(now_ms))["ok"] is False)

        async def replace_payload(payload):
            async with db.get_session() as session:
                await session.execute(update(PolicySimulationState).where(
                    PolicySimulationState.state_key == before["state"]["state_key"]).values(payload=payload))
                await session.commit()

        invalid = copy.deepcopy(stored)
        invalid["artifact"]["artifact_hash"] = "0" * 64
        await replace_payload(invalid)
        denied = await study.load_latest_study(db.get_session, now_ms=now_ms)
        check("artefatos_divergentes_nos_dois_locais_recusam_identidade",
              denied["available"] is False and denied["artifact"] is None
              and denied["reason_code"] == "CALIBRATION_STUDY_IDENTITY_INVALID")
        invalid["calibration"]["artifact"] = copy.deepcopy(invalid["artifact"])
        await replace_payload(invalid)
        denied = await study.load_latest_study(db.get_session, now_ms=now_ms)
        check("artefato_adulterado_no_banco_nao_vira_probabilidade",
              denied["available"] is False and denied["artifact"] is None
              and denied["reason_code"] == calib.ARTIFACT_INVALID)
        await replace_payload(stored)
        expired = await study.load_latest_study(db.get_session,
            now_ms=artifact["generation"]["valid_until_ms"] + 1)
        check("artefato_vencido_recarregado_e_recusado",
              expired["available"] is False and expired["artifact"] is None
              and expired["reason_code"] == calib.ARTIFACT_EXPIRED)
        revoked = copy.deepcopy(stored)
        revoked["artifact"] = calib.revoke(artifact, reason="TEST_ONLY_REVOCATION")["artifact"]
        revoked["calibration"]["artifact"] = copy.deepcopy(revoked["artifact"])
        await replace_payload(revoked)
        denied = await study.load_latest_study(db.get_session, now_ms=now_ms)
        check("revogacao_confirmada_no_banco_impede_reuso",
              denied["available"] is False and denied["artifact"] is None
              and denied["reason_code"] == calib.ARTIFACT_REVOKED)
        await replace_payload(stored)

        @asynccontextmanager
        async def database_error_factory():
            async with db.get_session() as session:
                await session.execute(text("SELECT lote02_column_that_does_not_exist FROM policy_simulation_state"))
                yield session

        failed = await study.load_latest_study(database_error_factory, now_ms=now_ms)
        check("erro_SQL_real_nao_e_NO_STUDY_nem_probabilidade_zero",
              failed["available"] is False and failed["artifact"] is None
              and failed["reason_code"] == "CALIBRATION_STUDY_READ_ERROR")
        check("guard_de_rede_anterior_permanece_instalado",
              socket.socket is previous.UnixOnlySocket and socket.getaddrinfo is previous.no_dns)
        check("schema_do_holdout_permanece_sem_outcomes_materializados",
              report["holdout_status"] == "SEALED" and report["calibration"]["holdout_status"] == "SEALED"
              and report["study"]["real_study_allowed"] is False)
    finally:
        await read_engine.dispose()
        await db._engine.dispose()
    print(f"LOTE02_CLOSURE_PG_OK: {len(CHECKS)} verificações + {len(previous.CHECKS)} regressões")


if __name__ == "__main__":
    asyncio.run(run())
