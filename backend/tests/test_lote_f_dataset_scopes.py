"""Bloco F — escopos do exportador R10B (aceitas, vetadas, candidatos estruturais).

Hermético: sem banco, sem rede, sem `.env`. Fixtures sintéticas versionadas.
O escopo legado tem de continuar idêntico — SQL e hash de requisição inclusive.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import os
import socket as _socket
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste do bloco F")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import research_dataset_scopes as scopes  # noqa: E402
from services import research_dataset_service as ds  # noqa: E402

BAR = 300_000
T0 = 1_760_000_100_000
VAL = T0 + 100 * BAR
HOLD = T0 + 200 * BAR
REPLAY = {"bar_ms": BAR, "entry_window_bars": 3, "pre_tp1_time_stop_bars": 12,
          "max_holding_bars": 24, "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
          "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5, "max_bars": 96}

# SQL do escopo legado, copiado do exportador ANTES desta extensão.
GOLDEN_INDEX = """
SELECT opportunity_key, decision_at
FROM rejected_setup_observations
WHERE decision_at >= :train_start AND decision_at < :index_end
ORDER BY decision_at, opportunity_key
LIMIT :row_limit
"""
GOLDEN_COUNTS = """
SELECT count(*) FILTER (WHERE decision_at < :train_start) AS before_training,
       count(*) FILTER (WHERE decision_at >= :holdout_start AND decision_at < :as_of) AS holdout_sealed,
       count(*) FILTER (WHERE decision_at >= :as_of) AS after_cutoff
FROM rejected_setup_observations
"""


def request_body(**over):
    body = {
        "as_of_utc": "2026-01-01T00:00:00Z",
        "split": {"train_start_ms": T0, "validation_start_ms": VAL,
                  "holdout_start_ms": HOLD, "purge_bars": 1},
        "baseline_config": dict(REPLAY),
        "candidate": {"candidate_id": "F-SYNTH-AA", "registered_at_ms": T0 - 1,
                      "kind": "MANAGEMENT_ONLY", "replay_config": dict(REPLAY)},
        "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0,
                  "funding_bps_per_bar": 1.0},
        "bootstrap": {"seed": 7, "samples": 100, "block_size": 1},
    }
    body.update(over)
    return body


def pre_payload(**over):
    payload = {
        "schema_version": "r09.pre.v1", "scope": "PRE_SELECTION",
        "policy": "OBSERVATION_ONLY", "outcome": "ACCEPTED",
        "decision_ts_ms": T0,
        "setup": {"symbol": "SYN/USDT:USDT", "timeframe": "15m", "side": "long",
                  "playbook": "TREND_PULLBACK", "playbook_version": "TREND_PULLBACK_V1",
                  "trigger_candle_ms": T0 - BAR, "entry": 100.0, "stop_loss": 95.0,
                  "tp1": 105.0, "tp2": 110.0, "atr": 2.0},
        "funnel": {"first_blocker": None, "blockers_observed": [],
                   "stages_not_evaluated": ["EXECUTION"], "out_of_order": False},
        "availability": {"depth": False, "funding": True},
        "source": {"label": "binance", "resolution": "5m"},
        "learning_eligible": False,
    }
    payload.update(over)
    return payload


def accepted_row(key="pre-aaa", *, decision_ms=T0, config=None, symbol="SYN/USDT:USDT",
                 scope_label="PRE_SELECTION"):
    return {"opportunity_key": key, "symbol": symbol,
            "decision_at": ds.ms_datetime(decision_ms),
            "opportunity_scope": scope_label,
            "frozen_setup": {"symbol": symbol, "timeframe": "15m"},
            "frozen_config": {"schema_version": "r09.v1", "scope": "POST_SELECTION",
                              "r09_pre_selection": pre_payload() if config is None else config},
            "score_trace": None}


def accepted_plan(request, keys_ms):
    return ds.plan_selection(request, [(key, ds.ms_datetime(ms)) for key, ms in keys_ms])


class EscopoLegadoIntacto(unittest.TestCase):
    def test_sql_legado_byte_a_byte(self):
        self.assertEqual(scopes.index_sql(scopes.LEGACY), GOLDEN_INDEX)
        self.assertEqual(scopes.counts_sql(scopes.LEGACY), GOLDEN_COUNTS)
        self.assertIn("FROM bounds AS b", scopes.detail_sql(scopes.LEGACY))
        self.assertNotIn("r09_pre_selection", scopes.detail_sql(scopes.LEGACY))

    def test_requisicao_sem_scope_mantem_hash(self):
        sem_scope = ds.parse_request(request_body())
        explicito = ds.parse_request(request_body(scope=scopes.SCOPE_REJECTED_POST))
        self.assertEqual(sem_scope.scope.scope_id, scopes.SCOPE_REJECTED_POST)
        self.assertEqual(sem_scope.request_hash(), explicito.request_hash())
        self.assertNotIn("scope", sem_scope.normalized())
        self.assertEqual(sem_scope.normalized()["cohort"], "R09_REJECTED_POST_SELECTION")

    def test_escopo_novo_muda_o_hash(self):
        legado = ds.parse_request(request_body())
        vetadas = ds.parse_request(request_body(scope=scopes.SCOPE_PRE_VETOED))
        self.assertNotEqual(legado.request_hash(), vetadas.request_hash())
        self.assertEqual(vetadas.normalized()["scope"], scopes.SCOPE_PRE_VETOED)

    def test_escopo_desconhecido_recusado(self):
        for valor in ("QUALQUER", 7, {"a": 1}):
            with self.assertRaises(ds.DatasetError):
                ds.parse_request(request_body(scope=valor))


class DescritoresDeEscopo(unittest.TestCase):
    def test_quatro_escopos_declarados(self):
        self.assertEqual(sorted(scopes.SCOPES), sorted([
            scopes.SCOPE_REJECTED_POST, scopes.SCOPE_PRE_VETOED,
            scopes.SCOPE_PRE_ACCEPTED, scopes.SCOPE_STRUCTURAL]))

    def test_lacuna_historica_tem_reason_code(self):
        for scope in scopes.SCOPES.values():
            for campo, motivo in scope.absent_map().items():
                self.assertTrue(campo)
                self.assertRegex(motivo, r"^[A-Z_]+$")

    def test_aceitas_nao_tem_trajetoria(self):
        aceitas = scopes.PRE_ACCEPTED
        self.assertFalse(aceitas.exports_trajectory)
        self.assertFalse(aceitas.comparable_with_r10a)
        self.assertEqual(aceitas.absent_map()["candles"], scopes.TRAJECTORY_NOT_COLLECTED)
        self.assertEqual(aceitas.absent_map()["outcome"], scopes.OUTCOME_NOT_COLLECTED)
        self.assertFalse(scopes.detail_needs_window_params(aceitas))

    def test_estruturais_filtram_playbook(self):
        sql = scopes.index_sql(scopes.STRUCTURAL)
        self.assertIn("'playbook'", sql)
        self.assertIn("PRE_SELECTION", sql)
        self.assertTrue(scopes.detail_needs_window_params(scopes.STRUCTURAL))

    def test_vetadas_pre_selecao_usam_a_mesma_tabela(self):
        self.assertEqual(scopes.PRE_VETOED.source_table, scopes.REJECTED_TABLE)
        self.assertEqual(scopes.PRE_VETOED.opportunity_scope, "PRE_SELECTION")
        self.assertIn("r09_pre_selection", scopes.index_sql(scopes.PRE_VETOED))


class ArtefatoSemTrajetoria(unittest.TestCase):
    def setUp(self):
        self.request = ds.parse_request(request_body(scope=scopes.SCOPE_PRE_ACCEPTED))

    def test_exporta_decisao_funil_e_disponibilidade(self):
        plan = accepted_plan(self.request, [("pre-aaa", T0)])
        dataset, manifest = ds.build_feature_artifacts(
            self.request, plan, [accepted_row()], {"holdout_sealed": 3})
        self.assertEqual(dataset["mode"], "features_only")
        linha = dataset["rows"][0]
        self.assertEqual(linha["playbook"], "TREND_PULLBACK")
        self.assertEqual(linha["side"], "long")
        self.assertEqual(linha["funnel"]["stages_not_evaluated"], ["EXECUTION"])
        self.assertEqual(linha["availability"], {"depth": False, "funding": True})
        self.assertEqual(manifest["counts"]["exported"], {"training": 1, "validation": 0})
        self.assertEqual(manifest["counts"]["holdout_sealed"], 3)

    def test_ausencia_de_outcome_nao_vira_zero(self):
        plan = accepted_plan(self.request, [("pre-aaa", T0)])
        dataset, manifest = ds.build_feature_artifacts(
            self.request, plan, [accepted_row()], {})
        linha = dataset["rows"][0]
        self.assertIsNone(linha["outcome"])
        self.assertEqual(linha["outcome_reason"], scopes.OUTCOME_NOT_COLLECTED)
        self.assertEqual(manifest["costs"]["status"], "UNKNOWN")
        self.assertFalse(manifest["costs"]["net_r_comparable"])
        self.assertFalse(manifest["source"]["comparable_with_r10a"])
        self.assertIsNone(manifest["analysis_command"])
        self.assertFalse(manifest["live_equivalent"])
        self.assertFalse(manifest["promotable"])
        self.assertEqual(manifest["holdout"]["policy"], "SEALED")

    def test_exclusoes_por_contrato(self):
        casos = {
            "PRE_SELECTION_PAYLOAD_MISSING": {"frozen_config": {"schema_version": "r09.v1"}},
            "SOURCE_CONTRACT_MISMATCH": {"frozen_config": {
                "r09_pre_selection": pre_payload(schema_version="r09.pre.v0")}},
            "IDENTITY_MISMATCH": {"opportunity_scope": "POST_SELECTION"},
            "TEMPORAL_INCONSISTENCY": {"frozen_config": {
                "r09_pre_selection": pre_payload(setup=dict(
                    pre_payload()["setup"], trigger_candle_ms=T0 + BAR))}},
        }
        for esperado, mudanca in casos.items():
            linha = {**accepted_row(), **mudanca}
            plan = accepted_plan(self.request, [("pre-aaa", T0)])
            dataset, manifest = ds.build_feature_artifacts(self.request, plan, [linha], {})
            self.assertEqual(dataset["rows"], [], esperado)
            self.assertEqual(manifest["counts"]["excluded"][esperado], 1, esperado)
            self.assertEqual(manifest["state"], "UNUSABLE_ALL_EXCLUDED", esperado)

    def test_detalhe_precisa_bater_com_o_plano(self):
        plan = accepted_plan(self.request, [("pre-aaa", T0)])
        with self.assertRaises(ds.DatasetError):
            ds.build_feature_artifacts(self.request, plan, [accepted_row("outra")], {})
        with self.assertRaises(ds.DatasetError):
            ds.build_feature_artifacts(self.request, plan,
                                       [accepted_row(), accepted_row()], {})

    def test_decisao_nao_pode_mudar_entre_indice_e_detalhe(self):
        plan = accepted_plan(self.request, [("pre-aaa", T0)])
        with self.assertRaises(ds.DatasetError):
            ds.build_feature_artifacts(self.request, plan,
                                       [accepted_row(decision_ms=T0 + BAR)], {})


class FronteirasDosConstrutores(unittest.TestCase):
    def test_construtor_errado_para_cada_escopo(self):
        aceitas = ds.parse_request(request_body(scope=scopes.SCOPE_PRE_ACCEPTED))
        legado = ds.parse_request(request_body())
        plan = accepted_plan(aceitas, [])
        with self.assertRaises(ds.DatasetError):
            ds.build_artifacts(aceitas, plan, [], {})
        with self.assertRaises(ds.DatasetError):
            ds.build_feature_artifacts(legado, plan, [], {})

    def test_exportador_nao_le_env_nem_dotenv(self):
        arvore = ast.parse((BACKEND / "services" / "research_dataset_scopes.py").read_text())
        importados = set()
        for node in ast.walk(arvore):
            if isinstance(node, ast.Import):
                importados.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                importados.add(node.module.split(".")[0])
        self.assertEqual(importados - {"__future__"}, {"dataclasses", "typing"})
        texto = (BACKEND / "services" / "research_dataset_service.py").read_text()
        self.assertNotIn("dotenv", texto)
        self.assertNotIn("DATABASE_URL", texto)

    def test_banco_real_nao_e_tocado(self):
        self.assertIsNone(os.environ.get("DATABASE_URL"))


if __name__ == "__main__":
    unittest.main()
