"""Lote 02 §2 — manifesto AUTORIZADO: congelar antes de medir.

Negativos (bloqueiam): campo desconhecido/ausente, bool no lugar de número,
NaN/inf, fonte/unidade de custo incompatível, escopo não implementado, mudança
não isolada ou no componente errado, lados idênticos com mudança declarada,
drift de hash, divisão temporal inválida e gestão que não reconstrói no motor.

Positivos: manifesto TEST_ONLY completo autoriza ENGENHARIA mas não o estudo
real; manifesto aprovado com evidência humana libera o estudo real; o resumo
não consulta outcome nenhum.
"""
from __future__ import annotations

import copy
import json
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


from services import offline_replay_service as r10a          # noqa: E402
from services import research_manifest_service as rm         # noqa: E402
from services import score_v3_service as s3                  # noqa: E402
from services import strategy_core_service as core           # noqa: E402

BAR5 = 300_000
T0 = 1_780_000_000_000


def gestao(**mudancas) -> dict:
    """Manifesto de um `ReplayConfig` REAL (o motor reconstrói e confere)."""
    campos = dict(bar_ms=BAR5, entry_window_bars=3, pre_tp1_time_stop_bars=12,
                  max_holding_bars=24, tp1_fraction=0.45, be_lock_fraction=0.2,
                  trail_atr_multiple=2.2, trail_activation_atr=0.5, max_bars=96)
    campos.update(mudancas)
    return r10a.ReplayConfig(**campos).manifest()


def custos_config() -> dict:
    return r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                           funding_bps_per_bar=1.0).manifest()


def lado(label: str, **mudancas) -> dict:
    base = {
        "label": label,
        "core_version": core.CORE_VERSION,
        "core_config_hash": core.DEFAULT_CONFIG.config_hash(),
        "score_version": s3.SCORE_VERSION,
        "score_config_hash": s3.model_fingerprint(
            playbook=core.PLAYBOOK_TREND_PULLBACK, config=s3.DEFAULT_CONFIG),
        "policy_version": "R11C_ROBUST_POLICY_V1",
        "playbooks": list(core.PLAYBOOKS),
        "management_config": gestao(),
        # Baseline = decisão OBSERVADA do champion; candidata roda o motor dela.
        "selection_rule": {"kind": rm.RULE_OBSERVED_CHAMPION, "min_score": None,
                           "playbook": None,
                           "source": "R09_PRE_SELECTION_OUTCOME"},
    }
    base.update(mudancas)
    return base


def manifesto(*, scope=rm.SCOPE_SELECTION, decision_state=rm.DECISION_TEST_ONLY,
              **mudancas) -> dict:
    """Manifesto sintético COMPLETO. `TEST_ONLY` por padrão: engenharia sim,
    estudo real não."""
    decisao = {"state": decision_state, "authority": None, "reference": None,
               "recorded_at_ms": None, "note": "fixture de teste"}
    if decision_state != rm.DECISION_DRAFT:
        decisao.update(authority="usuario:wagalan7",
                       reference="docs/FECHAMENTO_FINAL_02_CHECKPOINT.md#decisao",
                       recorded_at_ms=T0 - 10 * BAR5)
    if scope == rm.SCOPE_SELECTION:
        mudanca = {"component": "SCORE_MODEL", "from_value": "SCORE_V2",
                   "to_value": s3.SCORE_VERSION}
        candidata = lado("candidata", score_version="R08D_SCORE_V3_CANDIDATE",
                         score_config_hash="c" * 64,
                         selection_rule={"kind": rm.RULE_SCORE_V3_MIN,
                                         "min_score": 60.0,
                                         "playbook": core.PLAYBOOK_TREND_PULLBACK,
                                         "source": s3.SCORE_VERSION})
    else:
        mudanca = {"component": "MANAGEMENT_CONFIG",
                   "from_value": 0.45, "to_value": 0.60}
        candidata = lado("candidata", management_config=gestao(tp1_fraction=0.60))
    corpo = {
        "manifest_version": rm.MANIFEST_VERSION,
        "study_id": "L02-STUDY-0001",
        "decision": decisao,
        "comparison_scope": scope,
        "population": {"universe_version": "UNIVERSE_TOP60_V1",
                       "cohort": "R09_PRE_SELECTION_ACCEPTED",
                       "scope_id": "R09_PRE_SELECTION_ACCEPTED",
                       "quote": "USDT", "symbols": None, "min_rows": 50},
        "isolated_change": mudanca,
        "baseline": lado("baseline"),
        "candidate": candidata,
        "costs": {"source": "R10A_COST_CONFIG_BPS", "unit": "BPS_PER_SIDE",
                  "asset": "USDT", "availability": rm.COSTS_DECLARED_MODEL,
                  "versions": {"engine": "R10A_OFFLINE_REPLAY_V1"},
                  "fee_treatment": "APPLIED_PER_SIDE",
                  "slippage_treatment": "APPLIED_PER_SIDE",
                  "funding_treatment": "APPLIED_PER_BAR",
                  "config": custos_config()},
        "split": {"as_of_ms": T0 + 400 * BAR5, "train_start_ms": T0,
                  "validation_start_ms": T0 + 200 * BAR5,
                  "holdout_start_ms": T0 + 300 * BAR5,
                  "purge_bars": 1, "embargo_bars": 2},
        "hashes": None,
    }
    corpo.update(mudancas)
    return corpo


def recusa(teste, corpo, codigo):
    with teste.assertRaises(rm.ManifestError) as ctx:
        rm.parse_manifest(corpo)
    teste.assertEqual(ctx.exception.reason_code, codigo, ctx.exception.detail)


class ManifestoCongelado(unittest.TestCase):
    """Parse completo, hashes por seção e bundle — antes de qualquer métrica."""

    def test_manifesto_test_only_valida_e_nao_libera_estudo_real(self):
        parsed = rm.parse_manifest(manifesto())
        self.assertEqual(rm.manifest_state(parsed), rm.STATE_TEST_ONLY)
        for campo in rm.HASH_FIELDS:
            self.assertTrue(parsed["hashes"][campo])
        self.assertTrue(parsed["manifest_hash"])
        autorizado = rm.authorized_comparison(parsed)
        self.assertTrue(autorizado["available"])
        self.assertFalse(autorizado["real_study_allowed"])
        self.assertEqual(autorizado["reason_code"], "TEST_ONLY_MANIFEST")

    def test_manifesto_aprovado_exige_evidencia_humana(self):
        aprovado = rm.parse_manifest(manifesto(decision_state=rm.DECISION_APPROVED))
        verdict = rm.authorized_comparison(aprovado)
        self.assertEqual(verdict["state"], rm.STATE_APPROVED)
        self.assertTrue(verdict["real_study_allowed"])
        # Sem autoridade/referência/instante não existe aprovação.
        for campo in ("authority", "reference", "recorded_at_ms"):
            corpo = manifesto(decision_state=rm.DECISION_APPROVED)
            corpo["decision"][campo] = None
            recusa(self, corpo, rm.DECISION_EVIDENCE_MISSING)

    def test_approved_true_no_payload_nao_e_evidencia(self):
        corpo = manifesto()
        corpo["approved"] = True            # propriedade solta: fora do schema
        recusa(self, corpo, rm.UNKNOWN_FIELD)

    def test_draft_e_ausencia_mantem_estudo_bloqueado(self):
        rascunho = rm.parse_manifest(manifesto(decision_state=rm.DECISION_DRAFT))
        bloqueado = rm.authorized_comparison(rascunho)
        self.assertFalse(bloqueado["available"])
        self.assertEqual(bloqueado["state"], rm.STATE_BLOCKED)
        self.assertEqual(len(bloqueado["decision_required"]), 3)
        ausente = rm.authorized_comparison(None)
        self.assertFalse(ausente["available"])
        self.assertEqual(ausente["reason_code"], "AUTHORIZED_CANDIDATE_NOT_DECLARED")

    def test_sem_campo_ou_com_campo_extra_bloqueia(self):
        faltando = manifesto()
        faltando.pop("costs")
        recusa(self, faltando, rm.FIELD_MISSING)
        secao_extra = manifesto()
        secao_extra["population"] = {**secao_extra["population"], "extra": 1}
        recusa(self, secao_extra, rm.UNKNOWN_FIELD)

    def test_bool_numerico_e_nao_finito_bloqueiam(self):
        booleano = manifesto()
        booleano["population"] = {**booleano["population"], "min_rows": True}
        recusa(self, booleano, rm.BOOL_AS_NUMBER)
        for valor in (float("nan"), float("inf"), 1.5):
            corpo = manifesto()
            corpo["split"] = {**corpo["split"], "purge_bars": valor}
            recusa(self, corpo, rm.NON_FINITE)

    def test_divisao_temporal_invalida_bloqueia(self):
        fora_de_ordem = manifesto()
        fora_de_ordem["split"] = {**fora_de_ordem["split"],
                                  "holdout_start_ms": T0 + 100 * BAR5}
        recusa(self, fora_de_ordem, rm.SPLIT_INVALID)
        sem_purga = manifesto()
        sem_purga["split"] = {**sem_purga["split"], "purge_bars": 0}
        recusa(self, sem_purga, rm.SPLIT_INVALID)

    def test_custos_incompativeis_bloqueiam(self):
        desconhecida = manifesto()
        desconhecida["costs"] = {**desconhecida["costs"], "source": "PLANILHA"}
        recusa(self, desconhecida, rm.COSTS_SOURCE_INCOMPATIBLE)
        unidade = manifesto()
        unidade["costs"] = {**unidade["costs"], "unit": "PERCENT"}
        recusa(self, unidade, rm.COSTS_SOURCE_INCOMPATIBLE)
        # Modelo declarado não pode se apresentar como custo observado da conta.
        fingindo = manifesto()
        fingindo["costs"] = {**fingindo["costs"],
                             "availability": rm.COSTS_OBSERVED_ACCOUNT}
        recusa(self, fingindo, rm.COSTS_SOURCE_INCOMPATIBLE)
        tratamento = manifesto()
        tratamento["costs"] = {**tratamento["costs"], "fee_treatment": "TALVEZ"}
        recusa(self, tratamento, rm.COSTS_INCOMPLETE)
        sem_versao = manifesto()
        sem_versao["costs"] = {**sem_versao["costs"], "versions": {}}
        recusa(self, sem_versao, rm.COSTS_INCOMPLETE)

    def test_escopo_nao_implementado_e_recusado_sem_fallback(self):
        corpo = manifesto(scope="FULL_STACK")
        recusa(self, corpo, rm.SCOPE_NOT_IMPLEMENTED)

    def test_mudanca_precisa_ser_isolada_e_do_escopo(self):
        # Declarou SCORE_MODEL mas o núcleo também mudou.
        vazou = manifesto()
        vazou["candidate"] = lado("candidata", score_version="OUTRO",
                                  score_config_hash="c" * 64,
                                  core_config_hash="d" * 64)
        recusa(self, vazou, rm.CHANGE_NOT_ISOLATED)
        # Componente de gestão sob escopo de seleção.
        trocado = manifesto()
        trocado["isolated_change"] = {"component": "MANAGEMENT_CONFIG",
                                      "from_value": 1, "to_value": 2}
        recusa(self, trocado, rm.CHANGE_SCOPE_MISMATCH)
        # Lados idênticos com mudança declarada: nada atravessaria o componente.
        iguais = manifesto()
        iguais["candidate"] = lado("candidata")
        recusa(self, iguais, rm.SIDES_IDENTICAL_DECLARED_CHANGE)

    def test_gestao_que_nao_reconstroi_no_motor_bloqueia(self):
        corpo = manifesto(scope=rm.SCOPE_MANAGEMENT)
        corpo["candidate"] = lado("candidata",
                                  management_config={**gestao(), "tp1_fraction": 9.9})
        recusa(self, corpo, rm.CONFIG_INVALID)

    def test_drift_de_hash_declarado_bloqueia(self):
        parsed = rm.parse_manifest(manifesto())
        adulterado = copy.deepcopy(parsed)
        adulterado["hashes"]["bundle_hash"] = "0" * 64
        recusa(self, {campo: adulterado[campo] for campo in rm.MANIFEST_FIELDS},
               rm.MANIFEST_DRIFT)

    def test_adulteracao_apos_congelamento_e_detectada(self):
        parsed = rm.parse_manifest(manifesto())
        adulterado = copy.deepcopy(parsed)
        adulterado["population"]["min_rows"] = 1      # corpo mudou
        verdict = rm.verify_manifest(adulterado)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], rm.MANIFEST_DRIFT)
        # E o hash esperado de outro manifesto também recusa.
        outro = rm.parse_manifest(manifesto(study_id="L02-STUDY-0002"))
        self.assertFalse(rm.verify_manifest(
            parsed, expected_hash=outro["manifest_hash"])["ok"])

    def test_manifesto_do_escopo_de_gestao_continua_valido(self):
        parsed = rm.parse_manifest(manifesto(scope=rm.SCOPE_MANAGEMENT))
        self.assertEqual(parsed["comparison_scope"], rm.SCOPE_MANAGEMENT)
        self.assertEqual(parsed["isolated_change"]["component"],
                         "MANAGEMENT_CONFIG")
        self.assertNotEqual(parsed["baseline"]["management_config"]["config_hash"],
                            parsed["candidate"]["management_config"]["config_hash"])

    def test_resumo_nao_consulta_outcome(self):
        resumo = rm.manifest_summary(rm.parse_manifest(manifesto()))
        self.assertFalse(resumo["outcomes_consulted"])
        self.assertTrue(resumo["hashes_generated_before_results"])
        self.assertEqual(resumo["component_changed"], "SCORE_MODEL")

    def test_arquivo_ilegivel_ou_invalido_recusa_com_motivo(self):
        import tempfile
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "manifesto.json"
            caminho.write_text("{", encoding="utf-8")
            self.assertEqual(rm.load_manifest_file(caminho)["reason_code"],
                             "MANIFEST_FILE_UNREADABLE")
            caminho.write_text(json.dumps(manifesto()), encoding="utf-8")
            lido = rm.load_manifest_file(caminho)
            self.assertTrue(lido["ok"])
            self.assertEqual(lido["state"], rm.STATE_TEST_ONLY)
            self.assertEqual(rm.load_manifest_file(Path(pasta) / "nao-existe")
                             ["reason_code"], "MANIFEST_FILE_UNREADABLE")


if __name__ == "__main__":
    unittest.main()
