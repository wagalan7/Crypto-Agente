"""R06B2 — cerca entre score, calibração e autoexecução.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, credencial, seed privado ou outcome de holdout. Nenhuma ordem real.

O defeito, comprovado no R06A e tornado observável no R06B1: `SCORE_V2` pedida
→ V2 indisponível → fallback `LEGACY_V1` → bins continuam V2 → o lookup
devolvia `p_global` e esse número seguia para os gates e para o sizing como se
fosse probabilidade calibrada daquele score.

Aqui os testes são de CORREÇÃO e de NÃO-REGRESSÃO. Fora de escopo: a semântica
financeira do Kelly (R06B3) e qualquer mudança de score, tier, stop, TP ou cap.
"""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
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
    raise RuntimeError(f"REDE BLOQUEADA no teste R06B2 (hermético): {a[:1]}")


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
LEGACY = calib.CALIBRATION_FORMULA_LEGACY


# ── Calibrações sintéticas (nenhum dado real, nenhuma amostra de produção) ──
def _bin(lo, hi, p1, p2):
    return {"score_lo": lo, "score_hi": hi, "label": f"[{lo}-{hi})",
            "n_total": 40, "n_wins": 20,
            "p_calibrated": p1, "p_tp2_calibrated": p2}


def _calibracao(formula=V2, *, bins=None, total=120, **extra):
    bins = bins if bins is not None else [
        _bin(15, 40, 0.30, 0.10), _bin(40, 60, 0.55, 0.25), _bin(60, 75, 0.80, 0.40),
    ]
    out = {
        "enabled": True,
        "source": "teste",
        "total_resolved": total,
        "contract_version": calib.CALIBRATION_CONTRACT_VERSION,
        "calibration_formula": formula,
        "bins_version": calib.bins_version(
            [(b["score_lo"], b["score_hi"]) for b in bins], formula),
        "score_range": [bins[0]["score_lo"], bins[-1]["score_hi"]],
        "pairs_formula_provenance": calib.PAIRS_PROVENANCE_UNVERSIONED,
        "p_global": 0.42,
        "p_tp2_global": 0.21,
        "bins": bins,
    }
    out.update(extra)
    return out


def _calibracao_dos_bins_reais(formula=None):
    """Calibração sintética cujos bins são exatamente os `SCORE_BINS` ativos."""
    formula = calib.active_calibration_formula() if formula is None else formula
    n = len(calib.SCORE_BINS)
    bins = [
        _bin(lo, hi, round(0.30 + 0.05 * i, 4), round(0.10 + 0.02 * i, 4))
        for i, (lo, hi) in enumerate(calib.SCORE_BINS)
    ]
    assert n == len(bins)
    return _calibracao(formula, bins=bins)


def _codigo(fonte: str) -> str:
    """Fonte sem comentários e sem docstrings — para grep de CÓDIGO real.

    Aprendizado do R06A/R06B1: `assertNotIn` cru casa com a própria prosa que
    escrevemos para explicar o contrato, e o teste falha pelo motivo errado.
    """
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


P_GLOBAL_SENTINELA = (0.42, 0.21)


# ════════════════════════════════════════════════════════════════════════════
#  A. CONTRATO DO LOOKUP
# ════════════════════════════════════════════════════════════════════════════
class ContratoDoLookup(unittest.TestCase):

    def test_v2_com_bins_v2_e_score_no_range_fica_ready(self):
        r = calib.probability_for_score(50.0, V2, _calibracao(V2))
        self.assertEqual(r.status, calib.PROB_STATUS_READY)
        self.assertEqual(r.reason_code, calib.PROB_REASON_OK)
        self.assertEqual((r.prob_tp1, r.prob_tp2), (0.55, 0.25))
        self.assertEqual(r.bin_index, 1)
        self.assertEqual(r.score_formula_effective, V2)
        self.assertEqual(r.calibration_formula, V2)
        self.assertFalse(r.fallback_used)
        self.assertTrue(r.ok)
        self.assertFalse(r.blocking)

    def test_legado_com_bins_legado_e_score_no_range_fica_ready(self):
        r = calib.probability_for_score(50.0, LEGACY, _calibracao(LEGACY))
        self.assertEqual(r.status, calib.PROB_STATUS_READY)
        self.assertEqual(r.prob_tp1, 0.55)

    def test_legado_com_bins_v2_e_mismatch(self):
        r = calib.probability_for_score(50.0, LEGACY, _calibracao(V2))
        self.assertEqual(r.status, calib.PROB_STATUS_FORMULA_MISMATCH)
        self.assertEqual(r.reason_code, calib.PROB_REASON_FORMULA_NOT_ACCEPTED)
        self.assertIsNone(r.prob_tp1)
        self.assertIsNone(r.prob_tp2)
        self.assertTrue(r.blocking)

    def test_v2_com_bins_legado_e_mismatch(self):
        r = calib.probability_for_score(50.0, V2, _calibracao(LEGACY))
        self.assertEqual(r.status, calib.PROB_STATUS_FORMULA_MISMATCH)
        self.assertIsNone(r.prob_tp1)

    def test_score_fora_do_range_com_formula_compativel(self):
        for fora in (14.9, 75.0, 999.0, -5.0):
            with self.subTest(score=fora):
                r = calib.probability_for_score(fora, V2, _calibracao(V2))
                self.assertEqual(r.status, calib.PROB_STATUS_SCORE_OUT_OF_RANGE)
                self.assertEqual(r.reason_code, calib.PROB_REASON_SCORE_OUTSIDE_BINS)
                self.assertIsNone(r.prob_tp1)
                self.assertIsNone(r.prob_tp2)
                self.assertIsNone(r.bin_index)

    def test_borda_superior_do_ultimo_bin_e_exclusiva(self):
        """Mesma semântica semiaberta [lo, hi) já usada pelos bins."""
        self.assertEqual(
            calib.probability_for_score(74.999, V2, _calibracao(V2)).status,
            calib.PROB_STATUS_READY)
        self.assertEqual(
            calib.probability_for_score(75.0, V2, _calibracao(V2)).status,
            calib.PROB_STATUS_SCORE_OUT_OF_RANGE)

    def test_cache_vazio_e_calibracao_indisponivel(self):
        for vazio in (None, {}, {"bins": []}, {"bins": None}):
            with self.subTest(calib=vazio):
                r = calib.probability_for_score(50.0, V2, vazio)
                self.assertEqual(r.status, calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
                self.assertIsNone(r.prob_tp1)
                self.assertFalse(r.blocking)

    def test_amostra_insuficiente_e_calibracao_indisponivel(self):
        r = calib.probability_for_score(
            50.0, V2, _calibracao(V2, total=calib.MIN_SAMPLE_TOTAL - 1))
        self.assertEqual(r.status, calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertEqual(r.reason_code, calib.PROB_REASON_SAMPLE_BELOW_MINIMUM)

    def test_indisponivel_nao_e_mismatch(self):
        """Distinção que o pacote inteiro depende: imaturo != incompatível."""
        indisponivel = calib.probability_for_score(50.0, LEGACY, None)
        self.assertEqual(indisponivel.status, calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertNotEqual(indisponivel.status, calib.PROB_STATUS_FORMULA_MISMATCH)
        self.assertNotIn(calib.PROB_STATUS_CALIBRATION_UNAVAILABLE,
                         calib.BLOCKING_PROB_STATUSES)

    def test_score_invalido(self):
        casos = {
            None: calib.PROB_REASON_SCORE_MISSING,
            True: calib.PROB_REASON_SCORE_NOT_NUMERIC,
            False: calib.PROB_REASON_SCORE_NOT_NUMERIC,
            "50": calib.PROB_REASON_SCORE_NOT_NUMERIC,
            float("nan"): calib.PROB_REASON_SCORE_NOT_FINITE,
            float("inf"): calib.PROB_REASON_SCORE_NOT_FINITE,
            float("-inf"): calib.PROB_REASON_SCORE_NOT_FINITE,
        }
        for valor, code in casos.items():
            with self.subTest(score=repr(valor)):
                r = calib.probability_for_score(valor, V2, _calibracao(V2))
                self.assertEqual(r.status, calib.PROB_STATUS_INVALID_SCORE)
                self.assertEqual(r.reason_code, code)
                self.assertIsNone(r.prob_tp1)
                self.assertTrue(r.blocking)

    def test_probabilidade_fora_de_zero_um_invalida_o_contrato(self):
        for p1, p2 in ((1.5, 0.2), (-0.1, 0.0), (float("nan"), 0.2),
                       (0.5, 1.7), (0.5, float("inf")), (0.5, None)):
            with self.subTest(p1=p1, p2=p2):
                c = _calibracao(V2, bins=[_bin(15, 75, p1, p2)])
                r = calib.probability_for_score(50.0, V2, c)
                self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
                self.assertEqual(r.reason_code, calib.PROB_REASON_PROB_OUT_OF_UNIT)
                self.assertIsNone(r.prob_tp1)

    def test_tp2_maior_que_tp1_invalida_o_contrato(self):
        c = _calibracao(V2, bins=[_bin(15, 75, 0.40, 0.90)])
        r = calib.probability_for_score(50.0, V2, c)
        self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
        self.assertEqual(r.reason_code, calib.PROB_REASON_TP2_ABOVE_TP1)
        self.assertIsNone(r.prob_tp2)

    def test_tp2_igual_a_tp1_e_valido(self):
        c = _calibracao(V2, bins=[_bin(15, 75, 0.40, 0.40)])
        r = calib.probability_for_score(50.0, V2, c)
        self.assertEqual(r.status, calib.PROB_STATUS_READY)
        self.assertEqual(r.prob_tp2, 0.40)

    def test_formula_da_calibracao_desconhecida(self):
        for ruim in (None, "", "V3", 7, {"x": 1}):
            with self.subTest(formula=repr(ruim)):
                r = calib.probability_for_score(50.0, V2, _calibracao(ruim))
                self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
                self.assertEqual(r.reason_code, calib.PROB_REASON_CALIB_FORMULA_UNKNOWN)

    def test_formula_do_score_desconhecida(self):
        for ruim in (None, "", "SCORE_V9", 3):
            with self.subTest(formula=repr(ruim)):
                r = calib.probability_for_score(50.0, ruim, _calibracao(V2))
                self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
                self.assertEqual(r.reason_code, calib.PROB_REASON_SCORE_FORMULA_UNKNOWN)

    def test_bins_version_ausente_invalida_o_contrato(self):
        r = calib.probability_for_score(50.0, V2, _calibracao(V2, bins_version=None))
        self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
        self.assertEqual(r.reason_code, calib.PROB_REASON_BINS_VERSION_MISSING)

    def test_bins_malformados(self):
        ruins = [
            [{"score_lo": "a", "score_hi": 60, "p_calibrated": 0.5, "p_tp2_calibrated": 0.2}],
            [{"score_lo": 60, "score_hi": 40, "p_calibrated": 0.5, "p_tp2_calibrated": 0.2}],
            [{"score_lo": 40, "score_hi": 40, "p_calibrated": 0.5, "p_tp2_calibrated": 0.2}],
            ["nao-e-dict"],
            [{"p_calibrated": 0.5}],
        ]
        for bins in ruins:
            with self.subTest(bins=bins):
                c = _calibracao(V2)
                c["bins"] = bins
                r = calib.probability_for_score(50.0, V2, c)
                self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
                self.assertEqual(r.reason_code, calib.PROB_REASON_BINS_MALFORMED)

    def test_bins_ambiguos(self):
        c = _calibracao(V2, bins=[_bin(15, 75, 0.5, 0.2), _bin(40, 60, 0.9, 0.4)])
        r = calib.probability_for_score(50.0, V2, c)
        self.assertEqual(r.status, calib.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
        self.assertEqual(r.reason_code, calib.PROB_REASON_BINS_AMBIGUOUS)

    def test_nenhum_estado_nao_ready_devolve_p_global(self):
        """A garantia central do pacote, varrida sobre TODOS os casos ruins."""
        casos = [
            (50.0, LEGACY, _calibracao(V2)),                       # mismatch
            (999.0, V2, _calibracao(V2)),                          # fora do range
            (float("nan"), V2, _calibracao(V2)),                   # score inválido
            (None, V2, _calibracao(V2)),                           # score ausente
            (50.0, "V9", _calibracao(V2)),                         # fórmula desconhecida
            (50.0, V2, _calibracao("V9")),                         # calib desconhecida
            (50.0, V2, _calibracao(V2, bins=[_bin(15, 75, 9.0, 0.2)])),
            (50.0, V2, _calibracao(V2, bins=[_bin(15, 75, 0.2, 0.9)])),
            (50.0, V2, None),                                      # indisponível
        ]
        for score, formula, c in casos:
            with self.subTest(score=score, formula=formula):
                r = calib.probability_for_score(score, formula, c)
                self.assertNotEqual(r.status, calib.PROB_STATUS_READY)
                self.assertIsNone(r.prob_tp1)
                self.assertIsNone(r.prob_tp2)
                self.assertNotIn(r.prob_tp1, P_GLOBAL_SENTINELA)
                self.assertNotIn(r.prob_tp2, P_GLOBAL_SENTINELA)

    def test_status_e_reason_pertencem_ao_vocabulario_fechado(self):
        casos = [
            (50.0, V2, _calibracao(V2)), (50.0, LEGACY, _calibracao(V2)),
            (999.0, V2, _calibracao(V2)), (float("inf"), V2, _calibracao(V2)),
            (50.0, V2, None), (50.0, V2, _calibracao("X")),
        ]
        for score, formula, c in casos:
            r = calib.probability_for_score(score, formula, c)
            self.assertIn(r.status, calib.PROB_STATUSES)
            self.assertIn(r.reason_code, calib.PROB_REASON_CODES)
            # nada de exceção crua, caminho de arquivo ou dado pessoal
            self.assertTrue(r.reason_code.replace("_", "").isalnum())

    def test_fallback_used_e_propagado_sem_alterar_o_veredito(self):
        base = calib.probability_for_score(50.0, LEGACY, _calibracao(V2))
        com_fb = calib.probability_for_score(50.0, LEGACY, _calibracao(V2),
                                             fallback_used=True)
        self.assertFalse(base.fallback_used)
        self.assertTrue(com_fb.fallback_used)
        self.assertEqual(base.status, com_fb.status)

    def test_funcao_e_pura(self):
        """Sem banco, rede, ENV ou exchange — a calibração chega por argumento."""
        fonte = _codigo(_fonte_de_funcao("services/calibration_service.py",
                                         "probability_for_score"))
        for proibido in ("await ", "get_session", "os.getenv", "select(",
                         "_cache", "exchange", "requests", "httpx"):
            self.assertNotIn(proibido, fonte, proibido)
        # e roda com a rede bloqueada (setUpModule) sem tocar em nada
        self.assertEqual(
            calib.probability_for_score(50.0, V2, _calibracao(V2)).status,
            calib.PROB_STATUS_READY)


# ════════════════════════════════════════════════════════════════════════════
#  B. IDENTIDADE DOS BINS
# ════════════════════════════════════════════════════════════════════════════
class IdentidadeDosBins(unittest.TestCase):

    def test_calibracao_declara_a_identidade(self):
        out = calib.compute_calibration_from_pairs(
            [(60.0, "won_tp1"), (58.0, "lost"), (70.0, "won_tp2")], source="teste")
        self.assertEqual(out["contract_version"], calib.CALIBRATION_CONTRACT_VERSION)
        self.assertIn(out["calibration_formula"], calib.KNOWN_FORMULAS)
        self.assertEqual(out["bins_version"], calib.bins_version())
        self.assertEqual(out["score_range"], calib.bins_score_range())
        self.assertIn("source", out)
        self.assertIn("total_resolved", out)

    def test_identidade_segue_a_mesma_flag_dos_bins(self):
        with patch.object(calib, "_SCORE_FORMULA_V2", True):
            self.assertEqual(calib.active_calibration_formula(), V2)
        with patch.object(calib, "_SCORE_FORMULA_V2", False):
            self.assertEqual(calib.active_calibration_formula(), LEGACY)

    def test_bins_version_e_estavel_e_distingue_configuracoes(self):
        self.assertEqual(calib.bins_version(), calib.bins_version())
        v2 = calib.bins_version(calib.SCORE_BINS_V2, V2)
        legado = calib.bins_version(calib.SCORE_BINS_LEGACY, LEGACY)
        self.assertNotEqual(v2, legado)
        self.assertIn(V2, v2)
        self.assertIn(LEGACY, legado)
        # muda quando a configuração muda
        self.assertNotEqual(v2, calib.bins_version(calib.SCORE_BINS_V2[:-1], V2))

    def test_ids_de_formula_batem_com_o_score_provenance(self):
        """Os dois módulos não se importam; os literais têm que coincidir."""
        self.assertEqual(calib.CALIBRATION_FORMULA_V2, rs.SCORE_FORMULA_V2_ID)
        self.assertEqual(calib.CALIBRATION_FORMULA_LEGACY, rs.SCORE_FORMULA_LEGACY_ID)
        self.assertEqual(calib.KNOWN_FORMULAS,
                         frozenset({rs.SCORE_FORMULA_V2_ID, rs.SCORE_FORMULA_LEGACY_ID}))

    def test_proveniencia_da_amostra_e_honesta(self):
        """A identidade diz qual fórmula os BINS aceitam — não afirma que cada
        par histórico tenha proveniência individual conhecida."""
        out = calib.compute_calibration_from_pairs([(60.0, "lost")], source="teste")
        self.assertEqual(out["pairs_formula_provenance"],
                         calib.PAIRS_PROVENANCE_UNVERSIONED)
        fonte = (BACKEND / "services" / "calibration_service.py").read_text()
        self.assertIn("não é uma afirmação\n# retroativa", fonte)
        # e a fórmula individual NÃO é inferida do valor do score
        self.assertNotIn("infer_formula", fonte)
        self.assertNotIn("guess_formula", fonte)


# ════════════════════════════════════════════════════════════════════════════
#  C. REMOÇÃO DO FALLBACK PARA p_global
# ════════════════════════════════════════════════════════════════════════════
class RemocaoDoFallbackGlobal(unittest.TestCase):

    def setUp(self):
        # Os lookups LEGADOS indexam pelo `SCORE_BINS` do módulo (não pelos bins
        # do dict), então a calibração sintética tem que espelhar os bins reais.
        self._anterior = calib._cache.get("data")
        self.calib = _calibracao_dos_bins_reais()
        self.dentro = (calib.SCORE_BINS[1][0] + calib.SCORE_BINS[1][1]) / 2.0
        calib._cache["data"] = self.calib

    def tearDown(self):
        calib._cache["data"] = self._anterior

    def test_sync_fora_do_range_devolve_ausencia(self):
        self.assertIsNone(calib.prob_tp1_for_score_sync(999.0))
        self.assertIsNone(calib.prob_tp2_for_score_sync(999.0))
        self.assertIsNone(calib.prob_tp1_for_score_sync(-999.0))

    def test_sync_dentro_do_range_continua_funcionando(self):
        self.assertEqual(calib.prob_tp1_for_score_sync(self.dentro),
                         self.calib["bins"][1]["p_calibrated"])
        self.assertEqual(calib.prob_tp2_for_score_sync(self.dentro),
                         self.calib["bins"][1]["p_tp2_calibrated"])

    def test_async_fora_do_range_devolve_ausencia(self):
        alvo = self.calib

        async def _fake():
            return alvo
        with patch.object(calib, "get_calibration", _fake):
            self.assertIsNone(asyncio.run(calib.prob_tp1_for_score(999.0)))
            self.assertEqual(asyncio.run(calib.prob_tp1_for_score(self.dentro)),
                             alvo["bins"][1]["p_calibrated"])

    def test_nenhum_dos_tres_lookups_retorna_p_global(self):
        for nome in ("prob_tp1_for_score", "prob_tp1_for_score_sync",
                     "prob_tp2_for_score_sync"):
            fonte = _fonte_de_funcao("services/calibration_service.py", nome)
            self.assertNotIn("p_global", fonte, nome)
            self.assertNotIn("p_tp2_global", fonte, nome)

    def test_p_global_continua_como_estatistica_agregada(self):
        out = calib.compute_calibration_from_pairs(
            [(60.0, "won_tp1"), (58.0, "lost")], source="teste")
        self.assertIn("p_global", out)
        self.assertIn("p_tp2_global", out)
        self.assertIsInstance(out["p_global"], float)
        # e segue publicado no preview de A/B
        fonte = (BACKEND / "services" / "calibration_service.py").read_text()
        self.assertIn('"p_global_live"', fonte)
        self.assertIn('"p_global_blended"', fonte)


# ════════════════════════════════════════════════════════════════════════════
#  D. INTEGRAÇÃO NA RECOMENDAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class IntegracaoNaRecomendacao(unittest.TestCase):

    def setUp(self):
        self._anterior = calib._cache.get("data")

    def tearDown(self):
        calib._cache["data"] = self._anterior

    def test_campo_existe_e_nao_e_coluna(self):
        campo = rs.Recommendation.model_fields.get("probability_provenance")
        self.assertIsNotNone(campo)
        self.assertIsNone(campo.default)

    def test_payload_tem_exatamente_as_chaves_do_contrato(self):
        r = calib.probability_for_score(50.0, V2, _calibracao(V2))
        d = r.as_provenance()
        self.assertEqual(sorted(d), [
            "bin_index", "bins_version", "calibration_formula", "contract_version",
            "fallback_used", "reason_code", "score_formula_effective", "status",
        ])
        self.assertEqual(json.loads(json.dumps(d)), d)   # serializável

    def test_proveniencia_do_score_e_calculada_antes_do_lookup(self):
        fonte = _fonte_de_funcao("services/recommendation_service.py",
                                 "_build_recommendation")
        self.assertLess(fonte.index("compute_score_with_provenance(sig)"),
                        fonte.index("probability_for_score("))
        # e é a fórmula EFETIVA que governa o lookup, não a flag global
        trecho = fonte.split("probability_for_score(")[1][:300]
        self.assertIn('"formula_effective"', trecho)
        self.assertNotIn("SCORE_FORMULA_V2", trecho)

    def test_ready_preenche_probabilidade_e_proveniencia(self):
        calib._cache["data"] = _calibracao(V2)
        rec = self._recomendacao(score=50.0, formula=V2)
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_READY)
        self.assertEqual(rec.prob_tp1, 0.55)
        self.assertEqual(rec.prob_tp2, 0.25)

    def test_mismatch_zera_probabilidade_mas_mantem_a_rec(self):
        calib._cache["data"] = _calibracao(V2)
        rec = self._recomendacao(score=50.0, formula=LEGACY)
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_FORMULA_MISMATCH)
        self.assertIsNone(rec.prob_tp1)
        self.assertIsNone(rec.prob_tp2)
        self.assertNotEqual(rec.prob_tp1, 0.0)   # ausência não é zero

    def test_calibracao_indisponivel_preserva_o_comportamento_anterior(self):
        calib._cache["data"] = None
        rec = self._recomendacao(score=50.0, formula=V2)
        self.assertEqual(rec.probability_provenance["status"],
                         calib.PROB_STATUS_CALIBRATION_UNAVAILABLE)
        self.assertIsNone(rec.prob_tp1)
        # sizing segue usando o fallback por tier (calibração imatura)
        self.assertIsNotNone(rec.suggested_size_pct)

    def test_bot_verdict_recebe_os_dois_contratos(self):
        fonte = _fonte_de_funcao("services/recommendation_service.py",
                                 "_build_recommendation")
        self.assertIn('"probability_provenance": prob_prov', fonte)
        self.assertIn('"score_provenance": score_prov', fonte)

    def test_payload_antigo_sem_proveniencia_continua_legivel(self):
        antigo = rs.Recommendation.model_validate({
            "tier": "A", "score": 70.0, "symbol": "TESTUSDT", "timeframe": "4h",
            "direction": "long", "confidence": 0.6, "risk_reward": 2.0,
            "entry": 100.0, "stop_loss": 98.0, "tp2": 104.0, "summary": "x",
            "signal": _sinal(Indicator()).model_dump(), "prob_tp1": 0.6,
        })
        self.assertIsNone(antigo.probability_provenance)
        self.assertIsNone(antigo.score_provenance)
        self.assertEqual(antigo.prob_tp1, 0.6)

    # ── helper ──
    def _recomendacao(self, *, score, formula):
        sig = _sinal(Indicator(atr=1.0, atr_pct=0.02, adx=30.0), conf_pct=70.0)
        prov = {"score": score, "formula_requested": formula,
                "formula_effective": formula, "fallback_used": False,
                "fallback_reason": None}
        with patch.object(rs, "compute_score_with_provenance",
                          return_value=rs.ScoreProvenance(
                              score=score, formula_requested=formula,
                              formula_effective=formula, fallback_used=False,
                              fallback_reason=None)):
            rec = rs._build_recommendation(sig, score, "A")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.score_provenance["formula_effective"], prov["formula_effective"])
        return rec


# ════════════════════════════════════════════════════════════════════════════
#  E. BLOQUEIO DA AUTOEXECUÇÃO
# ════════════════════════════════════════════════════════════════════════════
class BloqueioDaAutoexecucao(unittest.TestCase):

    def _rec(self, status=None, *, fallback_used=False, reason_code=None, **extra):
        """Rec com contrato BEM FORMADO por padrão (o R06B2.1 exige envelope
        íntegro e coerência entre status, bin e probabilidade)."""
        rec = {"symbol": "TESTUSDT", "entry": 100.0, "stop_loss": 98.0,
               "tp1": 104.0, "tp2": 108.0, "score": 70.0, "edge_score": 2,
               "score_provenance": {"formula_effective": V2,
                                    "fallback_used": fallback_used}}
        if status is not None:
            pronto = status == calib.PROB_STATUS_READY
            if reason_code is None:
                reason_code = (calib.PROB_REASON_OK if pronto
                               else calib.PROB_REASON_NO_CALIBRATION)
            rec["probability_provenance"] = {
                "contract_version": calib.CALIBRATION_CONTRACT_VERSION,
                "status": status, "reason_code": reason_code,
                "score_formula_effective": V2, "calibration_formula": V2,
                "bins_version": calib.bins_version(),
                "bin_index": 1 if pronto else None,
                "fallback_used": fallback_used,
            }
            if pronto:
                rec["prob_tp1"], rec["prob_tp2"] = 0.55, 0.25
        rec.update(extra)
        return rec

    def test_estados_bloqueantes(self):
        for status in sorted(calib.BLOCKING_PROB_STATUSES):
            with self.subTest(status=status):
                v = calib.calibration_contract_verdict(self._rec(status))
                self.assertFalse(v["ok"])
                self.assertEqual(v["blocked_by"], calib.CALIBRATION_CONTRACT_GATE)
                self.assertEqual(v["status"], status)
                self.assertTrue(v["reason"])

    def test_estados_nao_bloqueantes(self):
        for status in (calib.PROB_STATUS_READY,
                       calib.PROB_STATUS_CALIBRATION_UNAVAILABLE):
            with self.subTest(status=status):
                v = calib.calibration_contract_verdict(self._rec(status))
                self.assertTrue(v["ok"])
                self.assertIsNone(v["blocked_by"])

    def test_fallback_sem_proveniencia_bloqueia(self):
        v = calib.calibration_contract_verdict(self._rec(None, fallback_used=True))
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason_code"], calib.PROB_REASON_PROVENANCE_MISSING)

    def test_fallback_com_proveniencia_malformada_bloqueia(self):
        for ruim in ({"status": "QUALQUER_COISA"}, {"status": None}, {},
                     "nao-e-dict", 7):
            with self.subTest(prov=repr(ruim)):
                rec = self._rec(None, fallback_used=True)
                rec["probability_provenance"] = ruim
                v = calib.calibration_contract_verdict(rec)
                self.assertFalse(v["ok"])

    def test_payload_antigo_sem_fallback_nao_bloqueia(self):
        """Compatibilidade: rec pré-R06B2 preserva o comportamento anterior."""
        rec = self._rec(None, fallback_used=False)
        self.assertTrue(calib.calibration_contract_verdict(rec)["ok"])
        rec.pop("score_provenance")
        self.assertTrue(calib.calibration_contract_verdict(rec)["ok"])

    def test_aceita_dict_e_objeto(self):
        from types import SimpleNamespace
        d = self._rec(calib.PROB_STATUS_FORMULA_MISMATCH)
        obj = SimpleNamespace(**d)
        self.assertEqual(calib.calibration_contract_verdict(d)["ok"],
                         calib.calibration_contract_verdict(obj)["ok"])
        self.assertFalse(calib.calibration_contract_verdict(obj)["ok"])

    def test_exec_verdict_bloqueia_antes_dos_demais_gates(self):
        """Mesmo com geometria ruim, o motivo reportado é o contrato."""
        rec = self._rec(calib.PROB_STATUS_FORMULA_MISMATCH,
                        tp1=100.05, tp2=100.1)   # R:R péssimo de propósito
        v = sts.exec_verdict(rec)
        self.assertFalse(v["ok"])
        self.assertEqual(v["blocked_by"], calib.CALIBRATION_CONTRACT_GATE)
        self.assertEqual(v["checks"]["calibration_contract"],
                         calib.PROB_STATUS_FORMULA_MISMATCH)

    def test_exec_verdict_deixa_passar_ready_e_indisponivel(self):
        for status in (calib.PROB_STATUS_READY,
                       calib.PROB_STATUS_CALIBRATION_UNAVAILABLE):
            with self.subTest(status=status):
                v = sts.exec_verdict(self._rec(status))
                self.assertNotEqual(v.get("blocked_by"),
                                    calib.CALIBRATION_CONTRACT_GATE)

    def test_regra_definida_uma_unica_vez(self):
        """O executor, o exec_verdict e o app usam o MESMO helper."""
        calib_src = (BACKEND / "services" / "calibration_service.py").read_text()
        shadow_src = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        self.assertEqual(calib_src.count("def calibration_contract_verdict("), 1)
        self.assertEqual(shadow_src.count("def calibration_contract_verdict("), 0)
        self.assertIn("from services.calibration_service import calibration_contract_verdict",
                      shadow_src)
        # ponte única no shadow, usada pelos dois caminhos
        self.assertEqual(shadow_src.count("def _calibration_contract_verdict("), 1)
        self.assertEqual(shadow_src.count("= _calibration_contract_verdict(rec"), 2)

    def test_bloqueio_ocorre_antes_de_qualquer_ordem(self):
        fonte = _fonte_de_funcao("services/shadow_trade_service.py",
                                 "open_shadow_for_recs")
        gate = fonte.index("_calibration_contract_verdict(rec")
        self.assertLess(gate, fonte.index("await exchange_service.place_order"))
        self.assertLess(gate, fonte.index("place_maker_entry_then_protect"))
        self.assertLess(gate, fonte.index("[rr-gate]"))
        self.assertIn('_record_skip(rec, "calibration-contract"', fonte)

    def test_ponte_do_shadow_e_fail_closed(self):
        fonte = _fonte_de_funcao("services/shadow_trade_service.py",
                                 "_calibration_contract_verdict")
        self.assertEqual(fonte.count('"ok": False'), 2)   # import e execução
        self.assertNotIn("Traceback", fonte)
        with patch.object(sts, "_calibration_contract_verdict") as _:
            pass
        with patch("services.calibration_service.calibration_contract_verdict",
                   side_effect=RuntimeError("boom")):
            v = sts._calibration_contract_verdict({"symbol": "X"})
        self.assertFalse(v["ok"])
        self.assertEqual(v["blocked_by"], calib.CALIBRATION_CONTRACT_GATE)
        self.assertNotIn("boom", json.dumps(v))   # sem stack trace / detalhe cru

    def test_skip_e_explicavel_e_sem_segredo(self):
        fonte = _fonte_de_funcao("services/shadow_trade_service.py",
                                 "open_shadow_for_recs")
        trecho = fonte.split("_calibration_contract_verdict(rec")[1][:600]
        self.assertIn("_record_skip", trecho)
        self.assertIn("continue", trecho)
        for proibido in ("API_KEY", "SECRET", "token", "Traceback"):
            self.assertNotIn(proibido, trecho, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  F. SIZING
# ════════════════════════════════════════════════════════════════════════════
class Sizing(unittest.TestCase):

    def test_mismatch_nao_usa_fallback_por_tier_e_nao_dimensiona(self):
        for status in sorted(calib.BLOCKING_PROB_STATUSES):
            with self.subTest(status=status):
                tamanho, motivo = rs._compute_dynamic_size(
                    score=80.0, tier="A", risk_reward=2.0, prob_tp1=None,
                    atr_pct=0.02, probability_contract_blocking=True)
                self.assertIsNone(tamanho)
                self.assertIn("Calibração incompatível", motivo)
                self.assertNotIn("Kelly", motivo)
                self.assertNotIn("p=", motivo)

    def test_mismatch_ignora_ate_uma_prob_que_tenha_vazado(self):
        """Defesa em profundidade: nem uma prob presente reabre o sizing."""
        tamanho, motivo = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=0.9, atr_pct=0.02,
            probability_contract_blocking=True)
        self.assertIsNone(tamanho)
        self.assertIn("Calibração incompatível", motivo)

    def test_ready_preserva_o_resultado_anterior(self):
        antes = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=0.7, atr_pct=0.02)
        depois = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=0.7, atr_pct=0.02,
            probability_contract_blocking=False)
        self.assertEqual(antes, depois)

    def test_calibracao_indisponivel_preserva_o_fallback_por_tier(self):
        antes = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=None, atr_pct=0.02)
        depois = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=None, atr_pct=0.02,
            probability_contract_blocking=False)
        self.assertEqual(antes, depois)
        self.assertIsNotNone(depois[0])
        self.assertIn(f"p={rs._TIER_WR_FALLBACK['A']*100:.0f}%", depois[1])

    def test_conviction_nao_contorna_o_bloqueio(self):
        """Sem prob, o multiplicador por convicção é NO-OP (1.0); e a entrada
        já foi bloqueada pelo helper comum antes de chegar aqui."""
        rec = {"prob_tp1": None, "prob_tp2": None}
        mult, _ = sts._conviction_mult(rec)
        self.assertEqual(mult, 1.0)

    def test_caps_e_constantes_inalterados(self):
        self.assertEqual(rs.KELLY_FRACTION, 0.25)
        self.assertEqual(rs.ATR_REFERENCE_PCT, 0.02)
        self.assertEqual((rs.SIZE_MIN_PCT, rs.SIZE_MAX_PCT), (0.25, 1.0))
        self.assertEqual((rs.ATR_MULT_FLOOR, rs.ATR_MULT_CEIL), (0.5, 2.0))
        self.assertEqual(rs._TIER_WR_FALLBACK,
                         {"A+": 0.62, "A": 0.55, "B": 0.50})
        fonte = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertIn("kelly = (p * b - (1.0 - p)) / b", fonte)


# ════════════════════════════════════════════════════════════════════════════
#  G. REGRESSÃO E ESCOPO
# ════════════════════════════════════════════════════════════════════════════
class RegressaoEEscopo(unittest.TestCase):

    def _diff(self, *caminhos):
        """Escopo do R06B2 = o RANGE de commits do R06B2 (14102771..8ae87567).
        Comparar com a árvore de trabalho faria uma fase posterior autorizada
        (R06B2.1) quebrar esta garantia retroativamente."""
        res = subprocess.run(["git", "diff", "--name-only", "14102771", "8ae87567",
                              "--", *caminhos],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 14102771 indisponível neste checkout")
        return [ln for ln in res.stdout.splitlines() if ln.strip()]

    def test_score_e_tier_nao_mudaram(self):
        sig = _sinal(Indicator(adx=30.0), conf_pct=80.0, risk_reward=3.0,
                     mtf={"alignment_score": 0.8}, timeframe="4h")
        esperado = round(max(0.0, min(100.0, (
            80.0 * 0.35 + ((0.8 + 1) * 50) * 0.25
            + min(3.0 / 3.0, 1.0) * 100 * 0.25 + 50.0 * 0.10))), 1)
        self.assertEqual(rs._compute_score_legacy(sig), esperado)
        cortes = ((rs.SCORE_V2_TIER_APLUS, rs.SCORE_V2_TIER_A, rs.SCORE_V2_TIER_B)
                  if rs.SCORE_FORMULA_V2 else (75.0, 65.0, 52.0))
        self.assertEqual(rs._classify_tier(sig, cortes[1]), "A")
        self.assertEqual(rs._classify_tier(sig, cortes[2]), "B")

    def test_emas_12_26_permanecem_e_9_21_nao_existem_no_runtime(self):
        fonte = (BACKEND / "services" / "indicator_service.py").read_text()
        self.assertIn("EMAIndicator(close, window=12)", fonte)
        self.assertIn("EMAIndicator(close, window=26)", fonte)
        self.assertNotIn("EMAIndicator(close, window=9)", fonte)
        self.assertNotIn("EMAIndicator(close, window=21)", fonte)

    def test_p05_e_holdout_intactos(self):
        self.assertEqual(self._diff("backend/services/strategy_evidence_service.py"), [])
        self.assertEqual(self._diff("backend/services/snapshot_service.py"), [])

    def test_execucao_risco_e_contabilidade_intactos(self):
        self.assertEqual(self._diff(
            "backend/services/kill_switch_service.py",
            "backend/services/risk_service.py",
            "backend/services/financial_risk_service.py",
            "backend/services/execution_accounting_service.py",
            "backend/services/binance_signed_service.py",
            "backend/services/exchange_service.py",
            "backend/services/trade_manager_service.py",
            "backend/services/entry_planner.py",
            "backend/services/signal_service.py",
        ), [])

    def test_sem_coluna_migration_env_flag_ou_endpoint(self):
        res = subprocess.run(
            ["git", "diff", "--unified=0", "14102771", "8ae87567", "--",
             "backend/db.py", "backend/models", "backend/main.py"],
            cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 14102771 indisponível neste checkout")
        self.assertEqual(res.stdout.strip(), "")
        # e nenhuma ENV nova nos arquivos tocados
        for arquivo in ("calibration_service.py", "recommendation_service.py",
                        "shadow_trade_service.py"):
            novo = subprocess.run(
                ["git", "diff", "--unified=0", "14102771", "8ae87567", "--",
                 f"backend/services/{arquivo}"],
                cwd=BACKEND.parent, capture_output=True, text=True).stdout
            adicionadas = [ln for ln in novo.splitlines()
                           if ln.startswith("+") and not ln.startswith("+++")]
            for ln in adicionadas:
                self.assertNotIn("os.getenv", ln, f"{arquivo}: {ln}")
                self.assertNotIn("ADD COLUMN", ln, f"{arquivo}: {ln}")

    def test_dist_do_frontend_nao_regerado(self):
        alterados = [ln for ln in self._diff("frontend/dist")
                     if not ln.endswith("index.html")]
        self.assertEqual(alterados, [], str(alterados))


# ════════════════════════════════════════════════════════════════════════════
#  H. FRONTEND
# ════════════════════════════════════════════════════════════════════════════
class Frontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        base = BACKEND.parent / "frontend" / "src"
        cls.painel = (base / "components" / "RecommendationsPanel.tsx").read_text()
        cls.tipos = (base / "types" / "index.ts").read_text()

    def test_tipo_declara_o_contrato_como_opcional(self):
        self.assertIn("probability_provenance?:", self.tipos)
        self.assertIn("status?: string | null", self.tipos)

    def test_incompativel_mostra_texto_e_nao_percentual(self):
        bloco = self.painel.split("probability_provenance?.status")[1][:1400]
        self.assertIn("Calibração incompatível com a fórmula deste score.", bloco)
        self.assertNotIn("toFixed", bloco)

    def test_indisponivel_tem_texto_proprio(self):
        self.assertIn("Calibração ainda indisponível", self.painel)
        self.assertIn("CALIBRATION_UNAVAILABLE", self.painel)

    def test_nao_expoe_codigo_interno_nem_objeto(self):
        bloco = self.painel.split("probability_provenance?.status")[1][:1400]
        for proibido in ("reason_code", "bins_version", "[object Object]",
                         "{st}", "JSON.stringify"):
            self.assertNotIn(proibido, bloco, proibido)

    def test_payload_antigo_nao_muda_nada(self):
        bloco = self.painel.split("probability_provenance?.status")[1][:400]
        self.assertIn("if (st == null) return null", bloco)

    def test_nao_bloqueia_botao_no_frontend(self):
        """A decisão oficial continua no backend."""
        bloco = self.painel.split("probability_provenance?.status")[1][:1400]
        self.assertNotIn("disabled", bloco)
        self.assertNotIn("onAddToManager", bloco)


# ── Utilidades ──────────────────────────────────────────────────────────────
def _fonte_de_funcao(caminho_relativo: str, nome: str) -> str:
    """Corpo textual de uma função/def de topo — para asserções de ORDEM."""
    import ast
    texto = (BACKEND / caminho_relativo).read_text()
    arvore = ast.parse(texto)
    for no in arvore.body:
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)) and no.name == nome:
            linhas = texto.splitlines()[no.lineno - 1:no.end_lineno]
            return "\n".join(linhas)
    raise AssertionError(f"função {nome} não encontrada em {caminho_relativo}")


def _sinal(ind: Indicator, *, timeframe="4h", conf_pct=70.0, risk_reward=2.0,
           mtf=None, derivatives=None, direction=SignalDirection.LONG,
           confidence=0.6) -> TradeSignal:
    confluence = None
    if conf_pct is not None:
        confluence = ConfluenceScore(total=conf_pct, max_total=100.0,
                                     pct=conf_pct, factors=[])
    return TradeSignal(
        symbol="TESTUSDT", timeframe=timeframe, direction=direction,
        trade_type=TradeType.DAY_TRADE, confidence=confidence,
        entry=100.0, stop_loss=98.0, tp1=102.0, tp2=104.0, tp3=106.0,
        risk_reward=risk_reward, patterns=[], indicators=ind,
        confluence=confluence, mtf=mtf, derivatives=derivatives,
        timestamp=0, signal_strength="moderate",
    )


if __name__ == "__main__":
    unittest.main()
