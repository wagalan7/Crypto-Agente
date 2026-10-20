"""Lote 02 §4 — dois caminhos de verdade e despacho por ESCOPO.

Positivos: SELECTION_ONLY válido atravessa pedido → contrato → verificador →
catálogo; baseline igual à candidata produz decisões idênticas; candidata
alterando a seleção DE VERDADE muda o conjunto que vai ao replay.

Negativos: escopo não implementado, candidato de seleção validado como de
gestão (e o inverso), manifesto adulterado, gestão que também muda num estudo
de seleção, e contrato V1 num escopo que exige V2 — todos bloqueiam ANTES de
qualquer leitura de outcome.
"""
from __future__ import annotations

import copy
import socket
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL_GETADDRINFO = socket.getaddrinfo


def setUpModule():
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DNS proibido no Lote 02"))


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO


from services import offline_replay_service as r10a                # noqa: E402
from services import preselection_experiment_service as r12        # noqa: E402
from services import research_dataset_service as ds               # noqa: E402
from services import research_manifest_service as rm              # noqa: E402
from services import research_selection_service as rsel           # noqa: E402
from services import score_v3_service as s3                       # noqa: E402
from services import strategy_core_service as core                # noqa: E402
from services import strategy_evidence_service as ev              # noqa: E402
from tests.test_lote02_research_manifest import (custos_config,   # noqa: E402
                                                gestao, lado, manifesto,
                                                champion_trace)

BAR5 = 300_000
T0 = 1_780_000_000_000


def features(**mudancas) -> dict:
    """Features PONTO-NO-TEMPO completas para o Score V3 real."""
    base = {"adx": 30.0, "htf_alignment_ratio": 1.0, "structure_quality": 0.8,
            "level_distance_atr": 0.5, "trigger_body_ratio": 0.7,
            "trigger_follow_through_atr": 0.4, "rr_tp2": 2.5,
            "entry_distance_atr": 0.1, "volume_ratio": 1.1, "spread_pct": 0.03,
            "funding_pct": 0.0}
    base.update(mudancas)
    return base


def linha(chave: str, *, outcome: str, feats=None, side: str = "long",
          niveis=True) -> dict:
    corpo = {"opportunity_key": chave, "symbol": "SYN/USDT:USDT", "side": side,
             "decision_ts_ms": T0 + 10 * BAR5, "observed_outcome": outcome,
             "observed_decision_scope": "FINAL_SCANNER_SELECTION",
             "funnel": {"first_blocker_reason": None if outcome == "ACCEPTED"
                        else "TIER_BELOW_MINIMUM"},
             "score_trace": champion_trace(),
             "features": features() if feats is None else feats, "atr": 1.0}
    if niveis:
        corpo.update(entry=100.0, stop_loss=99.0, tp1=103.0, tp2=106.0)
    return corpo


def pedido_gestao(**mudancas) -> dict:
    """Pedido LEGADO de gestão (o hash dele não pode mudar neste lote)."""
    replay = {chave: valor for chave, valor in gestao().items()
              if chave not in ("schema_version", "config_hash")}
    corpo = {
        "as_of_utc": ds.ms_datetime(T0 + 400 * BAR5).isoformat().replace("+00:00", "Z"),
        "split": {"train_start_ms": T0, "validation_start_ms": T0 + 200 * BAR5,
                  "holdout_start_ms": T0 + 300 * BAR5, "purge_bars": 1},
        "baseline_config": dict(replay),
        "candidate": {"candidate_id": "CAND-MGMT", "registered_at_ms": T0 - BAR5,
                      "kind": "MANAGEMENT_ONLY",
                      "replay_config": {**replay, "tp1_fraction": 0.60}},
        "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0,
                  "funding_bps_per_bar": 1.0},
        "bootstrap": {"seed": 5, "samples": 100, "block_size": 1}}
    corpo.update(mudancas)
    return corpo


def bloco_selecao(**mudancas) -> dict:
    base = {"core_version": core.CORE_VERSION,
            "core_config_hash": core.DEFAULT_CONFIG.config_hash(),
            "score_version": s3.SCORE_VERSION,
            "score_config_hash": s3.model_fingerprint(
                playbook=core.PLAYBOOK_TREND_PULLBACK, config=s3.DEFAULT_CONFIG),
            "score_config": s3.DEFAULT_CONFIG.as_dict(),
            "playbooks": list(core.PLAYBOOKS),
            "selection_rule": {"kind": rm.RULE_SCORE_V3_MIN, "min_score": 60.0,
                               "playbook": core.PLAYBOOK_TREND_PULLBACK,
                               "source": s3.SCORE_VERSION}}
    base.update(mudancas)
    return base


def pedido_selecao(**mudancas) -> dict:
    """Pedido de SELEÇÃO: gestão IDÊNTICA dos dois lados + motores declarados."""
    replay = {chave: valor for chave, valor in gestao().items()
              if chave not in ("schema_version", "config_hash")}
    corpo = pedido_gestao()
    corpo["candidate"] = {"candidate_id": "CAND-SEL",
                          "registered_at_ms": T0 - BAR5,
                          "kind": "SELECTION_ONLY",
                          "replay_config": dict(replay),
                          "selection": bloco_selecao()}
    corpo.update(mudancas)
    return corpo


class DespachoPorEscopo(unittest.TestCase):
    """Pedido, contrato, verificador e catálogo andam juntos — por escopo."""

    def test_pedido_de_gestao_mantem_o_hash_legado(self):
        antes = ds.parse_request(pedido_gestao())
        self.assertIsNone(antes.selection)
        self.assertNotIn("selection", antes.payload_head()["candidate"])
        # Mesmo corpo ⇒ mesmo hash (o escopo novo não mexe no pedido antigo).
        self.assertEqual(antes.request_hash(),
                         ds.parse_request(pedido_gestao()).request_hash())

    def test_pedido_de_selecao_entra_com_schema_fechado(self):
        pedido = ds.parse_request(pedido_selecao())
        self.assertEqual(pedido.candidate.kind, "SELECTION_ONLY")
        self.assertEqual(pedido.selection["selection_rule"]["kind"],
                         rm.RULE_SCORE_V3_MIN)
        self.assertEqual(pedido.changed, (),
                         "gestão congelada: nada de diff de gestão")
        self.assertIn("selection", pedido.payload_head()["candidate"])
        # Hash do pedido muda com os motores — eles entram na identidade.
        outro = ds.parse_request(pedido_selecao(
            candidate={**pedido_selecao()["candidate"],
                       "selection": bloco_selecao(
                           selection_rule={"kind": rm.RULE_SCORE_V3_MIN,
                                           "min_score": 70.0,
                                           "playbook": core.PLAYBOOK_TREND_PULLBACK,
                                           "source": s3.SCORE_VERSION})}))
        self.assertNotEqual(pedido.request_hash(), outro.request_hash())

    def test_selecao_com_gestao_diferente_e_recusada(self):
        replay = {chave: valor for chave, valor in gestao().items()
                  if chave not in ("schema_version", "config_hash")}
        mau = pedido_selecao()
        mau["candidate"]["replay_config"] = {**replay, "tp1_fraction": 0.60}
        with self.assertRaises(ds.DatasetError) as ctx:
            ds.parse_request(mau)
        self.assertIn("gestão idêntica", str(ctx.exception))

    def test_tipo_desconhecido_nao_tem_fallback(self):
        mau = pedido_selecao()
        mau["candidate"]["kind"] = "FULL_STACK"
        with self.assertRaises(ds.DatasetError):
            ds.parse_request(mau)

    def test_regra_nao_implementada_bloqueia_o_pedido(self):
        mau = pedido_selecao()
        mau["candidate"]["selection"] = bloco_selecao(
            selection_rule={"kind": "CHUTE", "min_score": 1.0,
                            "playbook": "X", "source": "Y"})
        with self.assertRaises(ds.DatasetError):
            ds.parse_request(mau)


class ContratoEVerificador(unittest.TestCase):
    """Contrato V2 por escopo; envelope do catálogo do MESMO tipo."""

    def _contrato(self, *, escopo=rm.SCOPE_SELECTION, manifest_hash=None,
                 selection=None, fingerprint="f" * 64, cutoff_ms=None):
        gestao_manifest = gestao()
        extras = {}
        universe, bundle, cutoff = "SYN-6", "b" * 64, cutoff_ms or T0
        if escopo == rm.SCOPE_SELECTION:
            frozen = rm.parse_manifest(manifesto())
            universe = frozen["population"]["universe_version"]
            bundle = frozen["hashes"]["bundle_hash"]
            cutoff = cutoff_ms or frozen["split"]["as_of_ms"]
            extras = {"manifest_hash": manifest_hash or frozen["manifest_hash"],
                      "selection_config": selection or bloco_selecao(),
                      "research_manifest": frozen,
                      "dataset_scope": frozen["population"]["scope_id"],
                      "temporal_split": frozen["split"]}
        return r12.preselection_contract(
            population="SHADOW", study_kind="PRE_SELECTION",
            policy_version="R11C_ROBUST_POLICY_V1", universe_version=universe,
            comparison_scope=escopo, baseline_config=gestao_manifest,
            candidate_config=gestao_manifest, costs_config=custos_config(),
            bundle_hash=bundle, dataset_fingerprint=fingerprint,
            cutoff_ms=cutoff, **extras)

    def _estudo(self, contrato):
        return {"contract": contrato, "contract_hash": contrato["contract_hash"],
                "population": contrato["population"],
                "study_kind": contrato["study_kind"],
                "policy_version": contrato["policy_version"],
                "universe_version": contrato["universe_version"],
                "comparison_scope": contrato["comparison_scope"],
                "bundle_hash": contrato["bundle_hash"],
                "dataset_fingerprint": contrato["dataset_fingerprint"],
                "cutoff_ms": contrato["cutoff_ms"],
                "evidence_key": "e" * 32, "gate_verdict": "NO_GO",
                "evidence": {}}

    def _verificar(self, contrato, envelope):
        from datetime import datetime, timezone
        return ev.verify_study_identity(
            self._estudo(contrato), candidate_config=envelope,
            fingerprint=contrato["dataset_fingerprint"],
            cutoff=datetime.fromtimestamp(contrato["cutoff_ms"] / 1000,
                                          tz=timezone.utc))

    def test_selecao_valida_atravessa_contrato_e_catalogo(self):
        contrato = self._contrato()
        self.assertEqual(contrato["contract_version"], r12.PRE_SELECTION_CONTRACT_V2)
        envelope = r12.build_preselection_envelope(
            replay_config=gestao(), contract_hash=contrato["contract_hash"],
            selection_config=bloco_selecao())
        validado = r12.validate_preselection_envelope(envelope)
        self.assertTrue(validado["ok"], validado)
        self.assertEqual(validado["comparison_scope"], r12.SCOPE_SELECTION_ONLY)
        verdict = self._verificar(contrato, envelope)
        self.assertTrue(verdict["ok"], verdict)

    def test_gestao_legada_continua_valida_com_contrato_v1(self):
        contrato = self._contrato(escopo=rm.SCOPE_MANAGEMENT)
        self.assertEqual(contrato["contract_version"],
                         r12.PRE_SELECTION_CONTRACT_VERSION)
        envelope = r12.build_preselection_envelope(
            replay_config=gestao(), contract_hash=contrato["contract_hash"])
        verdict = self._verificar(contrato, envelope)
        self.assertTrue(verdict["ok"], verdict)

    def test_candidato_de_selecao_nao_vale_como_de_gestao(self):
        contrato = self._contrato()
        envelope_gestao = r12.build_preselection_envelope(
            replay_config=gestao(), contract_hash=contrato["contract_hash"])
        verdict = self._verificar(contrato, envelope_gestao)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], r12.SCOPE_CONTRACT_MISMATCH)

    def test_candidato_de_gestao_nao_vale_como_de_selecao(self):
        contrato = self._contrato(escopo=rm.SCOPE_MANAGEMENT)
        envelope_selecao = r12.build_preselection_envelope(
            replay_config=gestao(), contract_hash=contrato["contract_hash"],
            selection_config=bloco_selecao())
        verdict = self._verificar(contrato, envelope_selecao)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], r12.SCOPE_CONTRACT_MISMATCH)

    def test_escopo_nao_implementado_e_recusado(self):
        with self.assertRaises(ValueError):
            self._contrato(escopo="FULL_STACK")

    def test_selecao_sem_manifesto_ou_sem_motores_nao_nasce(self):
        with self.assertRaises(ValueError):
            r12.preselection_contract(
                population="SHADOW", study_kind="PRE_SELECTION",
                policy_version="P", universe_version="U",
                comparison_scope=rm.SCOPE_SELECTION, baseline_config=gestao(),
                candidate_config=gestao(), costs_config=custos_config(),
                bundle_hash="b" * 64, dataset_fingerprint="f" * 64,
                cutoff_ms=T0)

    def test_contrato_adulterado_bloqueia_antes_do_outcome(self):
        contrato = self._contrato()
        envelope = r12.build_preselection_envelope(
            replay_config=gestao(), contract_hash=contrato["contract_hash"],
            selection_config=bloco_selecao())
        adulterado = copy.deepcopy(contrato)
        adulterado["selection_config"]["selection_rule"]["min_score"] = 10.0
        verdict = self._verificar(adulterado, envelope)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], r12.CONTRACT_INVALID)
        # Também recusa quando o corpo é V1 num escopo que exige V2 — mesmo com
        # o envelope declarando o hash recalculado do corpo V1 (ou seja: não é
        # só o hash que protege, é o contrato de escopo).
        v1 = {key: contrato[key] for key in r12.PRE_SELECTION_CONTRACT_FIELDS}
        v1["contract_version"] = r12.PRE_SELECTION_CONTRACT_VERSION
        v1["contract_hash"] = None
        v1["contract_hash"] = r12.contract_hash_of(v1)
        envelope_v1 = r12.build_preselection_envelope(
            replay_config=gestao(), contract_hash=v1["contract_hash"],
            selection_config=bloco_selecao())
        verdict_v1 = self._verificar(v1, envelope_v1)
        self.assertFalse(verdict_v1["ok"])
        self.assertEqual(verdict_v1["reason_code"], r12.SCOPE_CONTRACT_MISMATCH)


class DoisCaminhosDeVerdade(unittest.TestCase):
    """A decisão de cada lado vem do motor dele, sobre a MESMA população."""

    def _manifesto(self, *, min_score=60.0):
        corpo = manifesto()
        corpo["candidate"]["selection_rule"] = {
            "kind": rm.RULE_SCORE_V3_MIN, "min_score": min_score,
            "playbook": core.PLAYBOOK_TREND_PULLBACK, "source": s3.SCORE_VERSION}
        return rm.parse_manifest(corpo)

    def test_candidata_altera_a_selecao_de_verdade(self):
        # Score V3 real desta população ≈ 73.3: corte 60 seleciona, 90 rejeita.
        populacao = [linha("a", outcome="ACCEPTED"),
                     linha("b", outcome="VETOED"),
                     linha("c", outcome="ACCEPTED")]
        frouxo = rsel.compare_population(populacao, manifest=self._manifesto(min_score=60.0))
        apertado = rsel.compare_population(populacao, manifest=self._manifesto(min_score=90.0))
        self.assertEqual(sorted(l["opportunity_key"] for l in frouxo["selected"]["candidate"]),
                         ["a", "b", "c"], "candidata seleciona por conta própria")
        self.assertEqual(apertado["selected"]["candidate"], [],
                         "corte mais alto muda a seleção de verdade")
        # A baseline NÃO muda: ela é a decisão observada do champion.
        for resultado in (frouxo, apertado):
            self.assertEqual(sorted(l["opportunity_key"]
                                    for l in resultado["selected"]["baseline"]),
                             ["a", "c"])
        # Conjuntos diferentes ⇒ entradas de replay diferentes.
        self.assertNotEqual(rsel.replay_candidates(frouxo["selected"]["candidate"]),
                            rsel.replay_candidates(frouxo["selected"]["baseline"]))

    def test_baseline_igual_a_candidata_da_o_mesmo_conjunto(self):
        """Mesma regra dos dois lados ⇒ MESMAS decisões (nada de delta mágico)."""
        corpo = manifesto()
        # Nesta coorte ambos selecionam todos: decisões/entradas iguais, sem
        # alegar que a fórmula V2 observada seja a mesma fórmula da candidata.
        corpo["candidate"]["selection_rule"] = {
            "kind": rm.RULE_SCORE_V3_MIN, "min_score": 0.0,
            "playbook": core.PLAYBOOK_TREND_PULLBACK, "source": s3.SCORE_VERSION}
        manifesto_igual = rm.parse_manifest(corpo)
        populacao = [linha("a", outcome="ACCEPTED"), linha("c", outcome="ACCEPTED")]
        resultado = rsel.compare_population(populacao, manifest=manifesto_igual)
        self.assertEqual([l["opportunity_key"] for l in resultado["selected"]["baseline"]],
                         [l["opportunity_key"] for l in resultado["selected"]["candidate"]])
        self.assertEqual(rsel.replay_candidates(resultado["selected"]["baseline"]),
                         rsel.replay_candidates(resultado["selected"]["candidate"]))

    def test_unknown_e_excluido_simetricamente_e_reportado(self):
        populacao = [
            linha("a", outcome="ACCEPTED"),
            # Sem features: candidata não decide ⇒ UNKNOWN dos dois lados.
            linha("b", outcome="ACCEPTED", feats={}),
            # Sem decisão observada: baseline desconhecida ⇒ exclui os dois.
            linha("c", outcome="UNRESOLVED"),
            # Features insuficientes: o modelo real devolve UNAVAILABLE.
            linha("d", outcome="ACCEPTED", feats={"adx": 30.0}),
        ]
        resultado = rsel.compare_population(populacao, manifest=self._manifesto())
        self.assertEqual([l["opportunity_key"] for l in resultado["selected"]["baseline"]],
                         ["a"])
        self.assertEqual([l["opportunity_key"] for l in resultado["selected"]["candidate"]],
                         ["a"])
        self.assertEqual(resultado["coverage"]["excluded_symmetric"], 3)
        self.assertEqual(resultado["coverage"]["rows_decided"], 1)
        self.assertAlmostEqual(resultado["coverage"]["coverage_pct"], 25.0)
        motivos = resultado["coverage"]["excluded_reasons"]
        self.assertIn(f"candidate:{rsel.FEATURES_MISSING}", motivos)
        self.assertIn(f"baseline:{rsel.OUTCOME_MISSING}", motivos)
        self.assertIn(f"candidate:{rsel.SCORE_UNAVAILABLE}", motivos)
        # Nada foi convertido em rejeição para "salvar" cobertura.
        self.assertEqual(resultado["counts"]["candidate"][rsel.STATE_REJECTED], 0)

    def test_sem_niveis_a_linha_e_inviavel_para_os_dois_lados(self):
        populacao = [linha("a", outcome="ACCEPTED", niveis=False)]
        resultado = rsel.compare_population(populacao, manifest=self._manifesto())
        self.assertEqual(resultado["selected"]["baseline"], [])
        self.assertEqual(resultado["selected"]["candidate"], [])
        self.assertEqual(resultado["coverage"]["levels_missing"], 1)

    def test_manifesto_de_gestao_nao_roda_comparacao_de_selecao(self):
        resultado = rsel.compare_population(
            [linha("a", outcome="ACCEPTED")],
            manifest=rm.parse_manifest(manifesto(scope=rm.SCOPE_MANAGEMENT)))
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["reason_code"], rsel.SCOPE_NOT_IMPLEMENTED)

    def test_manifesto_ausente_ou_rascunho_bloqueia(self):
        for manifesto_invalido in (None,
                                   rm.parse_manifest(
                                       manifesto(decision_state=rm.DECISION_DRAFT))):
            resultado = rsel.compare_population([linha("a", outcome="ACCEPTED")],
                                                manifest=manifesto_invalido)
            self.assertFalse(resultado["ok"])
            self.assertEqual(resultado["reason_code"],
                             "AUTHORIZED_CANDIDATE_NOT_DECLARED")

    def test_nenhum_outcome_e_consultado_na_selecao(self):
        resultado = rsel.compare_population([linha("a", outcome="ACCEPTED")],
                                            manifest=self._manifesto())
        self.assertFalse(resultado["outcomes_consulted"])
        self.assertTrue(resultado["same_population_both_sides"])


if __name__ == "__main__":
    unittest.main()
