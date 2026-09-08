"""R06B3 — coerência do Kelly consultivo, unidades e sugestão de risco.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, credencial, seed privado, holdout ou ordem real.

O defeito: `_compute_dynamic_size` casava `p = P(TP1)` com `b = RR do ALVO
FINAL`, aplicava piso artificial `b >= 0.5`, presumia probabilidade por tier
quando ela faltava, devolvia `None` para Kelly não positivo (confundindo "sem
edge" com "sem dado") e deixava o piso de 0,25% ressuscitar um zero. O rótulo
no app ainda chamava o resultado de "tamanho da posição".

Modelo adotado: TP1_BINARY_PROXY_V1, ADVISORY_ONLY, unidade BANKROLL_RISK_PCT.
Matemática de referência reimplementada AQUI, não importada do código auditado.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste R06B3 (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import calibration_service as calib                 # noqa: E402
from services import recommendation_service as rs                 # noqa: E402
from services import shadow_trade_service as sts                  # noqa: E402
from models.trade_signal import (                                 # noqa: E402
    ConfluenceScore, Indicator, SignalDirection, TradeSignal, TradeType,
)

V2 = calib.CALIBRATION_FORMULA_V2
SEGREDO = "segredo-que-nao-pode-vazar-b3f1"


# ── Matemática de referência, independente do código auditado ───────────────
def kelly_tp1(p: float, b: float) -> float:
    """Kelly binário: ganha +b com prob p, perde 1 com prob (1-p)."""
    return p - (1.0 - p) / b


def raw_esperado(p, b, score, atr_pct):
    vol = 1.0 if atr_pct is None else max(0.5, min(2.0, 0.02 / atr_pct))
    return 100.0 * kelly_tp1(p, b) * 0.25 * max(0.0, min(1.0, score / 100.0)) * vol


def kelly_antigo(p: float, rr_final: float) -> float:
    """A fórmula que existia antes: b = RR do alvo FINAL, com piso 0.5."""
    b = max(rr_final, 0.5)
    return (p * b - (1.0 - p)) / b


def _calc(**kw):
    base = dict(direction="long", entry=100.0, stop_loss=99.0, tp1=101.0,
                score=80.0, prob_tp1=0.52, atr_pct=0.02)
    base.update(kw)
    return rs._compute_dynamic_size(**base)


# ── Calibração e sinais sintéticos ──────────────────────────────────────────
def _bin(lo, hi, p1, p2):
    return {"score_lo": lo, "score_hi": hi, "label": f"[{lo}-{hi})",
            "n_total": 40, "n_wins": 20,
            "p_calibrated": p1, "p_tp2_calibrated": p2}


def _calibracao(formula=V2, *, bins=None, total=120, **extra):
    bins = bins if bins is not None else [
        _bin(15, 40, 0.30, 0.10), _bin(40, 60, 0.52, 0.25), _bin(60, 75, 0.80, 0.40)]
    pares = [(b["score_lo"], b["score_hi"]) for b in bins]
    out = {
        "enabled": True, "source": "teste", "total_resolved": total,
        "contract_version": calib.CALIBRATION_CONTRACT_VERSION,
        "calibration_formula": formula,
        "bins_version": calib.bins_version(pares, formula),
        "score_range": [float(pares[0][0]), float(pares[-1][1])],
        "pairs_formula_provenance": calib.PAIRS_PROVENANCE_UNVERSIONED,
        "p_global": 0.42, "p_tp2_global": 0.21, "bins": bins,
    }
    out.update(extra)
    return out


def _sinal(*, entry=100.0, stop_loss=99.0, tp1=101.0, tp2=103.0, tp3=104.0,
           direction=SignalDirection.LONG, risk_reward=3.0, atr_pct=0.02,
           conf_pct=70.0) -> TradeSignal:
    return TradeSignal(
        symbol="TESTUSDT", timeframe="4h", direction=direction,
        trade_type=TradeType.DAY_TRADE, confidence=0.6,
        entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2, tp3=tp3,
        risk_reward=risk_reward, patterns=[],
        indicators=Indicator(atr=1.0, atr_pct=atr_pct, adx=30.0),
        confluence=ConfluenceScore(total=conf_pct, max_total=100.0,
                                   pct=conf_pct, factors=[]),
        mtf=None, derivatives=None, timestamp=0, signal_strength="moderate",
    )


def _constroi(sig, score=50.0, tier="A", formula=V2):
    """`_build_recommendation` REAL, com a proveniência de score fixada."""
    with patch.object(rs, "compute_score_with_provenance",
                      return_value=rs.ScoreProvenance(
                          score=score, formula_requested=formula,
                          formula_effective=formula, fallback_used=False,
                          fallback_reason=None)):
        return rs._build_recommendation(sig, score, tier)


# ════════════════════════════════════════════════════════════════════════════
#  A. FÓRMULA E GEOMETRIA
# ════════════════════════════════════════════════════════════════════════════
class FormulaEGeometria(unittest.TestCase):

    def test_long_e_short_espelhados_dao_o_mesmo_resultado(self):
        longo = _calc(direction="long", entry=100.0, stop_loss=99.0, tp1=101.5)
        curto = _calc(direction="short", entry=100.0, stop_loss=101.0, tp1=98.5)
        self.assertAlmostEqual(longo.provenance["rr_tp1"],
                               curto.provenance["rr_tp1"], places=9)
        self.assertEqual(longo.pct, curto.pct)
        self.assertEqual(longo.provenance["status"], curto.provenance["status"])
        # e o enum do modelo funciona igual à string
        self.assertEqual(_calc(direction=SignalDirection.LONG).pct, _calc().pct)

    def test_geometria_incoerente_fica_indisponivel(self):
        casos = {
            "tp1 abaixo do entry num long": dict(tp1=98.0),
            "stop acima do entry num long": dict(stop_loss=101.0),
            "short com tp1 acima": dict(direction="short", stop_loss=101.0, tp1=102.0),
            "tp1 igual ao entry": dict(tp1=100.0),
            "stop igual ao entry": dict(stop_loss=100.0),
        }
        for nome, kw in casos.items():
            with self.subTest(caso=nome):
                r = _calc(**kw)
                self.assertIsNone(r.pct)
                self.assertEqual(r.provenance["status"], rs.ADVISORY_STATUS_UNAVAILABLE)
                self.assertEqual(r.provenance["reason_code"],
                                 rs.ADV_REASON_GEOMETRY_INVALID)

    def test_precos_invalidos_ficam_indisponiveis(self):
        for campo in ("entry", "stop_loss", "tp1"):
            for ruim in (None, "100", True, float("nan"), float("inf"), -1.0, 0.0):
                with self.subTest(campo=campo, valor=repr(ruim)):
                    r = _calc(**{campo: ruim})
                    self.assertIsNone(r.pct)
                    self.assertEqual(r.provenance["status"],
                                     rs.ADVISORY_STATUS_UNAVAILABLE)

    def test_direcao_ausente_nao_vira_long(self):
        for ruim in (None, "", "buy", 1, "LONGO"):
            with self.subTest(direction=repr(ruim)):
                r = _calc(direction=ruim)
                self.assertIsNone(r.pct)
                self.assertEqual(r.provenance["reason_code"],
                                 rs.ADV_REASON_DIRECTION_UNKNOWN)

    def test_rr1_abaixo_de_meio_permanece_real(self):
        """Sem piso artificial em `b`: RR 0.40 é 0.40, e o Kelly fica negativo."""
        r = _calc(tp1=100.40, prob_tp1=0.70)
        self.assertAlmostEqual(r.provenance["rr_tp1"], 0.4, places=6)
        self.assertAlmostEqual(r.provenance["kelly_full"], kelly_tp1(0.70, 0.4), places=6)
        self.assertLess(r.provenance["kelly_full"], 0)
        # a fórmula antiga escondia isso levantando b para 0.5
        self.assertGreater(kelly_antigo(0.70, 0.40), 0)

    def test_alterar_apenas_o_alvo_final_nao_muda_a_referencia(self):
        base = _constroi(_sinal(tp2=103.0, risk_reward=3.0))
        longe = _constroi(_sinal(tp2=140.0, risk_reward=40.0))
        self.assertEqual(base.suggested_size_pct, longe.suggested_size_pct)
        self.assertEqual(base.sizing_provenance["rr_tp1"],
                         longe.sizing_provenance["rr_tp1"])
        self.assertEqual(base.sizing_provenance["kelly_full"],
                         longe.sizing_provenance["kelly_full"])

    def test_alterar_prob_tp2_nao_muda_a_formula_baseada_em_tp1(self):
        antes = self._com_calibracao(_bin(40, 60, 0.52, 0.10))
        depois = self._com_calibracao(_bin(40, 60, 0.52, 0.50))
        self.assertEqual(antes.suggested_size_pct, depois.suggested_size_pct)
        self.assertEqual(antes.sizing_provenance["probability_used"],
                         depois.sizing_provenance["probability_used"])

    def _com_calibracao(self, bin_do_meio):
        anterior = calib._cache.get("data")
        try:
            calib._cache["data"] = _calibracao(bins=[
                _bin(15, 40, 0.30, 0.10), bin_do_meio, _bin(60, 75, 0.80, 0.40)])
            return _constroi(_sinal(), score=50.0)
        finally:
            calib._cache["data"] = anterior

    def test_probabilidade_zero_e_um(self):
        zero = _calc(prob_tp1=0.0)
        self.assertEqual(zero.pct, 0.0)
        self.assertEqual(zero.provenance["status"], rs.ADVISORY_STATUS_NO_POSITIVE_EDGE)
        self.assertAlmostEqual(zero.provenance["kelly_full"], -1.0, places=9)
        um = _calc(prob_tp1=1.0)
        self.assertAlmostEqual(um.provenance["kelly_full"], 1.0, places=9)
        self.assertEqual(um.provenance["status"], rs.ADVISORY_STATUS_READY)

    def test_probabilidade_fora_de_zero_um_e_invalida(self):
        for ruim in (1.5, -0.1, float("nan"), float("inf"), True, "0.5"):
            with self.subTest(p=repr(ruim)):
                r = _calc(prob_tp1=ruim)
                self.assertIsNone(r.pct)
                self.assertEqual(r.provenance["reason_code"], rs.ADV_REASON_PROB_INVALID)

    def test_atr_ausente_usa_multiplicador_neutro_com_motivo(self):
        r = _calc(atr_pct=None)
        self.assertEqual(r.provenance["status"], rs.ADVISORY_STATUS_READY)
        self.assertIn("ATR n/d", r.rationale)
        self.assertAlmostEqual(r.provenance["raw_pct"],
                               raw_esperado(0.52, 1.0, 80.0, None), places=9)

    def test_atr_presente_e_invalido_deixa_a_referencia_indisponivel(self):
        for ruim in (0.0, -0.01, float("nan"), float("inf"), "0.02", True):
            with self.subTest(atr=repr(ruim)):
                r = _calc(atr_pct=ruim)
                self.assertIsNone(r.pct)
                self.assertEqual(r.provenance["reason_code"], rs.ADV_REASON_ATR_INVALID)

    def test_score_invalido_e_score_zero_sao_coisas_diferentes(self):
        for ruim in (None, -1.0, 100.1, float("nan"), True, "80"):
            with self.subTest(score=repr(ruim)):
                self.assertEqual(_calc(score=ruim).provenance["reason_code"],
                                 rs.ADV_REASON_SCORE_INVALID)
        zero = _calc(score=0.0, prob_tp1=0.70)
        self.assertEqual(zero.pct, 0.0)
        self.assertEqual(zero.provenance["status"], rs.ADVISORY_STATUS_ZERO_REFERENCE)

    # ── Casos numéricos obrigatórios (score=80, ATR=0.02 ⇒ vol_mult 1.0) ────
    def test_caso_p52_rr1_1_vs_rr_final_3(self):
        r = _calc(prob_tp1=0.52, tp1=101.0)          # RR1 = 1
        self.assertAlmostEqual(kelly_antigo(0.52, 3.0) * 100 * 0.25 * 0.8, 7.2, places=9)
        self.assertAlmostEqual(r.provenance["raw_pct"], 0.8, places=9)
        self.assertAlmostEqual(r.pct, 0.8, places=9)
        self.assertEqual(r.provenance["status"], rs.ADVISORY_STATUS_READY)

    def test_caso_p70_rr1_1_o_cap_esconde_a_correcao(self):
        r = _calc(prob_tp1=0.70, tp1=101.0)
        self.assertAlmostEqual(kelly_antigo(0.70, 3.0) * 100 * 0.25 * 0.8, 12.0, places=9)
        self.assertAlmostEqual(r.provenance["raw_pct"], 8.0, places=9)
        # raw caiu de 12% para 8% — mas o teto de 1% iguala os dois finais.
        # É por isso que comparar só o valor final esconde uma correção real.
        self.assertEqual(r.pct, 1.0)
        self.assertEqual(rs.SIZE_MAX_PCT, 1.0)

    def test_caso_p50_rr1_1_kelly_zero(self):
        r = _calc(prob_tp1=0.50, tp1=101.0)
        self.assertAlmostEqual(r.provenance["kelly_full"], 0.0, places=12)
        self.assertEqual(r.pct, 0.0)
        self.assertEqual(r.provenance["status"], rs.ADVISORY_STATUS_NO_POSITIVE_EDGE)

    def test_caso_p70_rr1_040_kelly_negativo(self):
        r = _calc(prob_tp1=0.70, tp1=100.40)
        self.assertAlmostEqual(r.provenance["kelly_full"], -0.05, places=6)
        self.assertEqual(r.pct, 0.0)

    def test_caso_score_zero_sem_piso(self):
        r = _calc(prob_tp1=0.70, tp1=101.0, score=0.0)
        self.assertEqual(r.pct, 0.0)
        self.assertNotEqual(r.pct, rs.SIZE_MIN_PCT)
        self.assertEqual(r.provenance["raw_pct"], 0.0)

    def test_bruto_pre_cap_e_final_sao_distinguiveis(self):
        r = _calc(prob_tp1=0.70, tp1=101.0)
        self.assertAlmostEqual(r.provenance["kelly_full"], 0.4, places=9)
        self.assertAlmostEqual(r.provenance["raw_pct"], 8.0, places=9)
        self.assertEqual(r.provenance["final_pct"], r.pct)
        self.assertNotEqual(r.provenance["raw_pct"], r.provenance["final_pct"])

    def test_caps_e_constantes_nao_mudaram(self):
        self.assertEqual(rs.KELLY_FRACTION, 0.25)
        self.assertEqual(rs.ATR_REFERENCE_PCT, 0.02)
        self.assertEqual((rs.ATR_MULT_FLOOR, rs.ATR_MULT_CEIL), (0.5, 2.0))
        self.assertEqual((rs.SIZE_MIN_PCT, rs.SIZE_MAX_PCT), (0.25, 1.0))


# ════════════════════════════════════════════════════════════════════════════
#  B. CONTRATO DE PROBABILIDADE E AUSÊNCIA
# ════════════════════════════════════════════════════════════════════════════
class ContratoEAusencia(unittest.TestCase):

    def setUp(self):
        self._anterior = calib._cache.get("data")

    def tearDown(self):
        calib._cache["data"] = self._anterior

    def test_contrato_ready_completo_dimensiona(self):
        calib._cache["data"] = _calibracao(V2)
        rec = _constroi(_sinal(), score=50.0)
        self.assertEqual(rec.probability_provenance["status"], calib.PROB_STATUS_READY)
        self.assertEqual(rec.sizing_provenance["status"], rs.ADVISORY_STATUS_READY)
        self.assertEqual(rec.sizing_provenance["probability_used"], 0.52)

    def test_contrato_divergente_nao_dimensiona(self):
        calib._cache["data"] = _calibracao(calib.CALIBRATION_FORMULA_LEGACY)
        rec = _constroi(_sinal(), score=50.0, formula=V2)
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_FORMULA_MISMATCH)
        self.assertIsNone(rec.suggested_size_pct)
        self.assertEqual(rec.sizing_provenance["reason_code"],
                         rs.ADV_REASON_CONTRACT_BLOCKING)

    def test_erro_de_lookup_nao_dimensiona(self):
        calib._cache["data"] = _calibracao(V2)
        with patch.object(calib, "probability_for_score",
                          side_effect=RuntimeError(SEGREDO)):
            rec = _constroi(_sinal(), score=50.0)
        self.assertIsNone(rec.suggested_size_pct)
        self.assertEqual(rec.sizing_provenance["status"], rs.ADVISORY_STATUS_UNAVAILABLE)
        self.assertNotIn(SEGREDO, json.dumps(rec.model_dump(), default=str))

    def test_ready_incompleto_nao_dimensiona(self):
        """Não basta `status == READY`: o veredito valida o contrato inteiro."""
        calib._cache["data"] = _calibracao(V2)
        rec = _constroi(_sinal(), score=50.0)
        quebrado = dict(rec.probability_provenance, bin_index=None)
        with patch.object(rs, "compute_score_with_provenance",
                          return_value=rs.ScoreProvenance(
                              score=50.0, formula_requested=V2,
                              formula_effective=V2, fallback_used=False,
                              fallback_reason=None)), \
             patch.object(calib, "probability_for_score") as _pfs:
            _pfs.return_value = calib.CalibrationProbabilityResult(
                prob_tp1=0.52, prob_tp2=0.25, status=calib.PROB_STATUS_READY,
                reason_code=calib.PROB_REASON_OK, score=50.0,
                score_formula_effective=V2, calibration_formula=V2,
                bins_version=quebrado["bins_version"], bin_index=None,
                fallback_used=False)
            rec2 = rs._build_recommendation(_sinal(), 50.0, "A")
        self.assertIsNone(rec2.suggested_size_pct)
        self.assertEqual(rec2.sizing_provenance["reason_code"],
                         rs.ADV_REASON_CONTRACT_BLOCKING)

    def test_calibracao_indisponivel_nao_cria_probabilidade_por_tier(self):
        calib._cache["data"] = None
        rec = _constroi(_sinal(), score=50.0, tier="A")
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertIsNone(rec.suggested_size_pct)
        self.assertEqual(rec.sizing_provenance["reason_code"],
                         rs.ADV_REASON_PROB_UNAVAILABLE)
        self.assertIsNone(rec.sizing_provenance["probability_used"])
        # a política operacional por tier segue existindo, só não entra aqui
        self.assertEqual(rs._TIER_WR_FALLBACK, {"A+": 0.62, "A": 0.55, "B": 0.50})

    def test_none_e_zero_sao_estados_distintos(self):
        ausente = _calc(prob_tp1=None)
        zero = _calc(prob_tp1=0.0)
        self.assertIsNone(ausente.pct)
        self.assertEqual(zero.pct, 0.0)
        self.assertNotEqual(ausente.provenance["status"], zero.provenance["status"])
        self.assertIsNone(ausente.provenance["probability_used"])
        self.assertEqual(zero.provenance["probability_used"], 0.0)

    def test_p_global_e_prob_tp2_nunca_substituem_a_ausencia(self):
        calib._cache["data"] = _calibracao(V2)
        rec = _constroi(_sinal(), score=9999.0)   # fora dos bins
        self.assertIsNone(rec.prob_tp1)
        self.assertIsNone(rec.suggested_size_pct)
        self.assertNotEqual(rec.sizing_provenance["probability_used"], 0.42)
        self.assertNotEqual(rec.sizing_provenance["probability_used"], 0.25)

    def test_nenhum_motivo_vaza_excecao_ou_objeto(self):
        for r in (_calc(prob_tp1=None), _calc(tp1=98.0), _calc(atr_pct=0.0),
                  _calc(direction=None), _calc(score="x")):
            self.assertIn(r.provenance["reason_code"], rs.ADVISORY_REASON_CODES)
            self.assertTrue(r.provenance["reason_code"].replace("_", "").isalnum())
            self.assertNotIn("Traceback", r.rationale)
            self.assertNotIn("object at 0x", r.rationale)

    def test_falha_interna_suprime_so_a_referencia(self):
        with patch.object(rs, "_adv_finite", side_effect=RuntimeError(SEGREDO)):
            r = _calc()
        self.assertIsNone(r.pct)
        self.assertEqual(r.provenance["reason_code"], rs.ADV_REASON_INTERNAL_ERROR)
        self.assertNotIn(SEGREDO, json.dumps(r.provenance) + r.rationale)


# ════════════════════════════════════════════════════════════════════════════
#  C. INTEGRAÇÃO COMPLETA
# ════════════════════════════════════════════════════════════════════════════
class IntegracaoCompleta(unittest.TestCase):

    def setUp(self):
        self._anterior = calib._cache.get("data")
        calib._cache["data"] = _calibracao(V2)
        # `test_p03_execution_reconciliation` instala um `shadow_trade_service`
        # falso em `sys.modules` e, no teardown, REMOVE a chave em vez de
        # restaurar a original. Rodando depois dele, o `import_module` do código
        # de produção cria um módulo NOVO e o `patch.object(sts, ...)` deste
        # arquivo passaria a mirar outro objeto. Fixamos a chave no mesmo módulo
        # que patchamos — determinismo independente da ordem dos testes.
        self._mod_anterior = sys.modules.get("services.shadow_trade_service")
        sys.modules["services.shadow_trade_service"] = sts

    def tearDown(self):
        calib._cache["data"] = self._anterior
        if self._mod_anterior is None:
            sys.modules.pop("services.shadow_trade_service", None)
        else:
            sys.modules["services.shadow_trade_service"] = self._mod_anterior

    def test_recomendacao_real_carrega_precos_e_proveniencias(self):
        sig = _sinal(entry=100.0, stop_loss=99.0, tp1=101.0, tp2=104.0, risk_reward=4.0)
        rec = _constroi(sig, score=50.0)
        d = rec.model_dump()
        sp = d["sizing_provenance"]
        self.assertEqual(sp["model"], rs.ADVISORY_SIZING_MODEL)
        self.assertEqual(sp["mode"], rs.ADVISORY_SIZING_MODE)
        self.assertEqual(sp["unit"], rs.ADVISORY_SIZING_UNIT)
        self.assertEqual(sp["version"], rs.ADVISORY_SIZING_VERSION)
        self.assertAlmostEqual(sp["rr_tp1"], 1.0, places=9)      # do TP1, não do TP2
        self.assertEqual(sp["probability_used"], d["prob_tp1"])
        self.assertIsNotNone(d["score_provenance"])
        self.assertIsNotNone(d["probability_provenance"])
        self.assertEqual(json.loads(json.dumps(d, default=str))["sizing_provenance"], sp)

    def test_todo_numero_serializado_e_finito(self):
        cenarios = {
            "ready": (_sinal(), 50.0),
            "sem edge": (_sinal(tp1=100.40), 50.0),          # Kelly negativo
            "zero por score": (_sinal(), 0.0),               # score_mult = 0
            "fora dos bins": (_sinal(), 9999.0),             # sem probabilidade
        }
        for nome, (sig, score) in cenarios.items():
            with self.subTest(cenario=nome):
                rec = _constroi(sig, score=score)
                for chave, valor in rec.sizing_provenance.items():
                    if isinstance(valor, float):
                        self.assertTrue(math.isfinite(valor), f"{nome}.{chave}={valor}")

    def test_final_pct_acompanha_o_valor_final(self):
        rec = _constroi(_sinal(), score=50.0)
        self.assertEqual(rec.sizing_provenance["final_pct"], rec.suggested_size_pct)

    def test_zero_continua_zero_com_edge_e_liquidez_ligados(self):
        """Nenhum piso pode ressuscitar um zero em 0,25%."""
        sig = _sinal(tp1=100.40)      # RR1 = 0.4 ⇒ Kelly negativo ⇒ zero
        with patch.object(sts, "EDGE_SIZING_ENABLED", True), \
             patch.object(sts, "LIQ_TIER_SIZING_ENABLED", True):
            rec = _constroi(sig, score=50.0)
        self.assertEqual(rec.suggested_size_pct, 0.0)
        self.assertEqual(rec.sizing_provenance["final_pct"], 0.0)
        self.assertEqual(rec.sizing_provenance["status"],
                         rs.ADVISORY_STATUS_NO_POSITIVE_EDGE)

    def test_ausencia_continua_ausencia_com_multiplicadores_ligados(self):
        calib._cache["data"] = None
        with patch.object(sts, "EDGE_SIZING_ENABLED", True), \
             patch.object(sts, "LIQ_TIER_SIZING_ENABLED", True):
            rec = _constroi(_sinal(), score=50.0)
        self.assertIsNone(rec.suggested_size_pct)
        self.assertIsNone(rec.sizing_provenance["final_pct"])

    def test_multiplicador_positivo_move_valor_e_proveniencia_juntos(self):
        with patch.object(sts, "_edge_mult", return_value=(0.5, "teste")):
            rec = _constroi(_sinal(), score=50.0)
        self.assertEqual(rec.sizing_provenance["final_pct"], rec.suggested_size_pct)
        self.assertIn("edge ×0.50", rec.size_rationale)
        # `raw_pct` continua sendo o bruto do modelo, antes dos espelhamentos
        self.assertNotEqual(rec.sizing_provenance["raw_pct"],
                            rec.sizing_provenance["final_pct"])

    def test_multiplicador_invalido_suprime_a_referencia(self):
        for ruim in (float("nan"), float("inf"), -1.0, None, "x"):
            with self.subTest(mult=repr(ruim)):
                with patch.object(sts, "_edge_mult", return_value=(ruim, "teste")):
                    rec = _constroi(_sinal(), score=50.0)
                self.assertIsNone(rec.suggested_size_pct)
                self.assertEqual(rec.sizing_provenance["reason_code"],
                                 rs.ADV_REASON_MULTIPLIER_INVALID)

    def test_hotfix_do_prob_tp2_no_bot_verdict_preservado(self):
        fonte = (BACKEND / "services" / "recommendation_service.py").read_text()
        bloco = fonte.split("exec_verdict({")[1].split("})")[0]
        self.assertIn('"prob_tp1": prob_tp1', bloco)
        self.assertIn('"prob_tp2": prob_tp2', bloco)
        rec = _constroi(_sinal(), score=50.0)
        self.assertIsNotNone(rec.bot_verdict)


# ════════════════════════════════════════════════════════════════════════════
#  D. INDEPENDÊNCIA OPERACIONAL
# ════════════════════════════════════════════════════════════════════════════
class IndependenciaOperacional(unittest.TestCase):
    """A referência consultiva não pode mover NADA do caminho de execução."""

    def _rec_operacional(self, **consultivo):
        base = {
            "symbol": "TESTUSDT", "entry": 100.0, "stop_loss": 98.0,
            "tp1": 104.0, "tp2": 108.0, "score": 70.0, "edge_score": 2,
            "edge_tags": ["A+", "funding"], "quote_vol_usd": 50_000_000.0,
            "spread_pct": 0.02, "risk_pct": 1.0, "leverage": 5,
            "prob_tp1": 0.60, "prob_tp2": 0.30,
            "score_provenance": {"formula_effective": V2, "fallback_used": False},
            "probability_provenance": {
                "contract_version": calib.CALIBRATION_CONTRACT_VERSION,
                "status": calib.PROB_STATUS_READY,
                "reason_code": calib.PROB_REASON_OK,
                "score_formula_effective": V2, "calibration_formula": V2,
                "bins_version": calib.bins_version(), "bin_index": 1,
                "fallback_used": False,
            },
        }
        base.update(consultivo)
        return base

    #: positivo, zero e ausente — só os campos CONSULTIVOS mudam
    VARIANTES = (
        {"suggested_size_pct": 0.85,
         "sizing_provenance": {"status": "READY", "final_pct": 0.85}},
        {"suggested_size_pct": 0.0,
         "sizing_provenance": {"status": "NO_POSITIVE_EDGE", "final_pct": 0.0}},
        {"suggested_size_pct": None,
         "sizing_provenance": {"status": "UNAVAILABLE", "final_pct": None}},
    )

    def test_exec_verdict_nao_olha_a_referencia_consultiva(self):
        vereditos = [sts.exec_verdict(self._rec_operacional(**v)) for v in self.VARIANTES]
        for v in vereditos[1:]:
            self.assertEqual(v, vereditos[0])

    def test_multiplicadores_reais_nao_olham_a_referencia(self):
        for fn in (sts._edge_mult, sts._liq_tier_mult, sts._conviction_mult):
            with self.subTest(fn=fn.__name__):
                saidas = [fn(self._rec_operacional(**v)) for v in self.VARIANTES]
                for s in saidas[1:]:
                    self.assertEqual(s, saidas[0])

    def test_risk_pct_e_quantidade_nao_mudam(self):
        for v in self.VARIANTES:
            rec = self._rec_operacional(**v)
            self.assertEqual(rec["risk_pct"], 1.0)
            sizing = sts._compute_qty(rec["entry"], rec["stop_loss"],
                                      rec["risk_pct"], 1000.0, leverage=rec["leverage"])
            self.assertIsNotNone(sizing)
            if not hasattr(self, "_ref"):
                self._ref = sizing
            self.assertEqual(sizing, self._ref)

    def test_leverage_da_recomendacao_nao_depende_da_referencia(self):
        base = rs._compute_leverage(100.0, 98.0, "A")
        for v in self.VARIANTES:
            rec = self._rec_operacional(**v)
            self.assertEqual(rs._compute_leverage(rec["entry"], rec["stop_loss"], "A"),
                             base)

    def test_executor_nunca_le_a_sugestao_consultiva(self):
        """Reforço de grep sobre CÓDIGO — a prova principal são os testes acima."""
        for arquivo in ("shadow_trade_service.py", "trade_manager_service.py",
                        "risk_service.py", "portfolio_service.py"):
            caminho = BACKEND / "services" / arquivo
            if not caminho.exists():
                continue
            texto = caminho.read_text()
            self.assertNotIn("suggested_size_pct", texto, arquivo)
            self.assertNotIn("sizing_provenance", texto, arquivo)

    def test_nenhuma_ordem_real_foi_tocada(self):
        self.assertEqual(_NET_ATTEMPTS, [])


# ════════════════════════════════════════════════════════════════════════════
#  E. FRONTEND E ROTULAGEM
# ════════════════════════════════════════════════════════════════════════════
class Frontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        base = BACKEND.parent / "frontend" / "src"
        cls.painel = (base / "components" / "RecommendationsPanel.tsx").read_text()
        cls.tipos = (base / "types" / "index.ts").read_text()

    def test_rotulo_deixou_de_dizer_tamanho_da_posicao(self):
        self.assertNotIn("Size sugerido", self.painel)
        self.assertNotIn("TAMANHO da posição", self.painel)
        self.assertIn("Referência de risco até o TP1", self.painel)

    def test_explicacao_acessivel_presente(self):
        self.assertIn("Modelo simplificado. Este valor não define o tamanho das ordens do bot.",
                      self.painel)

    def test_estados_tem_apresentacao_propria(self):
        for texto in ("Sem referência positiva de risco neste modelo",
                      "Referência de risco até o TP1 indisponível",
                      "Referência legada"):
            self.assertIn(texto, self.painel)

    def test_zero_nao_e_tratado_como_ausencia(self):
        """Zero cai no ramo NO_POSITIVE_EDGE/ZERO_REFERENCE, que renderiza."""
        bloco = self.painel.split("sizing_provenance")[1][:2200]
        self.assertIn("NO_POSITIVE_EDGE", bloco)
        self.assertIn("UNAVAILABLE", bloco)
        self.assertNotIn("suggested_size_pct > 0", bloco)

    def test_payload_legado_nao_recebe_o_modelo_novo(self):
        bloco = self.painel.split("sizing_provenance")[1][:2200]
        self.assertIn("const legado = sp == null", bloco)
        self.assertIn("Referência legada", bloco)

    def test_tipo_declara_o_contrato_como_opcional(self):
        self.assertIn("sizing_provenance?:", self.tipos)
        for campo in ("status?:", "reason_code?:", "rr_tp1?:", "kelly_full?:",
                      "raw_pct?:", "final_pct?:"):
            self.assertIn(campo, self.tipos, campo)
        # e o tipo já enumera os estados que o painel trata
        for estado in ("'READY'", "'ZERO_REFERENCE'", "'NO_POSITIVE_EDGE'",
                       "'UNAVAILABLE'"):
            self.assertIn(estado, self.tipos, estado)

    def test_nao_muda_gates_botoes_nem_ordenacao(self):
        bloco = self.painel.split("sizing_provenance")[1][:2200]
        for proibido in ("bot_verdict", "entry_grade", "onAddToManager",
                         "disabled", "sort", "filter"):
            self.assertNotIn(proibido, bloco, proibido)

    def test_comentario_nao_confunde_mais_risco_com_notional(self):
        # o comentário é quebrado em linhas — normaliza o espaço antes do grep
        bloco = " ".join(self.painel.split("R06B3")[1][:400].split())
        self.assertIn("não o tamanho da posição", bloco)
        self.assertIn("não notional", bloco)
        self.assertIn("não margem", bloco)
        self.assertIn("fração TEÓRICA de risco da banca", bloco)


if __name__ == "__main__":
    unittest.main()
