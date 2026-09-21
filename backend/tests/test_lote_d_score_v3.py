"""Bloco D — Score V3 de pesquisa: allowlist, caps, decomposição e fronteiras.

Cobre ausência, finitude, domínio, caps, decomposição, monotonicidade, papel do
ADX por playbook, funding direcional em parcela única, correção dos degraus de
confluência, fingerprint e a proibição de fallback da V2.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import score_v3_service as s3  # noqa: E402

PERFEITO_LONG = {
    "adx": 40.0,
    "htf_alignment_ratio": 1.0,
    "structure_quality": 1.0,
    "level_distance_atr": 0.2,
    "trigger_body_ratio": 0.8,
    "trigger_follow_through_atr": 1.0,
    "rr_tp2": 4.0,
    "entry_distance_atr": 0.0,
    "volume_ratio": 2.0,
    "spread_pct": 0.02,
    "funding_pct": -0.05,
}
NEUTRO = dict(PERFEITO_LONG, funding_pct=0.0)


def pontuar(features=None, *, playbook=s3.PLAYBOOK_TREND_PULLBACK, side="long",
            config=None):
    return s3.score(features if features is not None else PERFEITO_LONG,
                    playbook=playbook, side=side, config=config)


class Allowlist(unittest.TestCase):
    def test_documentacao_completa_de_cada_feature(self):
        for feature in s3.ALLOWLIST + (s3.COMPOSITE_FEATURE,):
            self.assertIn(feature.category,
                          set(s3.CATEGORY_CAPS) | {s3.CATEGORY_CONFLUENCE_COMPOSITE})
            self.assertTrue(feature.unit)
            self.assertEqual(len(feature.domain), 2)
            self.assertLess(feature.domain[0], feature.domain[1])
            self.assertIn(feature.sense, (s3.SENSE_MAGNITUDE, s3.SENSE_QUALITY,
                                          s3.SENSE_INVERSE, s3.SENSE_DIRECTIONAL))
            self.assertGreater(feature.weight, 0)
            self.assertTrue(feature.evidence_key)
            self.assertTrue(feature.description)

    def test_evidencia_nao_e_contada_duas_vezes(self):
        chaves = [feature.evidence_key for feature in s3.ALLOWLIST]
        self.assertEqual(len(chaves), len(set(chaves)))
        ativas = s3.active_features(s3.ScoreConfig(include_composite_confluence=True))
        chaves = [feature.evidence_key for feature in ativas]
        self.assertEqual(len(chaves), len(set(chaves)))

    def test_caps_somam_cem_nas_duas_configuracoes(self):
        self.assertEqual(sum(s3.CATEGORY_CAPS.values()), 100.0)
        composto = s3.ScoreConfig(include_composite_confluence=True)
        self.assertEqual(sum(s3.active_caps(composto).values()), 100.0)

    def test_composto_substitui_e_nao_soma(self):
        composto = s3.ScoreConfig(include_composite_confluence=True)
        categorias = {feature.category for feature in s3.active_features(composto)}
        for substituida in s3.COMPOSITE_SUPERSEDES:
            self.assertNotIn(substituida, categorias)
        self.assertIn(s3.CATEGORY_CONFLUENCE_COMPOSITE, categorias)

    def test_exclusoes_registradas(self):
        for nome in ("confluence_pct", "score_v2", "tier", "probability_tp1"):
            self.assertTrue(s3.EXCLUDED_FEATURES[nome])


class Pontuacao(unittest.TestCase):
    def test_evidencia_completa_atinge_a_escala(self):
        payload = pontuar()
        self.assertEqual(payload["state"], s3.STATE_OK)
        self.assertAlmostEqual(payload["score"], 100.0)
        self.assertAlmostEqual(payload["max_possible_points"], 100.0)
        self.assertAlmostEqual(payload["evidence_coverage"], 1.0)

    def test_decomposicao_bate_com_o_total(self):
        payload = pontuar()
        soma = sum(bucket["points"] for bucket in payload["decomposition"].values())
        self.assertAlmostEqual(soma, payload["score"])
        for categoria, bucket in payload["decomposition"].items():
            self.assertLessEqual(bucket["points"], bucket["cap"] + 1e-12, categoria)
            for nome, detalhe in bucket["features"].items():
                self.assertAlmostEqual(detalhe["contribution"],
                                       detalhe["normalized"] * detalhe["weight"])

    def test_cap_de_categoria_e_aplicado(self):
        bucket = pontuar()["decomposition"][s3.CATEGORY_REGIME_MTF]
        self.assertTrue(bucket["cap_applied"])
        self.assertAlmostEqual(bucket["raw_points"], 30.0)
        self.assertAlmostEqual(bucket["points"], 25.0)

    def test_ausencia_nao_vira_zero(self):
        sem_estrutura = {k: v for k, v in PERFEITO_LONG.items() if k != "structure_quality"}
        payload = pontuar(sem_estrutura)
        self.assertEqual(payload["state"], s3.STATE_OK)
        self.assertIn("structure_quality",
                      payload["missing_by_category"][s3.CATEGORY_STRUCTURE])
        self.assertNotIn("structure_quality",
                         payload["decomposition"][s3.CATEGORY_STRUCTURE]["features"])
        # A escala disponível encolhe junto — a ausência não é pontuada como 0.
        self.assertAlmostEqual(payload["max_possible_points"], 88.0)
        self.assertAlmostEqual(payload["score"], 88.0)
        self.assertLess(payload["evidence_coverage"], 1.0)

    def test_valor_invalido_nao_vira_evidencia(self):
        casos = {"adx": (float("nan"), s3.INPUT_NOT_FINITE),
                 "htf_alignment_ratio": ("1.0", s3.INPUT_NOT_NUMERIC),
                 "volume_ratio": (-5.0, s3.INPUT_OUT_OF_DOMAIN),
                 "spread_pct": (True, s3.INPUT_NOT_NUMERIC)}
        for nome, (valor, esperado) in casos.items():
            payload = pontuar(dict(PERFEITO_LONG, **{nome: valor}))
            feature = s3.ALLOWLIST_BY_NAME[nome]
            invalidos = payload["invalid_by_category"][feature.category]
            self.assertEqual(invalidos.get(nome), esperado, nome)
            self.assertNotIn(nome, payload["decomposition"][feature.category]["features"])
            self.assertIn(esperado, payload["reason_codes"])

    def test_cobertura_abaixo_do_piso_indisponibiliza(self):
        payload = pontuar({"funding_pct": 0.0})
        self.assertEqual(payload["state"], s3.STATE_UNAVAILABLE)
        self.assertIsNone(payload["score"])
        self.assertIn(s3.EVIDENCE_BELOW_FLOOR, payload["reason_codes"])

    def test_entradas_estruturalmente_invalidas(self):
        self.assertEqual(pontuar(side="neutro")["reason_codes"], (s3.SIDE_REQUIRED,))
        self.assertEqual(pontuar(playbook="QUALQUER")["reason_codes"],
                         (s3.PLAYBOOK_UNKNOWN,))

    def test_monotonicidade_do_rr(self):
        anterior = None
        for rr in (1.8, 2.2, 2.6, 3.0, 4.0, 5.0):
            atual = pontuar(dict(PERFEITO_LONG, rr_tp2=rr))["score"]
            if anterior is not None:
                self.assertGreaterEqual(atual + 1e-12, anterior, rr)
            anterior = atual

    def test_score_nao_e_probabilidade(self):
        payload = pontuar()
        self.assertIsNone(payload["probability"])
        self.assertIsNone(payload["tier"])
        self.assertFalse(payload["is_probability"])


class PapelDoADX(unittest.TestCase):
    def test_adx_nao_define_lado(self):
        """Mesmas features, lados opostos: com funding neutro o score é igual."""
        longo = pontuar(NEUTRO, side="long")
        curto = pontuar(NEUTRO, side="short")
        self.assertAlmostEqual(longo["score"], curto["score"])
        adx_long = longo["decomposition"][s3.CATEGORY_REGIME_MTF]["features"]["adx"]
        adx_short = curto["decomposition"][s3.CATEGORY_REGIME_MTF]["features"]["adx"]
        self.assertEqual(adx_long["contribution"], adx_short["contribution"])
        self.assertEqual(adx_long["sense"], s3.SENSE_MAGNITUDE)

    def test_significado_depende_do_playbook(self):
        def contribuicao(playbook, adx):
            payload = pontuar(dict(PERFEITO_LONG, adx=adx), playbook=playbook)
            return payload["decomposition"][s3.CATEGORY_REGIME_MTF]["features"]["adx"]["contribution"]

        fraco = contribuicao(s3.PLAYBOOK_TREND_PULLBACK, 12.0)
        forte = contribuicao(s3.PLAYBOOK_TREND_PULLBACK, 40.0)
        self.assertGreater(forte, fraco)
        fraco_range = contribuicao(s3.PLAYBOOK_RANGE_REVERSION, 12.0)
        forte_range = contribuicao(s3.PLAYBOOK_RANGE_REVERSION, 40.0)
        self.assertLess(forte_range, fraco_range)


class Funding(unittest.TestCase):
    def test_uma_unica_parcela(self):
        bucket = pontuar(NEUTRO)["decomposition"][s3.CATEGORY_DERIVATIVES]
        self.assertEqual(list(bucket["features"]), ["funding_pct"])
        self.assertAlmostEqual(bucket["points"], 4.0)  # neutro = metade do peso

    def test_direcional(self):
        custo_long = pontuar(dict(PERFEITO_LONG, funding_pct=0.05), side="long")
        premio_short = pontuar(dict(PERFEITO_LONG, funding_pct=0.05), side="short")
        self.assertAlmostEqual(
            custo_long["decomposition"][s3.CATEGORY_DERIVATIVES]["points"], 0.0)
        self.assertAlmostEqual(
            premio_short["decomposition"][s3.CATEGORY_DERIVATIVES]["points"], 8.0)

    def test_sem_bonus_duplo_por_neutro_e_baixo(self):
        """Funding pequeno não ganha 'bônus de neutro' somado ao mapeamento."""
        neutro = pontuar(dict(PERFEITO_LONG, funding_pct=0.0))
        baixo = pontuar(dict(PERFEITO_LONG, funding_pct=0.001))
        delta = neutro["score"] - baixo["score"]
        self.assertAlmostEqual(delta, 8.0 * (0.001 / 0.1), places=9)


class ConfluenciaSemDegraus(unittest.TestCase):
    def setUp(self):
        self.config = s3.ScoreConfig(include_composite_confluence=True)

    def test_normalizacao_continua_e_monotonica(self):
        anterior = None
        maior_salto = 0.0
        valor = 0.0
        while valor <= 100.0:
            payload = pontuar(dict(PERFEITO_LONG, confluence_pct=valor),
                              config=self.config)
            atual = payload["decomposition"][s3.CATEGORY_CONFLUENCE_COMPOSITE]["points"]
            if anterior is not None:
                self.assertGreaterEqual(atual + 1e-12, anterior, valor)
                maior_salto = max(maior_salto, atual - anterior)
            anterior = atual
            valor += 0.5
        # 0,5 ponto de confluência move no máximo 0,4 ponto de score: sem degrau.
        self.assertLessEqual(maior_salto, 0.4 + 1e-9)

    def test_composto_ocupa_a_escala_das_categorias_substituidas(self):
        payload = pontuar(dict(PERFEITO_LONG, confluence_pct=95.0), config=self.config)
        self.assertNotIn(s3.CATEGORY_STRUCTURE, payload["decomposition"])
        self.assertNotIn(s3.CATEGORY_TRIGGER, payload["decomposition"])
        self.assertAlmostEqual(
            payload["decomposition"][s3.CATEGORY_CONFLUENCE_COMPOSITE]["cap"], 40.0)
        self.assertAlmostEqual(payload["score"], 100.0)


class Fingerprint(unittest.TestCase):
    def test_muda_com_playbook_populacao_e_formula(self):
        base = s3.model_fingerprint(playbook=s3.PLAYBOOK_TREND_PULLBACK)
        self.assertEqual(base, s3.model_fingerprint(playbook=s3.PLAYBOOK_TREND_PULLBACK))
        self.assertNotEqual(base, s3.model_fingerprint(playbook=s3.PLAYBOOK_RANGE_REVERSION))
        self.assertNotEqual(base, s3.model_fingerprint(
            playbook=s3.PLAYBOOK_TREND_PULLBACK,
            config=s3.ScoreConfig(population="OUTRA")))
        self.assertNotEqual(base, s3.model_fingerprint(
            playbook=s3.PLAYBOOK_TREND_PULLBACK,
            config=s3.ScoreConfig(include_composite_confluence=True)))

    def test_playbook_desconhecido_recusado(self):
        with self.assertRaises(ValueError):
            s3.model_fingerprint(playbook="NADA")


class CalibracaoEAprovacao(unittest.TestCase):
    def setUp(self):
        self.payload = pontuar()
        self.fingerprint = self.payload["model_fingerprint"]

    def test_sem_calibracao_nao_ha_probabilidade(self):
        verdict = s3.calibration_verdict(self.payload)
        self.assertEqual(verdict["calibration_state"], s3.STATE_UNAVAILABLE)
        self.assertEqual(verdict["reason_code"], s3.CALIBRATION_ABSENT)
        self.assertIsNone(verdict["probability"])
        self.assertIsNone(verdict["tier"])

    def test_bins_da_v2_nao_servem_de_fallback(self):
        bins_v2 = {"source": "V2_BINS", "p_global": 0.55, "tier": "A"}
        verdict = s3.calibration_verdict(self.payload, bins_v2)
        self.assertEqual(verdict["reason_code"], s3.V2_FALLBACK_FORBIDDEN)
        self.assertFalse(verdict["v2_fallback_used"])
        self.assertIsNone(verdict["probability"])

    def test_fingerprint_e_populacao_precisam_bater(self):
        outra = s3.V3Calibration(model_fingerprint="0" * 64,
                                 population="RESEARCH_SHADOW", sample_size=500)
        self.assertEqual(s3.calibration_verdict(self.payload, outra)["reason_code"],
                         s3.CALIBRATION_FINGERPRINT_MISMATCH)
        populacao = s3.V3Calibration(model_fingerprint=self.fingerprint,
                                     population="OUTRA", sample_size=500)
        self.assertEqual(s3.calibration_verdict(self.payload, populacao)["reason_code"],
                         s3.CALIBRATION_FINGERPRINT_MISMATCH)

    def test_amostra_insuficiente(self):
        pequena = s3.V3Calibration(model_fingerprint=self.fingerprint,
                                   population="RESEARCH_SHADOW", sample_size=10)
        self.assertEqual(s3.calibration_verdict(self.payload, pequena)["reason_code"],
                         s3.CALIBRATION_SAMPLE_INSUFFICIENT)

    def test_calibracao_valida_nao_entrega_tier(self):
        boa = s3.V3Calibration(model_fingerprint=self.fingerprint,
                               population="RESEARCH_SHADOW", sample_size=500)
        verdict = s3.calibration_verdict(self.payload, boa)
        self.assertEqual(verdict["calibration_state"], "AVAILABLE")
        self.assertIsNone(verdict["tier"])
        self.assertFalse(verdict["v2_fallback_used"])

    def test_ev_liquido_exige_probabilidade_fora_da_amostra_e_custos(self):
        self.assertEqual(s3.net_ev(probability_out_of_sample=None, rr_tp2=2.5,
                                   cost_r=0.1)["reason_code"], s3.PROBABILITY_UNAVAILABLE)
        self.assertEqual(s3.net_ev(probability_out_of_sample=1.5, rr_tp2=2.5,
                                   cost_r=0.1)["reason_code"], s3.OUT_OF_SAMPLE_REQUIRED)
        self.assertEqual(s3.net_ev(probability_out_of_sample=0.4, rr_tp2=2.5,
                                   cost_r=None)["reason_code"], s3.COSTS_UNKNOWN)
        bom = s3.net_ev(probability_out_of_sample=0.4, rr_tp2=2.5, cost_r=0.1)
        self.assertTrue(bom["available"])
        self.assertAlmostEqual(bom["ev_r"], 0.3)

    def test_elegibilidade_live_nunca_sai_daqui(self):
        boa = s3.V3Calibration(model_fingerprint=self.fingerprint,
                               population="RESEARCH_SHADOW", sample_size=500)
        ev = s3.net_ev(probability_out_of_sample=0.5, rr_tp2=3.0, cost_r=0.1)
        verdict = s3.economic_verdict(self.payload, boa, ev)
        self.assertEqual(verdict["live_eligibility"], s3.STATE_UNAVAILABLE)
        self.assertEqual(verdict["economic_approval"], "PENDING_SIMULATION")

        sem_calibracao = s3.economic_verdict(self.payload, None, ev)
        self.assertEqual(sem_calibracao["economic_approval"], s3.STATE_UNAVAILABLE)
        self.assertEqual(sem_calibracao["reason_code"], s3.CALIBRATION_ABSENT)


class FronteiraEParidade(unittest.TestCase):
    def test_score_v3_so_usa_biblioteca_padrao(self):
        tree = ast.parse((BACKEND / "services" / "score_v3_service.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"dataclasses", "hashlib", "json", "math", "os", "typing"})

    def test_laboratorio_v2_do_r08a_permanece_intacto(self):
        from services import score_research_service as lab
        pesos = lab.local_v2_weights()
        antes = lab.score_v2_raw(confluence_pct=72.0, adx=28.0, funding_pct=0.01,
                                 weights=pesos)
        pontuar()
        pontuar(config=s3.ScoreConfig(include_composite_confluence=True))
        self.assertEqual(lab.score_v2_raw(confluence_pct=72.0, adx=28.0,
                                          funding_pct=0.01, weights=pesos), antes)
        self.assertEqual(lab.local_v2_weights(), pesos)
        self.assertEqual(lab.FORMULA_V3_ABLATION, "SCORE_V3_CONF_ONLY_ABLATION")
        self.assertNotEqual(s3.SCORE_VERSION, lab.FORMULA_V3_ABLATION)

    def test_modo_default_inativo(self):
        anterior = os.environ.pop(s3.MODE_ENV, None)
        try:
            self.assertEqual(s3.selected_mode(), s3.MODE_INACTIVE)
            self.assertFalse(s3.score_active())
        finally:
            if anterior is not None:
                os.environ[s3.MODE_ENV] = anterior

    def test_manifest_congelado(self):
        manifest = s3.score_manifest()
        self.assertFalse(manifest["outcomes_consulted"])
        self.assertFalse(manifest["approved_for_production"])
        self.assertFalse(manifest["is_probability"])
        self.assertEqual(manifest["caps_total"], 100.0)
        self.assertEqual(len(manifest["features"]), len(s3.ALLOWLIST))
        self.assertEqual(set(manifest["fingerprints"]), set(s3.PLAYBOOKS))

    def test_config_invalida_recusada(self):
        for kwargs in ({"min_evidence_fraction": 0.0}, {"min_evidence_fraction": 2.0},
                       {"population": " "}, {"include_composite_confluence": "sim"}):
            with self.assertRaises(ValueError, msg=kwargs):
                s3.ScoreConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
