"""R06B2.1 — fechamento das quatro lacunas do contrato de probabilidade.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, credencial, seed privado ou holdout. Nenhuma ordem real.

As quatro lacunas comprovadas na revisão do R06B2:

1. exceção ao construir o contrato deixava `probability_provenance=None` e,
   sem `fallback_used`, a rec podia passar — falha ABERTA;
2. `calibration_contract_verdict` confiava no campo `status`, então um objeto
   incompleto como `{"status": "READY"}` era aceito;
3. `bins_version` resumia fórmula + quantidade + extremos, então mover uma
   divisão INTERNA não mudava a versão;
4. `snapshot_service` recalculava a probabilidade histórica com o cache de hoje,
   sem saber a fórmula que gerou aquele score antigo.

Os testes aqui são COMPORTAMENTAIS. Grep aparece só como reforço, nunca como
prova principal.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timezone
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
    raise RuntimeError(f"REDE BLOQUEADA no teste R06B2.1 (hermético): {a[:1]}")


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
from services import snapshot_service as snap                     # noqa: E402
from models.trade_signal import (                                 # noqa: E402
    ConfluenceScore, Indicator, SignalDirection, TradeSignal, TradeType,
)

V2 = calib.CALIBRATION_FORMULA_V2
LEGACY = calib.CALIBRATION_FORMULA_LEGACY
SEGREDO = "detalhe-interno-que-nao-pode-vazar-9f3a"


def _bin(lo, hi, p1, p2):
    return {"score_lo": lo, "score_hi": hi, "label": f"[{lo}-{hi})",
            "n_total": 40, "n_wins": 20,
            "p_calibrated": p1, "p_tp2_calibrated": p2}


_BINS_PADRAO = [_bin(15, 40, 0.30, 0.10), _bin(40, 60, 0.55, 0.25),
                _bin(60, 75, 0.80, 0.40)]


def _calibracao(formula=V2, *, bins=None, total=120, **extra):
    """Calibração sintética COERENTE (versão, faixa e fingerprint conferem)."""
    bins = _BINS_PADRAO if bins is None else bins
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


def _sinal(ind=None, *, conf_pct=70.0, risk_reward=2.0) -> TradeSignal:
    return TradeSignal(
        symbol="TESTUSDT", timeframe="4h", direction=SignalDirection.LONG,
        trade_type=TradeType.DAY_TRADE, confidence=0.6,
        entry=100.0, stop_loss=98.0, tp1=102.0, tp2=104.0, tp3=106.0,
        risk_reward=risk_reward, patterns=[],
        indicators=ind if ind is not None else Indicator(atr=1.0, atr_pct=0.02, adx=30.0),
        confluence=ConfluenceScore(total=conf_pct, max_total=100.0,
                                   pct=conf_pct, factors=[]),
        mtf=None, derivatives=None, timestamp=0, signal_strength="moderate",
    )


def _provenance(status=calib.PROB_STATUS_READY, **over):
    pronto = status == calib.PROB_STATUS_READY
    base = {
        "contract_version": calib.CALIBRATION_CONTRACT_VERSION,
        "status": status,
        "reason_code": (calib.PROB_REASON_OK if pronto
                        else calib.PROB_REASON_NO_CALIBRATION),
        "score_formula_effective": V2,
        "calibration_formula": V2,
        "bins_version": calib.bins_version(),
        "bin_index": 1 if pronto else None,
        "fallback_used": False,
    }
    base.update(over)
    return base


def _rec(prov=None, *, p1=0.55, p2=0.25, score_prov=None, **extra):
    r = {"symbol": "TESTUSDT", "entry": 100.0, "stop_loss": 98.0,
         "tp1": 104.0, "tp2": 108.0, "score": 70.0, "edge_score": 2,
         "prob_tp1": p1, "prob_tp2": p2,
         "score_provenance": (score_prov if score_prov is not None
                              else {"formula_effective": V2, "fallback_used": False})}
    if prov is not None:
        r["probability_provenance"] = prov
    r.update(extra)
    return r


# ════════════════════════════════════════════════════════════════════════════
#  1. FALHA DE CONSTRUÇÃO — antes falhava ABERTA
# ════════════════════════════════════════════════════════════════════════════
class FalhaDeConstrucao(unittest.TestCase):

    def setUp(self):
        self._anterior = calib._cache.get("data")
        calib._cache["data"] = _calibracao(V2)

    def tearDown(self):
        calib._cache["data"] = self._anterior

    def _com_lookup_quebrado(self, alvo="probability_for_score"):
        def _explode(*a, **k):
            raise RuntimeError(SEGREDO)
        return patch.object(calib, alvo, _explode)

    def _constroi(self, score=50.0, formula=V2):
        with patch.object(rs, "compute_score_with_provenance",
                          return_value=rs.ScoreProvenance(
                              score=score, formula_requested=formula,
                              formula_effective=formula, fallback_used=False,
                              fallback_reason=None)):
            rec = rs._build_recommendation(_sinal(), score, "A")
        self.assertIsNotNone(rec)
        return rec

    def test_excecao_no_lookup_vira_contrato_invalido_explicito(self):
        with self._com_lookup_quebrado():
            rec = self._constroi()
        prov = rec.probability_provenance
        self.assertIsNotNone(prov)
        self.assertEqual(prov["status"], calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
        self.assertEqual(prov["reason_code"], calib.PROB_REASON_LOOKUP_FAILED)
        self.assertEqual(prov["contract_version"], calib.CALIBRATION_CONTRACT_VERSION)
        self.assertIsNone(rec.prob_tp1)
        self.assertIsNone(rec.prob_tp2)

    def test_excecao_na_leitura_do_cache_tambem_falha_fechado(self):
        with self._com_lookup_quebrado("cached_calibration"):
            rec = self._constroi()
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)

    def test_falha_suspende_o_sizing_dinamico(self):
        with self._com_lookup_quebrado():
            rec = self._constroi()
        self.assertIsNone(rec.suggested_size_pct)
        self.assertIn("Calibração incompatível", rec.size_rationale)
        # e NÃO caiu no fallback por tier
        self.assertNotIn("Kelly", rec.size_rationale)

    def test_falha_bloqueia_o_bot_verdict(self):
        with self._com_lookup_quebrado():
            rec = self._constroi()
        self.assertIsNotNone(rec.bot_verdict)
        self.assertFalse(rec.bot_verdict["ok"])
        self.assertEqual(rec.bot_verdict["blocked_by"],
                         calib.CALIBRATION_CONTRACT_GATE)

    def test_ready_chega_com_tp2_ao_bot_verdict(self):
        rec = self._constroi()
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_READY)
        self.assertIsNotNone(rec.prob_tp2)
        self.assertIsNotNone(rec.bot_verdict)
        self.assertNotEqual(rec.bot_verdict.get("blocked_by"),
                            calib.CALIBRATION_CONTRACT_GATE)

    def test_falha_bloqueia_a_autoexecucao(self):
        with self._com_lookup_quebrado():
            rec = self._constroi()
        v = sts._calibration_contract_verdict(rec.model_dump(),
                                              require_current_contract=True)
        self.assertFalse(v["ok"])
        self.assertEqual(v["blocked_by"], calib.CALIBRATION_CONTRACT_GATE)

    def test_nenhuma_mensagem_da_excecao_vaza(self):
        with self._com_lookup_quebrado():
            rec = self._constroi()
        serializado = json.dumps(rec.model_dump(), default=str)
        self.assertNotIn(SEGREDO, serializado)
        self.assertNotIn("RuntimeError", serializado)
        self.assertNotIn("Traceback", serializado)

    def test_recomendacao_nova_nunca_fica_sem_proveniencia(self):
        cenarios = [
            ("ok", None, _calibracao(V2), V2),
            ("mismatch", None, _calibracao(V2), LEGACY),
            ("sem calibracao", None, None, V2),
            ("lookup quebrado", "probability_for_score", _calibracao(V2), V2),
        ]
        for nome, quebra, cache, formula in cenarios:
            with self.subTest(cenario=nome):
                calib._cache["data"] = cache
                ctx = self._com_lookup_quebrado(quebra) if quebra else _nada()
                with ctx:
                    rec = self._constroi(formula=formula)
                self.assertIsNotNone(rec.probability_provenance, nome)
                self.assertIn(rec.probability_provenance["status"],
                              calib.PROB_STATUSES, nome)

    def test_literais_do_caminho_de_falha_batem_com_o_servico(self):
        """O caminho de falha não pode depender do import que acabou de falhar."""
        self.assertEqual(rs._CALIB_CONTRACT_VERSION,
                         calib.CALIBRATION_CONTRACT_VERSION)
        self.assertEqual(rs._CALIB_STATUS_INVALID,
                         calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
        self.assertEqual(rs._CALIB_REASON_LOOKUP_FAILED,
                         calib.PROB_REASON_LOOKUP_FAILED)
        self.assertIn(rs._CALIB_REASON_LOOKUP_FAILED, calib.PROB_REASON_CODES)


class _nada:
    def __enter__(self): return None
    def __exit__(self, *a): return False


# ════════════════════════════════════════════════════════════════════════════
#  2. VEREDICT — READY isolado não é mais aceito
# ════════════════════════════════════════════════════════════════════════════
class VeredictoIntegral(unittest.TestCase):

    def _ok(self, rec, **kw):
        return calib.calibration_contract_verdict(rec, **kw)["ok"]

    def test_status_ready_isolado_bloqueia(self):
        self.assertFalse(self._ok(_rec({"status": "READY"})))

    def test_ready_bem_formado_passa(self):
        self.assertTrue(self._ok(_rec(_provenance())))

    def test_ready_sem_formula_bloqueia(self):
        for campo in ("score_formula_effective", "calibration_formula"):
            with self.subTest(campo=campo):
                self.assertFalse(self._ok(_rec(_provenance(**{campo: None}))))

    def test_ready_com_formulas_diferentes_bloqueia(self):
        self.assertFalse(self._ok(_rec(_provenance(calibration_formula=LEGACY))))

    def test_ready_com_fallback_used_true_bloqueia(self):
        self.assertFalse(self._ok(_rec(_provenance(fallback_used=True))))
        # e nem `None`/ausente passa: exige exatamente False
        self.assertFalse(self._ok(_rec(_provenance(fallback_used=None))))

    def test_ready_com_probabilidade_ruim_bloqueia(self):
        for p1, p2 in ((None, 0.25), (0.55, None), (float("nan"), 0.25),
                       (float("inf"), 0.25), (1.5, 0.25), (-0.1, -0.2),
                       ("0.5", 0.25)):
            with self.subTest(p1=p1, p2=p2):
                self.assertFalse(self._ok(_rec(_provenance(), p1=p1, p2=p2)))

    def test_ready_com_tp2_acima_de_tp1_bloqueia(self):
        self.assertFalse(self._ok(_rec(_provenance(), p1=0.40, p2=0.90)))

    def test_ready_com_tp2_igual_passa(self):
        self.assertTrue(self._ok(_rec(_provenance(), p1=0.40, p2=0.40)))

    def test_ready_com_bin_index_invalido_bloqueia(self):
        for idx in (None, -1, True, False, 1.5, "1"):
            with self.subTest(bin_index=repr(idx)):
                self.assertFalse(self._ok(_rec(_provenance(bin_index=idx))))

    def test_ready_com_envelope_ruim_bloqueia(self):
        for over in ({"contract_version": None}, {"contract_version": "r00"},
                     {"reason_code": None}, {"reason_code": "INVENTADO"},
                     {"bins_version": "x"}, {"bins_version": None},
                     {"bins_version": f"{V2}:md5:" + "a" * 64}):
            with self.subTest(over=over):
                self.assertFalse(self._ok(_rec(_provenance(**over))))

    def test_unavailable_bem_formado_nao_bloqueia(self):
        prov = _provenance(calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertTrue(self._ok(_rec(prov, p1=None, p2=None)))

    def test_unavailable_com_probabilidade_bloqueia(self):
        prov = _provenance(calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertFalse(self._ok(_rec(prov, p1=0.6, p2=0.2)))
        self.assertFalse(self._ok(_rec(prov, p1=None, p2=0.2)))

    def test_unavailable_que_afirma_bin_bloqueia(self):
        prov = _provenance(calib.PROB_STATUS_CALIBRATION_UNAVAILABLE, bin_index=1)
        self.assertFalse(self._ok(_rec(prov, p1=None, p2=None)))

    def test_status_desconhecido_bloqueia(self):
        for status in ("QUALQUER", "", None, 7, "ready"):
            with self.subTest(status=repr(status)):
                self.assertFalse(self._ok(_rec(_provenance(status=status))))

    def test_proveniencia_presente_mas_malformada_bloqueia(self):
        for ruim in ("nao-e-dict", 7, [], 0.5):
            with self.subTest(prov=repr(ruim)):
                self.assertFalse(self._ok(_rec(ruim)))

    def test_estados_bloqueantes_seguem_bloqueantes(self):
        for status in sorted(calib.BLOCKING_PROB_STATUSES):
            with self.subTest(status=status):
                self.assertFalse(self._ok(_rec(_provenance(status=status))))

    def test_execucao_exige_contrato_atual(self):
        legado = _rec(None, score_prov=None)
        legado.pop("score_provenance")
        # leitura/exibição de histórico: continua legível
        self.assertTrue(self._ok(legado))
        # caminho oficial de execução: não vira ordem nova
        self.assertFalse(self._ok(legado, require_current_contract=True))

    def test_payload_legado_e_serializavel_mas_nao_vira_ordem(self):
        legado = rs.Recommendation.model_validate({
            "tier": "A", "score": 70.0, "symbol": "TESTUSDT", "timeframe": "4h",
            "direction": "long", "confidence": 0.6, "risk_reward": 2.0,
            "entry": 100.0, "stop_loss": 98.0, "tp2": 104.0, "summary": "x",
            "signal": _sinal().model_dump(), "prob_tp1": 0.6,
        })
        d = legado.model_dump()
        self.assertEqual(json.loads(json.dumps(d, default=str))["prob_tp1"], 0.6)
        self.assertIsNone(legado.probability_provenance)
        self.assertTrue(self._ok(d))
        self.assertFalse(self._ok(d, require_current_contract=True))

    def test_atalho_booleano_usa_o_mesmo_helper(self):
        for rec in (_rec(_provenance()), _rec({"status": "READY"}), _rec(None)):
            for exigir in (False, True):
                self.assertEqual(
                    calib.contract_is_blocking(rec, require_current_contract=exigir),
                    not self._ok(rec, require_current_contract=exigir))

    def test_regra_continua_definida_uma_unica_vez(self):
        calib_src = (BACKEND / "services" / "calibration_service.py").read_text()
        shadow_src = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        self.assertEqual(calib_src.count("def calibration_contract_verdict("), 1)
        self.assertEqual(shadow_src.count("def calibration_contract_verdict("), 0)


# ════════════════════════════════════════════════════════════════════════════
#  3. FINGERPRINT INTEGRAL DOS BINS
# ════════════════════════════════════════════════════════════════════════════
class FingerprintDosBins(unittest.TestCase):

    def test_determinismo(self):
        a = calib.bins_fingerprint(calib.SCORE_BINS_V2, V2)
        b = calib.bins_fingerprint(calib.SCORE_BINS_V2, V2)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)
        # aceita tanto pares quanto dicts de bin — mesmo hash
        como_dicts = [_bin(lo, hi, 0.5, 0.2) for lo, hi in calib.SCORE_BINS_V2]
        self.assertEqual(calib.bins_fingerprint(como_dicts, V2), a)

    def test_borda_INTERNA_alterada_muda_o_hash(self):
        """A lacuna do R06B2: mesma fórmula, mesma quantidade, mesmos extremos."""
        original = [(15, 31), (31, 40), (40, 75)]
        movido = [(15, 35), (35, 40), (40, 75)]
        self.assertEqual(len(original), len(movido))
        self.assertEqual((original[0][0], original[-1][1]),
                         (movido[0][0], movido[-1][1]))
        self.assertNotEqual(calib.bins_fingerprint(original, V2),
                            calib.bins_fingerprint(movido, V2))

    def test_formula_diferente_muda_o_hash(self):
        self.assertNotEqual(calib.bins_fingerprint(calib.SCORE_BINS_V2, V2),
                            calib.bins_fingerprint(calib.SCORE_BINS_V2, LEGACY))
        self.assertNotEqual(calib.bins_version(calib.SCORE_BINS_V2, V2),
                            calib.bins_version(calib.SCORE_BINS_LEGACY, LEGACY))

    def test_hash_nao_carrega_data_probabilidade_nem_amostra(self):
        magros = [{"score_lo": lo, "score_hi": hi} for lo, hi in calib.SCORE_BINS_V2]
        gordos = [_bin(lo, hi, 0.9, 0.8) for lo, hi in calib.SCORE_BINS_V2]
        for b in gordos:
            b["n_total"] = 999
        self.assertEqual(calib.bins_fingerprint(magros, V2),
                         calib.bins_fingerprint(gordos, V2))

    def test_hash_falso_bloqueia(self):
        c = _calibracao(V2, bins_version=f"{V2}:sha256:" + "0" * 64)
        r = calib.probability_for_score(50.0, V2, c)
        self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
        self.assertEqual(r.reason_code, calib.PROB_REASON_BINS_VERSION_MISMATCH)
        self.assertIsNone(r.prob_tp1)

    def test_repartição_interna_alterada_bloqueia_o_lookup(self):
        """Bins repartidos por dentro, `bins_version` do contrato antigo."""
        antigos = [_bin(15, 40, 0.30, 0.10), _bin(40, 60, 0.55, 0.25),
                   _bin(60, 75, 0.80, 0.40)]
        novos = [_bin(15, 45, 0.30, 0.10), _bin(45, 60, 0.55, 0.25),
                 _bin(60, 75, 0.80, 0.40)]
        c = _calibracao(V2, bins=novos)
        c["bins_version"] = calib.bins_version(
            [(b["score_lo"], b["score_hi"]) for b in antigos], V2)
        r = calib.probability_for_score(50.0, V2, c)
        self.assertEqual(r.reason_code, calib.PROB_REASON_BINS_VERSION_MISMATCH)

    def test_score_range_divergente_bloqueia(self):
        for faixa in ([15, 99], [0, 75], [15], None, "15-75", [15, "75"]):
            with self.subTest(faixa=repr(faixa)):
                c = _calibracao(V2, score_range=faixa)
                r = calib.probability_for_score(50.0, V2, c)
                self.assertEqual(r.reason_code, calib.PROB_REASON_SCORE_RANGE_MISMATCH)

    def test_total_resolved_invalido_bloqueia(self):
        for total in (None, "120", float("nan"), float("inf"), True, [120]):
            with self.subTest(total=repr(total)):
                r = calib.probability_for_score(50.0, V2, _calibracao(V2, total=total))
                self.assertEqual(r.status,
                                 calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
                self.assertEqual(r.reason_code,
                                 calib.PROB_REASON_TOTAL_RESOLVED_INVALID)

    def test_amostra_abaixo_do_minimo_continua_indisponivel(self):
        r = calib.probability_for_score(
            50.0, V2, _calibracao(V2, total=calib.MIN_SAMPLE_TOTAL - 1))
        self.assertEqual(r.status, calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertNotIn(r.status, calib.BLOCKING_PROB_STATUSES)

    def test_contract_version_nao_suportada_bloqueia(self):
        for v in (None, "r06b2.0", "", 1):
            with self.subTest(v=repr(v)):
                r = calib.probability_for_score(50.0, V2, _calibracao(V2, contract_version=v))
                self.assertEqual(r.reason_code,
                                 calib.PROB_REASON_CONTRACT_VERSION_UNSUPPORTED)

    def test_overlap_e_desordem_bloqueiam(self):
        sobrepostos = [_bin(15, 50, 0.3, 0.1), _bin(40, 75, 0.5, 0.2)]
        c = _calibracao(V2, bins=sobrepostos)
        self.assertEqual(calib.probability_for_score(20.0, V2, c).reason_code,
                         calib.PROB_REASON_BINS_AMBIGUOUS)
        desordenados = [_bin(40, 75, 0.5, 0.2), _bin(15, 30, 0.3, 0.1)]
        c2 = _calibracao(V2, bins=desordenados)
        self.assertEqual(calib.probability_for_score(20.0, V2, c2).reason_code,
                         calib.PROB_REASON_BINS_UNSORTED)

    def test_calibracao_real_valida_de_ponta_a_ponta(self):
        """A tabela produzida pelo próprio serviço passa nas novas validações."""
        pares = [(float(lo) + 0.5, st)
                 for lo, _ in calib.SCORE_BINS
                 for st in ("won_tp1", "won_tp2", "lost")] * 4
        out = calib.compute_calibration_from_pairs(pares, source="teste")
        self.assertGreaterEqual(out["total_resolved"], calib.MIN_SAMPLE_TOTAL)
        alvo = float(calib.SCORE_BINS[1][0]) + 0.5
        r = calib.probability_for_score(alvo, calib.active_calibration_formula(), out)
        self.assertEqual(r.status, calib.PROB_STATUS_READY, r.reason_code)
        self.assertIsNotNone(r.prob_tp1)
        # e o veredito integral também aceita
        self.assertTrue(calib.calibration_contract_verdict(
            {"probability_provenance": r.as_provenance(),
             "prob_tp1": r.prob_tp1, "prob_tp2": r.prob_tp2},
            require_current_contract=True)["ok"])

    def test_bordas_atuais_nao_mudaram(self):
        self.assertEqual(calib.SCORE_BINS_LEGACY[0], (55, 60))
        self.assertEqual(calib.SCORE_BINS_LEGACY[-1], (95, 100.1))
        self.assertEqual(calib.SCORE_BINS_V2[0], (15, 31))
        self.assertEqual(calib.SCORE_BINS_V2[-1], (63, 75))


# ════════════════════════════════════════════════════════════════════════════
#  4. SNAPSHOT IMUTÁVEL
# ════════════════════════════════════════════════════════════════════════════
class SnapshotImutavel(unittest.TestCase):

    def setUp(self):
        self._anterior = calib._cache.get("data")

    def tearDown(self):
        calib._cache["data"] = self._anterior

    def _features(self, **over):
        rec = {"symbol": "TESTUSDT", "timeframe": "4h", "tier": "A",
               "score": 50.0, "prob_tp1": 0.55, "prob_tp2": 0.25,
               "risk_reward": 2.0, "signal": _sinal().model_dump(),
               "probability_provenance": _provenance(),
               "score_provenance": {"formula_effective": V2, "fallback_used": False}}
        rec.update(over)
        return snap._extract_features(rec, datetime.now(timezone.utc))

    def test_snapshot_novo_congela_probabilidade_e_proveniencia(self):
        feat = self._features()
        contrato = feat["probability_contract"]
        self.assertEqual(contrato["schema_version"], 1)
        self.assertEqual(contrato["prob_tp1"], 0.55)
        self.assertEqual(contrato["prob_tp2"], 0.25)
        self.assertEqual(contrato["probability_provenance"]["status"],
                         calib.PROB_STATUS_READY)
        self.assertEqual(contrato["score_provenance"]["formula_effective"], V2)
        self.assertEqual(json.loads(json.dumps(feat, default=str))[
            "probability_contract"]["prob_tp1"], 0.55)

    def test_leitura_devolve_exatamente_o_valor_original(self):
        contrato = self._features()["probability_contract"]
        self.assertEqual(calib.probabilities_from_contract(contrato), (0.55, 0.25))

    def test_calibracao_posterior_diferente_nao_altera_o_exibido(self):
        """O ponto do defeito 4: o histórico não é reinterpretado."""
        contrato = self._features()["probability_contract"]
        antes = calib.probabilities_from_contract(contrato)
        # a calibração de HOJE muda completamente (outros bins, outra fórmula)
        calib._cache["data"] = _calibracao(
            LEGACY, bins=[_bin(0, 100, 0.99, 0.98)])
        depois = calib.probabilities_from_contract(contrato)
        self.assertEqual(antes, depois)
        self.assertEqual(depois, (0.55, 0.25))

    def test_snapshot_legado_sem_proveniencia_retorna_none(self):
        for legado in (None, {}, {"schema_version": 1, "prob_tp1": 0.7},
                       {"probability_provenance": None, "prob_tp1": 0.7},
                       "nao-e-dict"):
            with self.subTest(legado=repr(legado)):
                self.assertEqual(calib.probabilities_from_contract(legado),
                                 (None, None))

    def test_snapshot_com_contrato_incompativel_retorna_none(self):
        for prov in (_provenance(calib.PROB_STATUS_FORMULA_MISMATCH),
                     _provenance(calib.PROB_STATUS_SCORE_OUT_OF_RANGE),
                     _provenance(calibration_formula=LEGACY),
                     {"status": "READY"}):
            with self.subTest(prov=prov.get("status")):
                contrato = {"schema_version": 1, "probability_provenance": prov,
                            "prob_tp1": 0.55, "prob_tp2": 0.25}
                self.assertEqual(calib.probabilities_from_contract(contrato),
                                 (None, None))

    def test_zero_legitimo_permanece_zero(self):
        contrato = self._features(prob_tp1=0.0, prob_tp2=0.0)["probability_contract"]
        p1, p2 = calib.probabilities_from_contract(contrato)
        self.assertEqual((p1, p2), (0.0, 0.0))
        self.assertIsNotNone(p1)
        self.assertIsNotNone(p2)

    def test_ausencia_nunca_vira_zero(self):
        p1, p2 = calib.probabilities_from_contract({"probability_provenance": None})
        self.assertIsNone(p1)
        self.assertIsNone(p2)
        self.assertNotEqual(p1, 0.0)

    def test_snapshot_service_nao_usa_mais_os_wrappers_sem_formula(self):
        fonte = (BACKEND / "services" / "snapshot_service.py").read_text()
        self.assertNotIn("prob_tp1_for_score_sync", fonte)
        self.assertNotIn("prob_tp2_for_score_sync", fonte)
        self.assertIn("probabilities_from_contract", fonte)
        # e o campo de decisão lê o contrato persistido
        bloco = fonte.split("def _decision_fields")[1][:600]
        self.assertIn("probability_contract", bloco)
        self.assertIn("probabilities_from_contract", bloco)

    def test_nenhum_caminho_operacional_usa_os_wrappers(self):
        """Varredura de TODO o backend fora de `tests/`: só a definição sobra."""
        achados = []
        for caminho in BACKEND.rglob("*.py"):
            rel = caminho.relative_to(BACKEND.parent).as_posix()
            if "/tests/" in rel or "/__pycache__/" in rel or "/.venv" in rel:
                continue
            texto = caminho.read_text(encoding="utf-8", errors="ignore")
            if ("prob_tp1_for_score_sync" in texto
                    or "prob_tp2_for_score_sync" in texto):
                achados.append(rel)
        self.assertEqual(sorted(achados),
                         ["backend/services/calibration_service.py"], str(achados))

    def test_leitura_nao_muta_o_registro(self):
        """Sem backfill, sem reescrita: ler o contrato é operação pura."""
        contrato = self._features()["probability_contract"]
        copia = json.loads(json.dumps(contrato, default=str))
        calib.probabilities_from_contract(contrato)
        self.assertEqual(json.loads(json.dumps(contrato, default=str)), copia)

    def test_r06b2_1_nao_introduziu_escrita_em_features(self):
        """O pacote só ACRESCENTA o namespace em snapshots novos."""
        novo = subprocess.run(
            ["git", "diff", "--unified=0", "8ae87567", "--",
             "backend/services/snapshot_service.py"],
            cwd=BACKEND.parent, capture_output=True, text=True)
        if novo.returncode != 0:
            self.skipTest("baseline 8ae87567 indisponível neste checkout")
        adicionadas = [l for l in novo.stdout.splitlines()
                       if l.startswith("+") and not l.startswith("+++")]
        self.assertTrue(adicionadas)
        for ln in adicionadas:
            for proibido in ("update(", "delete(", "session.execute",
                             ".values(", "await session"):
                self.assertNotIn(proibido, ln, ln)


# ════════════════════════════════════════════════════════════════════════════
#  5. EXECUÇÃO E SIZING
# ════════════════════════════════════════════════════════════════════════════
class ExecucaoESizing(unittest.TestCase):

    def test_contrato_invalido_bloqueia_antes_de_qualquer_ordem(self):
        fonte = _fonte_de_funcao("services/shadow_trade_service.py",
                                 "open_shadow_for_recs")
        gate = fonte.index("_calibration_contract_verdict(rec")
        self.assertLess(gate, fonte.index("await exchange_service.place_order"))
        self.assertLess(gate, fonte.index("place_maker_entry_then_protect"))
        self.assertIn("require_current_contract=True", fonte)

    def test_loop_oficial_exige_contrato_atual(self):
        """Comportamental: rec sem proveniência não passa pelo caminho oficial."""
        legado = _rec(None)
        legado.pop("score_provenance")
        self.assertTrue(sts._calibration_contract_verdict(legado)["ok"])
        self.assertFalse(sts._calibration_contract_verdict(
            legado, require_current_contract=True)["ok"])

    def test_falha_no_helper_bloqueia(self):
        with patch("services.calibration_service.calibration_contract_verdict",
                   side_effect=RuntimeError(SEGREDO)):
            v = sts._calibration_contract_verdict(_rec(_provenance()),
                                                  require_current_contract=True)
        self.assertFalse(v["ok"])
        self.assertEqual(v["blocked_by"], calib.CALIBRATION_CONTRACT_GATE)
        self.assertNotIn(SEGREDO, json.dumps(v))

    def test_ready_mantem_o_fluxo(self):
        v = sts.exec_verdict(_rec(_provenance()))
        self.assertNotEqual(v.get("blocked_by"), calib.CALIBRATION_CONTRACT_GATE)

    def test_unavailable_mantem_o_comportamento_documentado(self):
        prov = _provenance(calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        v = sts.exec_verdict(_rec(prov, p1=None, p2=None))
        self.assertNotEqual(v.get("blocked_by"), calib.CALIBRATION_CONTRACT_GATE)

    def test_ready_malformado_bloqueia_no_exec_verdict(self):
        v = sts.exec_verdict(_rec({"status": "READY"}))
        self.assertFalse(v["ok"])
        self.assertEqual(v["blocked_by"], calib.CALIBRATION_CONTRACT_GATE)

    def test_sizing_bloqueante_nao_usa_fallback_por_tier(self):
        tamanho, motivo = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=None, atr_pct=0.02,
            probability_contract_blocking=True)
        self.assertIsNone(tamanho)
        self.assertNotIn(f"p={rs._TIER_WR_FALLBACK['A']*100:.0f}%", motivo)
        self.assertIn("Calibração incompatível", motivo)

    def test_sizing_nao_bloqueante_preserva_o_r06b2(self):
        for prob in (0.7, None):
            with self.subTest(prob=prob):
                self.assertEqual(
                    rs._compute_dynamic_size(score=80.0, tier="A", risk_reward=2.0,
                                             prob_tp1=prob, atr_pct=0.02),
                    rs._compute_dynamic_size(score=80.0, tier="A", risk_reward=2.0,
                                             prob_tp1=prob, atr_pct=0.02,
                                             probability_contract_blocking=False))

    def test_sizing_nao_depende_mais_de_import_interno(self):
        """A lacuna: import falhando virava conjunto vazio ⇒ fallback por tier."""
        fonte = _codigo(_fonte_de_funcao("services/recommendation_service.py",
                                         "_compute_dynamic_size"))
        self.assertNotIn("BLOCKING_PROB_STATUSES", fonte)
        self.assertNotIn("import", fonte)          # nenhum import no corpo
        self.assertNotIn("except", fonte)          # nem except silencioso
        self.assertIn("probability_contract_blocking", fonte)

    def test_kelly_e_caps_inalterados(self):
        self.assertEqual(rs.KELLY_FRACTION, 0.25)
        self.assertEqual((rs.SIZE_MIN_PCT, rs.SIZE_MAX_PCT), (0.25, 1.0))
        self.assertEqual(rs._TIER_WR_FALLBACK, {"A+": 0.62, "A": 0.55, "B": 0.50})
        fonte = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertIn("kelly = (p * b - (1.0 - p)) / b", fonte)


# ════════════════════════════════════════════════════════════════════════════
#  6. ESCOPO
# ════════════════════════════════════════════════════════════════════════════
class Escopo(unittest.TestCase):

    #: sujos PREEXISTENTES ao pacote, preservados de propósito
    PREEXISTENTES = {"frontend/dist/index.html"}

    def _diff(self, *caminhos):
        res = subprocess.run(["git", "diff", "--name-only", "8ae87567", "--", *caminhos],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 8ae87567 indisponível neste checkout")
        return [ln for ln in res.stdout.splitlines()
                if ln.strip() and ln.strip() not in self.PREEXISTENTES]

    def test_estrategia_execucao_e_risco_intactos(self):
        self.assertEqual(self._diff(
            "backend/services/indicator_service.py",
            "backend/services/signal_service.py",
            "backend/services/confluence_service.py",
            "backend/services/entry_planner.py",
            "backend/services/kill_switch_service.py",
            "backend/services/risk_service.py",
            "backend/services/financial_risk_service.py",
            "backend/services/exchange_service.py",
            "backend/services/binance_signed_service.py",
            "backend/services/strategy_evidence_service.py",
            "backend/models",
            "backend/db.py",
            "backend/main.py",
            "frontend",
        ), [])

    def test_sem_coluna_migration_env_flag_ou_endpoint(self):
        for arquivo in ("calibration_service.py", "recommendation_service.py",
                        "shadow_trade_service.py", "snapshot_service.py"):
            novo = subprocess.run(
                ["git", "diff", "--unified=0", "8ae87567", "--",
                 f"backend/services/{arquivo}"],
                cwd=BACKEND.parent, capture_output=True, text=True).stdout
            for ln in [l for l in novo.splitlines()
                       if l.startswith("+") and not l.startswith("+++")]:
                for proibido in ("os.getenv", "ADD COLUMN", "@app.get", "@app.post",
                                 "mapped_column"):
                    self.assertNotIn(proibido, ln, f"{arquivo}: {ln}")

    def test_bins_numericos_pav_e_shrinkage_intactos(self):
        fonte = (BACKEND / "services" / "calibration_service.py").read_text()
        self.assertIn("SHRINKAGE_K = 10", fonte)
        self.assertIn("p_shr = (SHRINKAGE_K * p_global + n * p_obs) / (SHRINKAGE_K + n)",
                      fonte)
        self.assertEqual(calib.MIN_SAMPLE_TOTAL, 30)
        self.assertEqual(len(calib.SCORE_BINS_V2), 9)
        self.assertEqual(len(calib.SCORE_BINS_LEGACY), 9)


def _codigo(fonte: str) -> str:
    """Fonte sem comentários e sem docstrings — grep de CÓDIGO, não de prosa."""
    import ast, io, textwrap, tokenize

    fonte = textwrap.dedent(fonte)
    arvore = ast.parse(fonte)
    docstrings = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef,
                           ast.FunctionDef, ast.AsyncFunctionDef)):
            corpo = getattr(no, "body", None)
            if (corpo and isinstance(corpo[0], ast.Expr)
                    and isinstance(corpo[0].value, ast.Constant)
                    and isinstance(corpo[0].value.value, str)):
                docstrings.add((corpo[0].lineno, corpo[0].col_offset))
    saida = []
    for tok in tokenize.generate_tokens(io.StringIO(fonte).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            continue
        saida.append(tok.string)
    return "\n".join(saida)


def _fonte_de_funcao(caminho_relativo: str, nome: str) -> str:
    import ast
    texto = (BACKEND / caminho_relativo).read_text()
    arvore = ast.parse(texto)
    for no in arvore.body:
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)) and no.name == nome:
            return "\n".join(texto.splitlines()[no.lineno - 1:no.end_lineno])
    raise AssertionError(f"função {nome} não encontrada em {caminho_relativo}")


if __name__ == "__main__":
    unittest.main()
