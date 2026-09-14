"""R08A — auditoria do score e laboratório local do Score V3.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco,
credencial, holdout, escrita ou ordem. Testes COMPORTAMENTAIS: a paridade é
verificada contra a implementação REAL de `_compute_score_v2`, e os achados de
auditoria são reproduzidos com exemplos determinísticos.

O laboratório é `LOCAL_RESEARCH_ONLY`: nenhum caminho de produção o importa e
nenhum candidato foi ativado.
"""
from __future__ import annotations

import json
import math
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
    raise RuntimeError(f"REDE BLOQUEADA no teste R08A (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import score_research_service as lab                 # noqa: E402
from services import recommendation_service as rs                  # noqa: E402
from services import strategy_evidence_service as p05              # noqa: E402

W = {"conf": 0.60, "adx": 0.30, "der": 0.10}
LAB_PATH = "backend/services/score_research_service.py"


def _codigo(fonte: str) -> str:
    """Fonte sem comentários e sem docstrings — grep de CÓDIGO, não de prosa."""
    import ast, io, tokenize

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

#: matriz de entradas VÁLIDAS (dentro dos domínios documentados)
MATRIZ = [
    (70.0, 30.0, 0.01), (70.0, None, None), (None, 30.0, None),
    (None, None, 0.0), (0.0, 0.0, 0.0), (100.0, 100.0, 5.0),
    (55.5, 12.3, -0.2), (49.9, 50.0, 0.05), (100.0, 0.0, -5.0),
    (0.0, None, 0.0), (33.3, 49.99, -0.0499), (12.5, 17.5, 0.0),
]


def _v2_real(conf, adx, fund, pesos=W):
    """`_compute_score_v2` REAL sob pesos explícitos, restaurados no fim."""
    with patch.object(rs, "SCORE_V2_W_CONF", pesos["conf"]), \
         patch.object(rs, "SCORE_V2_W_ADX", pesos["adx"]), \
         patch.object(rs, "SCORE_V2_W_DER", pesos["der"]):
        return rs._compute_score_v2(conf, adx, fund)


# ════════════════════════════════════════════════════════════════════════════
#  A. PARIDADE COM A V2 REAL
# ════════════════════════════════════════════════════════════════════════════
class ParidadeV2(unittest.TestCase):

    def test_matriz_de_entradas_validas(self):
        for conf, adx, fund in MATRIZ:
            with self.subTest(conf=conf, adx=adx, fund=fund):
                real = _v2_real(conf, adx, fund)
                out = lab.score_v2_raw(confluence_pct=conf, adx=adx,
                                       funding_pct=fund, weights=W)
                self.assertEqual(out["status"], lab.STATUS_OK)
                self.assertEqual(out["score"], real)

    def test_ausencia_de_cada_componente_e_combinacoes(self):
        base = (70.0, 30.0, 0.01)
        for mascara in range(8):
            entradas = [v if (mascara >> i) & 1 else None
                        for i, v in enumerate(base)]
            with self.subTest(entradas=tuple(entradas)):
                real = _v2_real(*entradas)
                out = lab.score_v2_raw(confluence_pct=entradas[0], adx=entradas[1],
                                       funding_pct=entradas[2], weights=W)
                if real is None:
                    self.assertEqual(out["status"], lab.STATUS_UNAVAILABLE)
                    self.assertIsNone(out["score"])
                else:
                    self.assertEqual(out["score"], real)

    def test_ausencia_total_nao_fabrica_score_neutro(self):
        out = lab.score_v2_raw(confluence_pct=None, adx=None, funding_pct=None,
                               weights=W)
        self.assertIsNone(_v2_real(None, None, None))
        self.assertEqual(out["status"], lab.STATUS_UNAVAILABLE)
        self.assertEqual(out["reason_code"], lab.REASON_NO_COMPONENT)
        self.assertIsNone(out["score"])
        self.assertNotEqual(out["score"], 0.0)
        self.assertEqual(out["missing_components"], ["adx", "conf", "der"])

    def test_pesos_explicitos_diferentes(self):
        alternativos = [
            {"conf": 1.0, "adx": 0.0, "der": 0.0},
            {"conf": 0.0, "adx": 1.0, "der": 0.0},
            {"conf": 0.2, "adx": 0.2, "der": 0.6},
            {"conf": 3.0, "adx": 1.0, "der": 1.0},
        ]
        for pesos in alternativos:
            for conf, adx, fund in MATRIZ[:6]:
                with self.subTest(pesos=pesos, conf=conf):
                    real = _v2_real(conf, adx, fund, pesos)
                    out = lab.score_v2_raw(confluence_pct=conf, adx=adx,
                                           funding_pct=fund, weights=pesos)
                    if real is None:
                        self.assertEqual(out["status"], lab.STATUS_UNAVAILABLE)
                    else:
                        self.assertEqual(out["score"], real)

    def test_peso_zero_remove_o_componente_do_denominador(self):
        pesos = {"conf": 1.0, "adx": 0.0, "der": 0.0}
        out = lab.score_v2_raw(confluence_pct=70.0, adx=10.0, funding_pct=0.04,
                               weights=pesos)
        self.assertEqual(out["score"], 70.0)
        self.assertEqual(out["score"], _v2_real(70.0, 10.0, 0.04, pesos))
        self.assertEqual(out["effective_weights"]["adx"], 0.0)
        self.assertEqual(out["contributions"]["der"], 0.0)
        # peso zero em TODOS os presentes ⇒ indisponível, não zero
        zerado = lab.score_v2_raw(confluence_pct=70.0, adx=None, funding_pct=None,
                                  weights={"conf": 0.0, "adx": 1.0, "der": 1.0})
        self.assertEqual(zerado["status"], lab.STATUS_UNAVAILABLE)

    def test_bordas_de_clamp_e_arredondamento(self):
        # ADX satura em 50; funding satura em |0.05|
        for adx in (50.0, 75.0, 100.0):
            self.assertEqual(
                lab.score_v2_raw(confluence_pct=70.0, adx=adx, funding_pct=None,
                                 weights=W)["score"],
                _v2_real(70.0, adx, None))
        for fund in (0.05, 0.5, 5.0, -0.05, -0.5, -5.0):
            self.assertEqual(
                lab.score_v2_raw(confluence_pct=70.0, adx=None, funding_pct=fund,
                                 weights=W)["score"],
                _v2_real(70.0, None, fund))
        # arredondamento em 1 casa, igual ao real
        for conf in (70.04, 70.05, 70.06, 0.04, 99.96):
            self.assertEqual(
                lab.score_v2_raw(confluence_pct=conf, adx=None, funding_pct=None,
                                 weights=W)["score"],
                _v2_real(conf, None, None))

    def test_pesos_de_producao_nao_foram_alterados_pelos_patches(self):
        """O patch de configuração é temporário; o módulo real fica intacto."""
        antes = (rs.SCORE_V2_W_CONF, rs.SCORE_V2_W_ADX, rs.SCORE_V2_W_DER)
        _v2_real(70.0, 30.0, 0.0, {"conf": 9.0, "adx": 9.0, "der": 9.0})
        self.assertEqual((rs.SCORE_V2_W_CONF, rs.SCORE_V2_W_ADX, rs.SCORE_V2_W_DER),
                         antes)


# ════════════════════════════════════════════════════════════════════════════
#  B. ABLAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class Ablacao(unittest.TestCase):

    def test_score_e_a_propria_confluencia(self):
        for conf in (0.0, 12.5, 55.5, 70.0, 100.0):
            with self.subTest(conf=conf):
                out = lab.score_v3_conf_only(confluence_pct=conf)
                self.assertEqual(out["status"], lab.STATUS_OK)
                self.assertEqual(out["score"], round(conf, 1))
                self.assertEqual(out["effective_weights"], {"conf": 1.0})

    def test_variar_adx_e_funding_nao_move_a_ablacao_mas_move_a_v2(self):
        ablacoes, baselines = set(), set()
        for adx in (5.0, 25.0, 45.0):
            for fund in (-0.05, 0.0, 0.05):
                c = lab.compare_formulas(confluence_pct=70.0, adx=adx,
                                         funding_pct=fund, weights=W)
                ablacoes.add(c["ablation"]["score"])
                baselines.add(c["baseline"]["score"])
        self.assertEqual(ablacoes, {70.0})          # imune por construção
        self.assertGreater(len(baselines), 1)       # a V2 se move

    def test_sem_confluencia_nao_usa_outro_componente(self):
        out = lab.score_v3_conf_only(confluence_pct=None, adx=45.0, funding_pct=0.0)
        self.assertEqual(out["status"], lab.STATUS_UNAVAILABLE)
        self.assertEqual(out["reason_code"], lab.REASON_NO_CONFLUENCE)
        self.assertIsNone(out["score"])
        self.assertEqual(out["components"], {})

    def test_ausencia_de_um_lado_impede_o_delta(self):
        # sem confluência: ablação indisponível, mas a V2 ainda calcula
        c = lab.compare_formulas(confluence_pct=None, adx=30.0, funding_pct=None,
                                 weights=W)
        self.assertEqual(c["baseline"]["status"], lab.STATUS_OK)
        self.assertEqual(c["ablation"]["status"], lab.STATUS_UNAVAILABLE)
        self.assertFalse(c["comparable"])
        self.assertIsNone(c["delta"])
        self.assertEqual(c["reason_code"], lab.REASON_COMPARISON_UNAVAILABLE)
        # e sem nada: os dois indisponíveis
        vazio = lab.compare_formulas(confluence_pct=None, adx=None,
                                     funding_pct=None, weights=W)
        self.assertIsNone(vazio["delta"])
        self.assertFalse(vazio["comparable"])

    def test_zero_legitimo_permanece_zero(self):
        out = lab.score_v3_conf_only(confluence_pct=0.0)
        self.assertEqual(out["score"], 0.0)
        self.assertIsNotNone(out["score"])
        c = lab.compare_formulas(confluence_pct=0.0, adx=0.0, funding_pct=0.0,
                                 weights=W)
        self.assertTrue(c["comparable"])
        self.assertEqual(c["ablation"]["score"], 0.0)
        self.assertEqual(c["delta"], round(0.0 - c["baseline"]["score"], 6))

    def test_ablacao_nao_e_score_v3_aprovado(self):
        manifesto = lab.lab_manifest()
        self.assertFalse(manifesto["promotable"])
        self.assertFalse(manifesto["calibrated"])
        self.assertEqual(manifesto["execution_mode"], "LOCAL_RESEARCH_ONLY")
        texto = " ".join(manifesto["limitations"]).lower()
        self.assertIn("não é o score v3 definitivo", texto)
        self.assertIn("não elimina toda dupla contagem", texto)


# ════════════════════════════════════════════════════════════════════════════
#  C. CONTRATOS
# ════════════════════════════════════════════════════════════════════════════
class Contratos(unittest.TestCase):

    def test_bool_string_nan_e_infinito_sao_recusados(self):
        ruins = [True, False, "70", "abc", float("nan"), float("inf"),
                 float("-inf"), [70.0], {"v": 70}]
        for ruim in ruins:
            for campo in ("confluence_pct", "adx", "funding_pct"):
                with self.subTest(valor=repr(ruim), campo=campo):
                    out = lab.score_v2_raw(**{campo: ruim}, weights=W)
                    self.assertEqual(out["status"], lab.STATUS_INVALID_INPUT)
                    self.assertIn(out["reason_code"],
                                  (lab.REASON_NOT_NUMERIC, lab.REASON_NOT_FINITE))
                    self.assertIsNone(out["score"])

    def test_fora_do_dominio_documentado_e_recusado(self):
        for campo, valor in (("confluence_pct", -0.1), ("confluence_pct", 100.1),
                             ("adx", -1.0), ("adx", 100.1),
                             ("funding_pct", -100.1), ("funding_pct", 100.1)):
            with self.subTest(campo=campo, valor=valor):
                out = lab.score_v2_raw(**{campo: valor}, weights=W)
                self.assertEqual(out["reason_code"], lab.REASON_OUT_OF_DOMAIN)
                self.assertIsNone(out["score"])

    def test_configuracao_invalida_nao_e_corrigida_em_silencio(self):
        casos = [
            ({"conf": -1.0, "adx": 1.0, "der": 1.0}, lab.REASON_WEIGHTS_NEGATIVE),
            ({"conf": 0.0, "adx": 0.0, "der": 0.0}, lab.REASON_WEIGHTS_SUM_ZERO),
            ({"conf": 1.0, "adx": 1.0}, lab.REASON_WEIGHTS_MISSING_KEY),
            ({"conf": "1", "adx": 1.0, "der": 1.0}, lab.REASON_NOT_NUMERIC),
            ({"conf": float("nan"), "adx": 1.0, "der": 1.0}, lab.REASON_NOT_FINITE),
            ({"conf": True, "adx": 1.0, "der": 1.0}, lab.REASON_NOT_NUMERIC),
            (None, lab.REASON_WEIGHTS_NOT_NUMERIC),
            ("0.6,0.3,0.1", lab.REASON_WEIGHTS_NOT_NUMERIC),
        ]
        for pesos, motivo in casos:
            with self.subTest(pesos=repr(pesos)):
                out = lab.score_v2_raw(confluence_pct=70.0, weights=pesos)
                self.assertEqual(out["status"], lab.STATUS_INVALID_CONFIG)
                self.assertEqual(out["reason_code"], motivo)
                self.assertIsNone(out["score"])

    def test_hash_estavel_e_sensivel_a_configuracao(self):
        a = lab.score_v2_raw(confluence_pct=70.0, weights=W)
        b = lab.score_v2_raw(confluence_pct=90.0, weights=W)
        self.assertEqual(a["config_hash"], b["config_hash"])   # resultado não entra
        c = lab.score_v2_raw(confluence_pct=70.0,
                             weights={"conf": 0.61, "adx": 0.30, "der": 0.10})
        self.assertNotEqual(a["config_hash"], c["config_hash"])
        self.assertEqual(len(a["config_hash"]), 64)
        self.assertNotIn("score", json.dumps(a["config"]))

    def test_overflow_de_pesos_finitos_nao_produz_score_ou_json_invalido(self):
        casos = [
            # Soma dos pesos, produto individual e soma dos produtos, respectivamente.
            ({"conf": 1e308, "adx": 1e308, "der": 1e308}, 70., 30., 0.),
            ({"conf": 1e307, "adx": 0., "der": 0.}, 70., None, None),
            ({"conf": 0., "adx": 1e307, "der": 0.}, 70., 35., None),
            ({"conf": 0., "adx": 0., "der": 1e307}, 70., None, 0.),
            ({"conf": 1e306, "adx": 1e306, "der": 1e306}, 100., 50., -.05),
        ]
        for pesos, conf, adx, funding in casos:
            with self.subTest(pesos=pesos):
                self.assertTrue(all(math.isfinite(w) for w in pesos.values()))
                out = lab.score_v2_raw(confluence_pct=conf, adx=adx,
                                       funding_pct=funding, weights=pesos)
                self.assertEqual(out["status"], lab.STATUS_INVALID_CONFIG)
                self.assertEqual(out["reason_code"], "WEIGHTS_ARITHMETIC_OVERFLOW")
                self.assertIsNone(out["score"])
                self.assertEqual(out["contributions"], {})
                self.assertEqual(out["effective_weights"], {})
                json.dumps(out, allow_nan=False)

    def test_comparacao_com_overflow_nao_fabrica_delta(self):
        out = lab.compare_formulas(confluence_pct=70.,
                                   weights={"conf": 1e307, "adx": 0., "der": 0.})
        self.assertFalse(out["comparable"])
        self.assertIsNone(out["delta"])
        self.assertEqual(out["baseline"]["status"], lab.STATUS_INVALID_CONFIG)
        self.assertEqual(out["ablation"]["score"], 70.)
        json.dumps(out, allow_nan=False)

    def test_peso_alto_sem_overflow_preserva_paridade(self):
        pesos = {"conf": 1e300, "adx": 1e300, "der": 1e300}
        out = lab.score_v2_raw(confluence_pct=70., adx=30., funding_pct=0.,
                               weights=pesos)
        self.assertEqual(out["status"], lab.STATUS_OK)
        self.assertEqual(out["score"], _v2_real(70., 30., 0., pesos))
        self.assertAlmostEqual(sum(out["effective_weights"].values()), 1.)
        self.assertAlmostEqual(sum(out["contributions"].values()), 60., places=5)
        json.dumps(out, allow_nan=False)

    def test_entradas_nao_sao_mutadas(self):
        pesos = dict(W)
        copia = dict(pesos)
        lab.score_v2_raw(confluence_pct=70.0, adx=30.0, funding_pct=0.0,
                         weights=pesos)
        self.assertEqual(pesos, copia)

    def test_resultado_serializavel_sem_nan_ou_infinity(self):
        for chamada in (
            lab.score_v2_raw(confluence_pct=70.0, adx=30.0, funding_pct=0.0, weights=W),
            lab.score_v2_raw(confluence_pct=None, adx=None, funding_pct=None, weights=W),
            lab.score_v3_conf_only(confluence_pct=0.0),
            lab.compare_formulas(confluence_pct=70.0, adx=None, funding_pct=None,
                                 weights=W),
        ):
            texto = json.dumps(chamada, allow_nan=False)
            self.assertNotIn("NaN", texto)
            self.assertNotIn("Infinity", texto)

    def test_status_e_motivos_pertencem_ao_vocabulario_fechado(self):
        saidas = [
            lab.score_v2_raw(confluence_pct=70.0, weights=W),
            lab.score_v2_raw(confluence_pct=None, adx=None, funding_pct=None, weights=W),
            lab.score_v2_raw(confluence_pct="x", weights=W),
            lab.score_v2_raw(confluence_pct=70.0, weights={"conf": -1, "adx": 1, "der": 1}),
            lab.score_v3_conf_only(confluence_pct=None),
        ]
        for out in saidas:
            self.assertIn(out["status"], lab.LAB_STATUSES)
            self.assertIn(out["reason_code"], lab.LAB_REASON_CODES)
            self.assertIn(out["formula_id"], lab.KNOWN_FORMULAS)

    def test_proveniencia_e_formula_nao_se_confundem(self):
        base = lab.score_v2_raw(confluence_pct=70.0, weights=W)
        abl = lab.score_v3_conf_only(confluence_pct=70.0)
        self.assertEqual(base["formula_id"], lab.FORMULA_V2_RAW)
        self.assertEqual(abl["formula_id"], lab.FORMULA_V3_ABLATION)
        self.assertNotEqual(base["config_hash"], abl["config_hash"])

    def test_nenhuma_probabilidade_tier_ou_sizing_e_produzido(self):
        """Grep sobre CÓDIGO: a prosa do módulo cita esses termos justamente
        para dizer que NÃO os produz."""
        # (a) nenhum IDENTIFICADOR desses domínios é usado no código. O texto
        # explicativo de `LAB_LIMITATIONS` cita alguns deles de propósito, para
        # dizer que NÃO são produzidos — por isso o grep é sobre identificador,
        # não sobre substring solta.
        codigo = _codigo((BACKEND / "services" / "score_research_service.py").read_text())
        for proibido in ("prob_tp1", "prob_tp2", "KELLY_FRACTION", "SCORE_BINS",
                         "calibration_service", "suggested_size_pct",
                         "_classify_tier", "promotion_plan", "_compute_dynamic_size"):
            self.assertNotIn(proibido, codigo, proibido)
        # (b) comportamental: nenhuma CHAVE desses domínios sai no payload
        payload = lab.compare_formulas(confluence_pct=70.0, adx=30.0,
                                       funding_pct=0.0, weights=W)
        chaves = set()

        def _coleta(no):
            if isinstance(no, dict):
                for k, v in no.items():
                    chaves.add(str(k).lower())
                    _coleta(v)
            elif isinstance(no, list):
                for v in no:
                    _coleta(v)

        _coleta(payload)
        for proibido in ("prob_tp1", "prob_tp2", "tier", "suggested_size_pct",
                         "probability", "kelly", "bins", "size_pct"):
            self.assertNotIn(proibido, chaves, proibido)
        self.assertFalse(payload["promotable"])
        self.assertFalse(payload["calibrated"])

    def test_laboratorio_nao_le_env_relogio_banco_ou_rede(self):
        fonte = (BACKEND / "services" / "score_research_service.py").read_text()
        for proibido in ("os.getenv", "os.environ", "datetime.now", "time.time",
                         "get_session", "requests", "httpx", "open("):
            self.assertNotIn(proibido, fonte, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  D. AUDITORIA — achados reproduzidos numericamente
# ════════════════════════════════════════════════════════════════════════════
class Auditoria(unittest.TestCase):
    """Cada teste reproduz um achado CONFIRMADO. Repetição de um indicador em
    camadas distintas não é, por si, defeito — o que se afirma aqui é apenas o
    que os números mostram."""

    CFG_EXEC = {"SCORE_ADJUSTERS_ENABLED": True, "SCORE_ADJUSTER_CAP": 20.0}

    def _exec(self, *, score=60.0, **features):
        return p05._execution_score({"score": score, "features": features},
                                    self.CFG_EXEC)

    def test_A1_adx_sobe_a_formula_bruta_e_desce_no_ajuste_de_execucao(self):
        """Camadas isoladas: V2 sobe; adjuster cai com base FIXA, sem confluência."""
        v2_baixo = lab.score_v2_raw(confluence_pct=70.0, adx=5.0, funding_pct=None,
                                    weights=W)["score"]
        v2_alto = lab.score_v2_raw(confluence_pct=70.0, adx=45.0, funding_pct=None,
                                   weights=W)["score"]
        self.assertGreater(v2_alto, v2_baixo)          # bruta: mais ADX, mais score
        exec_baixo = self._exec(adx=5.0)
        exec_alto = self._exec(adx=45.0)
        self.assertLess(exec_alto, exec_baixo)         # execução: sinal invertido
        self.assertAlmostEqual(v2_alto - v2_baixo, 26.7, places=1)
        self.assertAlmostEqual(exec_baixo - exec_alto, 8.0, places=6)

    def test_A1_composicao_local_nao_confunde_ajuste_com_efeito_liquido(self):
        # Apenas V2 bruta + adjusters, não replay do bot com HTF/learning/seleção.
        scores = []
        for adx in (5., 45.):
            base = lab.score_v2_raw(confluence_pct=70., adx=adx, weights=W)["score"]
            scores.append(self._exec(score=base, confluence_pct=70., adx=adx))
        self.assertEqual(scores, [68., 86.7])
        self.assertGreater(scores[1], scores[0])

    def test_A2_confluencia_nao_monotonica_no_ajuste_de_execucao(self):
        """Ajuste isolado: base FIXA 60, demais features ausentes; não replay."""
        self.assertEqual(self._exec(confluence_pct=70.0), 72.0)
        self.assertEqual(self._exec(confluence_pct=70.1), 60.0)
        self.assertLess(self._exec(confluence_pct=70.1),
                        self._exec(confluence_pct=70.0))
        # e a fórmula bruta anda no sentido contrário no mesmo passo
        bruta = [lab.score_v2_raw(confluence_pct=c, weights=W)["score"]
                 for c in (70.0, 70.1)]
        self.assertGreater(bruta[1], bruta[0])
        # há também um degrau de 16 pontos na borda de 50
        self.assertEqual(self._exec(confluence_pct=49.9), 56.0)
        self.assertEqual(self._exec(confluence_pct=50.0), 72.0)

    def test_A3_funding_na_v2_ignora_o_lado_da_operacao(self):
        """A V2 bruta não recebe direção: long e short pontuam igual."""
        long_ = lab.compare_formulas(confluence_pct=70.0, funding_pct=-0.10,
                                     weights=W, direction="long")
        short = lab.compare_formulas(confluence_pct=70.0, funding_pct=-0.10,
                                     weights=W, direction="short")
        self.assertEqual(long_["baseline"]["score"], short["baseline"]["score"])
        self.assertEqual(long_["direction"], "long")
        self.assertEqual(short["direction"], "short")
        # funding negativo sempre eleva o componente, qualquer que seja o lado
        self.assertEqual(long_["baseline"]["components"]["der"], 100.0)

    def test_A4_saturacao_esconde_diferenca_real_de_indicador(self):
        adxs = {lab.score_v2_raw(confluence_pct=70.0, adx=a, weights=W)["score"]
                for a in (50.0, 75.0, 100.0)}
        self.assertEqual(len(adxs), 1)
        fundings = {lab.score_v2_raw(confluence_pct=70.0, funding_pct=f,
                                     weights=W)["score"]
                    for f in (0.05, 0.5, 5.0)}
        self.assertEqual(len(fundings), 1)

    def test_A5_arredondamento_pode_esconder_mudanca(self):
        iguais = {lab.score_v2_raw(confluence_pct=c, weights=W)["score"]
                  for c in (70.0, 70.01, 70.04)}
        self.assertEqual(iguais, {70.0})

    def test_A6_ausencia_renormaliza_e_muda_o_peso_efetivo(self):
        completo = lab.score_v2_raw(confluence_pct=70.0, adx=30.0, funding_pct=0.0,
                                    weights=W)
        so_conf = lab.score_v2_raw(confluence_pct=70.0, weights=W)
        self.assertAlmostEqual(completo["effective_weights"]["conf"], 0.6, places=9)
        self.assertAlmostEqual(so_conf["effective_weights"]["conf"], 1.0, places=9)
        # ausência não vira zero: o score sobe porque o denominador encolhe
        self.assertNotEqual(completo["score"], so_conf["score"])
        self.assertEqual(so_conf["missing_components"], ["adx", "der"])

    def test_A7_score_da_recomendacao_ainda_recebe_ajuste_de_execucao(self):
        """Snapshot guarda rec.score (pode conter HTF/learning), antes de adjusters."""
        base = 60.0
        self.assertNotEqual(self._exec(score=base, confluence_pct=60.0), base)
        # e o bônus de confirmação HTF é aditivo sobre o score-base
        self.assertGreater(rs.HIGH_TF_CONFIRM_BONUS, 0)
        fonte = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        self.assertIn("rec_score += _delta", fonte)
        self.assertIn("if rec_score < SCORE_MIN:", fonte)

    def test_A8_reuso_de_indicador_nao_e_declarado_defeito_sozinho(self):
        """Guarda contra exagero: o documento distingue as categorias."""
        doc = (BACKEND.parent / "docs" / "R08A_SCORE_AUDIT_AND_LOCAL_LAB.md").read_text()
        for termo in ("sobreposição estrutural", "reforço intencional",
                      "assimetria comprovada", "hipótese não demonstrada"):
            self.assertIn(termo, doc, termo)


# ════════════════════════════════════════════════════════════════════════════
#  E. ISOLAMENTO
# ════════════════════════════════════════════════════════════════════════════
class Isolamento(unittest.TestCase):

    def test_nenhum_consumidor_de_producao_importa_o_laboratorio(self):
        achados = []
        for caminho in BACKEND.rglob("*.py"):
            rel = caminho.relative_to(BACKEND.parent).as_posix()
            if "/tests/" in rel or "/__pycache__/" in rel or "/.venv" in rel:
                continue
            if rel.endswith("services/score_research_service.py"):
                continue
            if "score_research_service" in caminho.read_text(encoding="utf-8",
                                                             errors="ignore"):
                achados.append(rel)
        self.assertEqual(achados, [], str(achados))

    def test_frontend_nao_conhece_o_laboratorio(self):
        base = BACKEND.parent / "frontend" / "src"
        for caminho in base.rglob("*.ts*"):
            self.assertNotIn("score_research",
                             caminho.read_text(encoding="utf-8", errors="ignore"),
                             caminho.name)

    def test_servicos_operacionais_permanecem_intactos(self):
        """O pacote R08A concluído não alterou serviços existentes.

        Audita os commits da fase, sem confundir correções operacionais futuras
        no checkout com mudanças feitas pelo laboratório.
        """
        import subprocess
        alvos = ["backend/services", "backend/models", "backend/main.py",
                 "backend/db.py", "frontend/src"]
        res = subprocess.run(["git", "diff", "--name-only", "7d202144..892d53f2", "--", *alvos],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("range R08A 7d202144..892d53f2 indisponível neste checkout")
        # o próprio laboratório é ARQUIVO NOVO: depois do commit ele passa a
        # aparecer neste diff, e isso não é alteração de serviço existente.
        alterados = [ln for ln in res.stdout.splitlines()
                     if ln.strip() and ln.strip() != LAB_PATH]
        self.assertEqual(alterados, [], str(alterados))
        # e ele realmente não existia na baseline
        rastreado = subprocess.run(
            ["git", "ls-tree", "7d202144", "--name-only",
             "backend/services/score_research_service.py"],
            cwd=BACKEND.parent, capture_output=True, text=True)
        self.assertEqual(rastreado.stdout.strip(), "")
        self.assertTrue((BACKEND / "services" / "score_research_service.py").exists())

    def test_nenhuma_avaliacao_p05_ou_r07_e_disparada(self):
        fonte = (BACKEND / "services" / "score_research_service.py").read_text()
        for proibido in ("strategy_evidence_service", "regime_playbook_service",
                         "build_stop_diagnosis", "load_stop_shadow_split",
                         "build_regime_playbooks", "snapshot_service"):
            self.assertNotIn(proibido, fonte, proibido)

    def test_o_unico_import_de_producao_e_de_leitura_de_config(self):
        """`local_v2_weights` lê os pesos locais e é explicitamente rotulado."""
        fonte = (BACKEND / "services" / "score_research_service.py").read_text()
        self.assertEqual(fonte.count("from services import"), 1)
        self.assertIn("recommendation_service", fonte)
        bloco = fonte.split("def local_v2_weights")[1][:600]
        self.assertIn("configuração LOCAL", bloco)
        self.assertIn("não deve ser apresentada como", bloco)
        pesos = lab.local_v2_weights()
        self.assertEqual(sorted(pesos), ["adx", "conf", "der"])

    def test_avaliacao_nao_toca_a_rede(self):
        lab.compare_formulas(confluence_pct=70.0, adx=30.0, funding_pct=0.0,
                             weights=W)
        self.assertEqual(_NET_ATTEMPTS, [])


if __name__ == "__main__":
    unittest.main()
