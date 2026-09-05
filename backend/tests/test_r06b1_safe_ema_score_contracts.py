"""R06B1 — contratos seguros de EMA, dados finitos e proveniência do score.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, credencial, seed privado ou outcome de holdout. Só séries sintéticas
determinísticas.

DIFERENÇA PARA O R06A: lá os testes eram de CARACTERIZAÇÃO (travavam o
comportamento errado). Aqui são de CORREÇÃO e de PARIDADE:

  • correção  — o dado inválido agora falha fechado, e a fórmula do score
                registra quem realmente a produziu;
  • paridade  — para série VÁLIDA, valor, alinhamento, score e tier são
                exatamente os de antes. O renomeio não move nenhum número.

Fora de escopo (fica para o R06B2 e depois): mudar o score numérico, os bins,
a probabilidade, o Kelly ou o sizing; e trocar 12/26 por 9/21.
"""
from __future__ import annotations

import math
import unittest
from pathlib import Path

import pandas as pd

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste R06B1 (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import indicator_service as ind_svc              # noqa: E402
from services import calibration_service as calib              # noqa: E402
from services import confluence_service as conf_svc            # noqa: E402
from services import recommendation_service as rs              # noqa: E402
from models.trade_signal import (                              # noqa: E402
    ConfluenceScore, Indicator, SignalDirection, TradeSignal, TradeType,
)


# ════════════════════════════════════════════════════════════════════════════
#  Referências independentes e séries sintéticas
# ════════════════════════════════════════════════════════════════════════════
def ema_reference(values, window: int) -> list:
    """EMA recursiva `alpha = 2/(N+1)` desde o primeiro ponto — a convenção da
    lib `ta` instalada. Implementada AQUI, não importada do código auditado."""
    alpha = 2.0 / (window + 1.0)
    vals = [float(v) for v in values]
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + alpha * (v - out[-1]))
    return out


def _df(closes, *, high_pad=1.0, low_pad=1.0, volume=1000.0) -> pd.DataFrame:
    closes = [float(c) for c in closes]
    return pd.DataFrame({
        "open": closes,
        "high": [c + high_pad for c in closes],
        "low": [c - low_pad for c in closes],
        "close": closes,
        "volume": [volume] * len(closes),
    })


def _constante(n=120, valor=100.0):
    return [valor] * n


def _tendencia(n=120, base=100.0, passo=0.5):
    return [base + i * passo for i in range(n)]


def _reversao(n=120, base=100.0, passo=0.5):
    meio = n // 2
    subida = [base + i * passo for i in range(meio)]
    topo = subida[-1]
    return subida + [topo - i * passo for i in range(n - meio)]


def _oscilacao(n=120, base=100.0, amp=3.0, periodo=13.0):
    return [base + amp * math.sin(2 * math.pi * i / periodo) + i * 0.05
            for i in range(n)]


def _precos_pequenos(n=120):
    return [0.0000123 + 0.0000004 * math.sin(i / 5.0) for i in range(n)]


TODAS_AS_SERIES = {
    "constante": _constante(260),
    "tendencia": _tendencia(260),
    "reversao": _reversao(260),
    "oscilacao": _oscilacao(260),
    "precos_pequenos": _precos_pequenos(260),
}


def _codigo(fonte: str) -> str:
    """Fonte sem comentários e sem docstrings — para grep de CÓDIGO real.

    Aprendizado do R06A: `assertNotIn` cru casa com a própria prosa que
    escrevemos para explicar o contrato, e o teste passa/falha pelo motivo
    errado.
    """
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


def _rotulo(curta, media, longa):
    """Rótulo de alinhamento — reimplementado aqui, fora do código auditado."""
    if curta is None or media is None or longa is None:
        return None
    if curta > media > longa:
        return "bullish"
    if curta < media < longa:
        return "bearish"
    return "mixed"


# ════════════════════════════════════════════════════════════════════════════
#  A. EMA CANÔNICA
# ════════════════════════════════════════════════════════════════════════════
class EmaCanonica(unittest.TestCase):

    def test_ema12_e_a_ema_de_12_periodos(self):
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                out = ind_svc.calculate_indicators(_df(closes))
                self.assertAlmostEqual(
                    out.ema12, round(ema_reference(closes, 12)[-1], 6), places=5)

    def test_ema26_e_a_ema_de_26_periodos(self):
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                out = ind_svc.calculate_indicators(_df(closes))
                self.assertAlmostEqual(
                    out.ema26, round(ema_reference(closes, 26)[-1], 6), places=5)

    def test_ema50_e_ema200_inalteradas(self):
        closes = _oscilacao(260)
        out = ind_svc.calculate_indicators(_df(closes))
        self.assertAlmostEqual(out.ema50, round(ema_reference(closes, 50)[-1], 6), places=5)
        self.assertAlmostEqual(out.ema200, round(ema_reference(closes, 200)[-1], 6), places=5)

    def test_alias_legado_e_identico_ao_canonico(self):
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                out = ind_svc.calculate_indicators(_df(closes))
                self.assertEqual(out.ema9, out.ema12)
                self.assertEqual(out.ema21, out.ema26)

    def test_alias_legado_nao_recalcula_nada(self):
        """Só uma EMA de 12 e uma de 26 são computadas — o alias é espelho.

        Se alguém trocasse o alias por um segundo cálculo, `ema9` passaria a
        divergir de `ema12` (arredondamento/período) e o grep abaixo acharia
        uma chamada nova de EMAIndicator.
        """
        fonte = (BACKEND / "services" / "indicator_service.py").read_text()
        self.assertEqual(fonte.count("ta.trend.EMAIndicator("), 4)
        for janela in ("window=12", "window=26", "window=50", "window=200"):
            self.assertEqual(fonte.count(f"EMAIndicator(close, {janela})"), 1, janela)
        self.assertNotIn("window=9)", fonte)
        self.assertNotIn("window=21)", fonte)

    def test_macd_permanece_12_26_9(self):
        fonte = (BACKEND / "services" / "indicator_service.py").read_text()
        self.assertIn("MACD(close, window_slow=26, window_fast=12, window_sign=9)",
                      fonte)

    def test_serializacao_publica_canonico_e_legado(self):
        out = ind_svc.calculate_indicators(_df(_oscilacao(260)))
        d = out.model_dump()
        for campo in ("ema12", "ema26", "ema50", "ema200", "ema9", "ema21"):
            self.assertIn(campo, d, campo)
        self.assertEqual(d["ema9"], d["ema12"])
        self.assertEqual(d["ema21"], d["ema26"])

    def test_round_trip_de_serializacao_preserva_o_par(self):
        original = ind_svc.calculate_indicators(_df(_oscilacao(260)))
        volta = Indicator.model_validate(original.model_dump())
        self.assertEqual(volta.ema12, original.ema12)
        self.assertEqual(volta.ema26, original.ema26)
        self.assertEqual(volta.ema9, volta.ema12)
        self.assertEqual(volta.ema21, volta.ema26)

    def test_payload_antigo_so_com_nome_legado_ainda_carrega(self):
        """Consumidor/registro antigo que só conhece ema9/ema21 não quebra."""
        antigo = Indicator.model_validate({"ema9": 3.0, "ema21": 2.0, "ema50": 1.0})
        self.assertEqual(antigo.ema12, 3.0)
        self.assertEqual(antigo.ema26, 2.0)
        self.assertEqual(antigo.ema9, 3.0)
        self.assertEqual(antigo.ema21, 2.0)

    def test_canonico_vence_quando_os_dois_nomes_chegam(self):
        misto = Indicator.model_validate(
            {"ema12": 10.0, "ema26": 20.0, "ema9": 999.0, "ema21": 888.0})
        self.assertEqual(misto.ema12, 10.0)
        self.assertEqual(misto.ema9, 10.0)
        self.assertEqual(misto.ema26, 20.0)
        self.assertEqual(misto.ema21, 20.0)

    def test_ausencia_continua_ausencia_nos_dois_nomes(self):
        vazio = Indicator()
        for campo in ("ema12", "ema26", "ema9", "ema21", "ema50", "ema200"):
            self.assertIsNone(getattr(vazio, campo), campo)

    def test_consumidores_internos_usam_o_nome_canonico(self):
        alvos = {
            "indicator_service.py": ["ind.ema12", "ind.ema26"],
            "mtf_service.py": ["ind.ema12", "ind.ema26"],
            "confluence_service.py": ["ind.ema12", "ind.ema26"],
            "recommendation_service.py": ["ind.ema12", "ind.ema26"],
            "recommendation_backtest.py": ["ind.ema12", "ind.ema26"],
            "entry_planner.py": ["ind.ema26"],
            "ai_service.py": ["ind.ema12", "ind.ema26"],
        }
        for arquivo, esperados in alvos.items():
            texto = (BACKEND / "services" / arquivo).read_text()
            for e in esperados:
                self.assertIn(e, texto, f"{arquivo}: {e}")
            # e nenhum LEITOR legado sobrou (o modelo ainda declara os aliases)
            self.assertNotIn("ind.ema9", texto, arquivo)
            self.assertNotIn("ind.ema21", texto, arquivo)


# ════════════════════════════════════════════════════════════════════════════
#  B. PARIDADE — série válida decide igual
# ════════════════════════════════════════════════════════════════════════════
class ParidadeDeDecisoes(unittest.TestCase):

    def test_serie_valida_nunca_passa_a_ser_rejeitada(self):
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                pad = 1e-9 if nome == "precos_pequenos" else 1.0
                out = ind_svc.calculate_indicators(
                    _df(closes, high_pad=pad, low_pad=pad))
                for campo in ("ema12", "ema26", "ema50", "ema200", "rsi",
                              "macd", "atr", "adx", "bb_upper", "bb_lower"):
                    self.assertIsNotNone(getattr(out, campo), f"{nome}.{campo}")

    def test_alinhamento_de_ema_e_o_mesmo_pelos_dois_nomes(self):
        """O rótulo derivado do canônico e o derivado do alias coincidem —
        é a garantia de que o renomeio não move nenhuma decisão de tendência."""
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                out = ind_svc.calculate_indicators(_df(closes))
                canonico = _rotulo(out.ema12, out.ema26, out.ema50)
                legado = _rotulo(out.ema9, out.ema21, out.ema50)
                self.assertEqual(canonico, legado)

    def test_sinal_de_ema_trend_bate_com_a_referencia_externa(self):
        esperado = {"bullish": 1, "bearish": -1, "mixed": 0}
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                out = ind_svc.calculate_indicators(_df(closes))
                sinais = ind_svc.get_indicator_signals(out, closes[-1])
                self.assertEqual(
                    sinais["ema_trend"],
                    esperado[_rotulo(out.ema12, out.ema26, out.ema50)])

    def test_ema_aligned_label_do_mtf_bate_com_a_referencia(self):
        for nome, closes in TODAS_AS_SERIES.items():
            with self.subTest(serie=nome):
                out = ind_svc.calculate_indicators(_df(closes))
                sig = _sinal(out, timeframe="1h")
                self.assertEqual(rs._ema_aligned_label(sig),
                                 _rotulo(out.ema12, out.ema26, out.ema50))

    def test_confluence_soma_o_fator_de_ema_como_antes(self):
        """18 pontos de trend quando as EMAs empilham a favor — inalterado."""
        out = ind_svc.calculate_indicators(_df(_tendencia(260)))
        self.assertEqual(_rotulo(out.ema12, out.ema26, out.ema50), "bullish")
        closes = _tendencia(260)
        score = conf_svc.calculate_confluence(
            out, [], _df(closes), SignalDirection.LONG, float(closes[-1]))
        alinhadas = [f for f in score.factors if f.name.startswith("EMAs alinhadas")]
        self.assertEqual(len(alinhadas), 1)
        self.assertEqual(alinhadas[0].points, 18)
        self.assertIn("12", alinhadas[0].name)
        self.assertIn("26", alinhadas[0].name)

    def test_score_legado_bate_com_a_formula_reimplementada(self):
        """A aritmética legada é reproduzida aqui, fora do código auditado."""
        out = ind_svc.calculate_indicators(_df(_tendencia(260)))
        for conf_pct, rr, mtf_align in ((80.0, 3.0, 0.8), (40.0, 1.5, -0.4),
                                        (55.0, 2.2, 0.0)):
            with self.subTest(conf=conf_pct, rr=rr):
                sig = _sinal(out, conf_pct=conf_pct, risk_reward=rr,
                             mtf={"alignment_score": mtf_align})
                esperado = round(max(0.0, min(100.0, (
                    conf_pct * 0.35
                    + ((mtf_align + 1) * 50) * 0.25
                    + min(rr / 3.0, 1.0) * 100 * 0.25
                    + 50.0 * 0.10   # derivatives ausente = neutro
                ))), 1)
                self.assertEqual(rs._compute_score_legacy(sig), esperado)
                self.assertEqual(rs._compute_score(sig), esperado)

    def test_wrapper_do_score_devolve_o_mesmo_numero_da_proveniencia(self):
        out = ind_svc.calculate_indicators(_df(_oscilacao(260)))
        for conf_pct in (10.0, 45.0, 90.0):
            sig = _sinal(out, conf_pct=conf_pct)
            self.assertEqual(rs._compute_score(sig),
                             rs.compute_score_with_provenance(sig).score)

    def test_tier_nao_muda_para_o_mesmo_score(self):
        """Os cortes de tier continuam sendo os mesmos números.

        TF 4h porque o gate TF×tier (pré-existente, fora do escopo do R06B1)
        exige tier mínimo A em 1h — em 4h os três tiers são publicáveis.
        """
        out = ind_svc.calculate_indicators(_df(_tendencia(260)))
        sig = _sinal(out, timeframe="4h", conf_pct=90.0, risk_reward=3.0,
                     mtf={"alignment_score": 0.9})
        if rs.SCORE_FORMULA_V2:
            cortes = (rs.SCORE_V2_TIER_APLUS, rs.SCORE_V2_TIER_A, rs.SCORE_V2_TIER_B)
        else:
            cortes = (75.0, 65.0, 52.0)
        self.assertGreaterEqual(sig.confidence, rs.MIN_CONFIDENCE_B)
        self.assertGreaterEqual(sig.risk_reward, rs.MIN_RR)
        self.assertEqual(rs._classify_tier(sig, cortes[1]), "A")
        self.assertEqual(rs._classify_tier(sig, cortes[2]), "B")
        self.assertIsNone(rs._classify_tier(sig, cortes[2] - 0.1))
        # o classifier do vision continua delegando ao principal
        self.assertEqual(rs._classify_tier_vision(sig, cortes[1]),
                         rs._classify_tier(sig, cortes[1]))

    def test_backtest_usa_os_mesmos_valores(self):
        """O backtest lê o alinhamento pelo canônico e chega ao mesmo rótulo."""
        fonte = (BACKEND / "services" / "recommendation_backtest.py").read_text()
        self.assertIn("if ind.ema12 > ind.ema26 > ind.ema50:", fonte)
        self.assertIn("elif ind.ema12 < ind.ema26 < ind.ema50:", fonte)
        out = ind_svc.calculate_indicators(_df(_tendencia(260)))
        self.assertEqual(_rotulo(out.ema12, out.ema26, out.ema50), "bullish")


# ════════════════════════════════════════════════════════════════════════════
#  C. FINITUDE
# ════════════════════════════════════════════════════════════════════════════
class Finitude(unittest.TestCase):

    def test_safe_rejeita_nao_finito(self):
        for ruim in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(valor=ruim):
                self.assertIsNone(ind_svc._safe(pd.Series([1.0, ruim])))

    def test_safe_aceita_finito_inclusive_zero_e_negativo(self):
        self.assertEqual(ind_svc._safe(pd.Series([1.0, 0.0])), 0.0)
        self.assertEqual(ind_svc._safe(pd.Series([1.0, -7.5])), -7.5)
        self.assertEqual(ind_svc._safe(pd.Series([1.0, 1e-12])), 1e-12)

    def test_safe_com_serie_vazia_ou_ausente(self):
        self.assertIsNone(ind_svc._safe(None))
        self.assertIsNone(ind_svc._safe(pd.Series([], dtype=float)))

    def test_safe_recusa_string_e_objeto(self):
        self.assertIsNone(ind_svc._safe(pd.Series(["5.0"])))
        self.assertIsNone(ind_svc._safe(pd.Series([{"a": 1}], dtype=object)))
        self.assertIsNone(ind_svc._safe(pd.Series([None], dtype=object)))

    def test_finite_recusa_bool_como_indicador(self):
        """`True` é `int` em Python; um booleano nunca é preço/indicador."""
        self.assertIsNone(ind_svc._finite(True))
        self.assertIsNone(ind_svc._finite(False))

    def test_ausencia_nunca_vira_zero(self):
        vazio = ind_svc.calculate_indicators(_df(_tendencia(10)))
        for campo in ("ema12", "ema26", "rsi", "atr", "volume_avg",
                      "volume_last", "volume_ratio", "pivot_high", "pivot_low"):
            self.assertIsNone(getattr(vazio, campo), campo)
            self.assertNotEqual(getattr(vazio, campo), 0.0, campo)

    def test_volume_nao_finito_nao_vira_ratio_falso(self):
        closes = _tendencia(120)
        df = _df(closes)
        df.loc[df.index[-1], "volume"] = float("nan")
        out = ind_svc.calculate_indicators(df)
        self.assertIsNone(out.volume_last)
        self.assertIsNone(out.volume_ratio)

    def test_precos_pequenos_e_finitos_continuam_validos(self):
        closes = _precos_pequenos(260)
        out = ind_svc.calculate_indicators(_df(closes, high_pad=1e-9, low_pad=1e-9))
        self.assertIsNotNone(out.ema12)
        self.assertGreater(out.ema12, 0.0)
        self.assertIsNotNone(out.ema26)
        self.assertIsNotNone(out.atr_pct)


# ════════════════════════════════════════════════════════════════════════════
#  D. CANDLE MAIS RECENTE INVÁLIDO — FAIL-CLOSED
# ════════════════════════════════════════════════════════════════════════════
class CandleInvalido(unittest.TestCase):

    def _com_ultimo(self, valor):
        closes = _tendencia(260)
        df = _df(closes)
        df.loc[df.index[-1], "close"] = valor
        return ind_svc.calculate_indicators(df)

    def test_ultimo_close_nan_falha_fechado(self):
        out = self._com_ultimo(float("nan"))
        self.assertEqual(out, Indicator())

    def test_ultimo_close_infinito_falha_fechado(self):
        for ruim in (float("inf"), float("-inf")):
            with self.subTest(valor=ruim):
                self.assertEqual(self._com_ultimo(ruim), Indicator())

    def test_ultimo_close_ausente_falha_fechado(self):
        self.assertEqual(self._com_ultimo(None), Indicator())

    def test_nao_reaproveita_o_candle_anterior(self):
        """O contrato antigo devolvia o valor da série SEM o candle corrompido —
        um indicador com cara de atual. Isso não pode mais acontecer."""
        limpo = ind_svc.calculate_indicators(_df(_tendencia(260)[:-1]))
        self.assertIsNotNone(limpo.ema12)
        self.assertIsNone(self._com_ultimo(float("nan")).ema12)

    def test_contrato_vazio_e_o_mesmo_do_historico_insuficiente(self):
        curto = ind_svc.calculate_indicators(_df(_tendencia(10)))
        self.assertEqual(self._com_ultimo(float("nan")), curto)

    def test_candle_invalido_nao_produz_confirmacao_de_sinal(self):
        vazio = self._com_ultimo(float("nan"))
        self.assertEqual(ind_svc.get_indicator_signals(vazio, 100.0), {})
        self.assertIsNone(rs._ema_aligned_label(_sinal(vazio)))
        score = conf_svc.calculate_confluence(
            vazio, [], _df(_tendencia(260)), SignalDirection.LONG, 100.0)
        self.assertEqual(
            [f for f in score.factors if f.name.startswith("EMAs alinhadas")], [])

    def test_candle_valido_no_fim_continua_passando(self):
        """Furo no MEIO da série não é escopo deste gate — só o mais recente."""
        closes = _tendencia(260)
        df = _df(closes)
        df.loc[df.index[100], "close"] = float("nan")
        out = ind_svc.calculate_indicators(df)
        self.assertIsNotNone(out.ema12)


# ════════════════════════════════════════════════════════════════════════════
#  E. PROVENIÊNCIA DO SCORE
# ════════════════════════════════════════════════════════════════════════════
class ProvenienciaDoScore(unittest.TestCase):

    def setUp(self):
        self.ind = ind_svc.calculate_indicators(_df(_tendencia(260)))

    def test_legado_solicitado_registra_legado(self):
        sig = _sinal(self.ind, conf_pct=70.0)
        with unittest.mock.patch.object(rs, "SCORE_FORMULA_V2", False):
            p = rs.compute_score_with_provenance(sig)
        self.assertEqual(p.formula_requested, rs.SCORE_FORMULA_LEGACY_ID)
        self.assertEqual(p.formula_effective, rs.SCORE_FORMULA_LEGACY_ID)
        self.assertFalse(p.fallback_used)
        self.assertIsNone(p.fallback_reason)

    def test_v2_computavel_registra_v2(self):
        sig = _sinal(self.ind, conf_pct=70.0)
        with unittest.mock.patch.object(rs, "SCORE_FORMULA_V2", True):
            p = rs.compute_score_with_provenance(sig)
        self.assertEqual(p.formula_requested, rs.SCORE_FORMULA_V2_ID)
        self.assertEqual(p.formula_effective, rs.SCORE_FORMULA_V2_ID)
        self.assertFalse(p.fallback_used)
        self.assertIsNone(p.fallback_reason)

    def test_fallback_registra_legado_com_motivo_controlado(self):
        """Sem confluence, sem ADX e sem funding a V2 não é computável."""
        sig = _sinal(Indicator(), conf_pct=None, derivatives=None)
        with unittest.mock.patch.object(rs, "SCORE_FORMULA_V2", True):
            p = rs.compute_score_with_provenance(sig)
        self.assertEqual(p.formula_requested, rs.SCORE_FORMULA_V2_ID)
        self.assertEqual(p.formula_effective, rs.SCORE_FORMULA_LEGACY_ID)
        self.assertTrue(p.fallback_used)
        self.assertEqual(p.fallback_reason, rs.SCORE_FALLBACK_V2_NO_COMPONENTS)

    def test_motivo_pertence_a_vocabulario_fechado(self):
        self.assertIn(rs.SCORE_FALLBACK_V2_NO_COMPONENTS, rs.SCORE_FALLBACK_REASONS)
        sig = _sinal(Indicator(), conf_pct=None, derivatives=None)
        with unittest.mock.patch.object(rs, "SCORE_FORMULA_V2", True):
            p = rs.compute_score_with_provenance(sig)
        self.assertIn(p.fallback_reason, rs.SCORE_FALLBACK_REASONS)
        # nada de exceção crua, caminho de arquivo ou dado pessoal no motivo
        self.assertTrue(p.fallback_reason.replace("_", "").isalnum())

    def test_score_numerico_identico_nos_dois_caminhos(self):
        """A proveniência não muda o número, em nenhum dos dois modos."""
        for flag in (False, True):
            for conf_pct in (None, 20.0, 60.0, 95.0):
                with self.subTest(v2=flag, conf=conf_pct):
                    sig = _sinal(self.ind, conf_pct=conf_pct)
                    with unittest.mock.patch.object(rs, "SCORE_FORMULA_V2", flag):
                        p = rs.compute_score_with_provenance(sig)
                        direto = rs._compute_score(sig)
                    self.assertEqual(p.score, direto)
                    if p.formula_effective == rs.SCORE_FORMULA_LEGACY_ID:
                        self.assertEqual(p.score, rs._compute_score_legacy(sig))

    def test_bins_e_probabilidade_ficam_como_estao(self):
        """R06B1 só torna a incompatibilidade OBSERVÁVEL — quem bloqueia é o R06B2."""
        fonte = (BACKEND / "services" / "calibration_service.py").read_text()
        self.assertIn("SCORE_BINS = SCORE_BINS_V2 if _SCORE_FORMULA_V2 else SCORE_BINS_LEGACY",
                      fonte)
        self.assertNotEqual(calib.SCORE_BINS_V2, calib.SCORE_BINS_LEGACY)
        # nenhuma recusa/bloqueio por fórmula incompatível foi introduzida
        rec = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertNotIn("formula_mismatch", rec)
        self.assertNotIn("formula_mismatch", fonte)

    def test_proveniencia_sobrevive_a_serializacao(self):
        campo = rs.Recommendation.model_fields.get("score_provenance")
        self.assertIsNotNone(campo)
        self.assertIsNone(campo.default)
        p = rs.ScoreProvenance(score=61.2, formula_requested=rs.SCORE_FORMULA_V2_ID,
                               formula_effective=rs.SCORE_FORMULA_LEGACY_ID,
                               fallback_used=True,
                               fallback_reason=rs.SCORE_FALLBACK_V2_NO_COMPONENTS)
        d = p.as_dict()
        self.assertEqual(sorted(d), ["fallback_reason", "fallback_used",
                                     "formula_effective", "formula_requested",
                                     "score"])
        import json
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_proveniencia_nao_cria_coluna_nem_flag_nem_endpoint(self):
        import subprocess
        res = subprocess.run(
            ["git", "diff", "--unified=0", "1f336a0f", "--",
             "backend/db.py", "backend/models", "backend/main.py"],
            cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 1f336a0f indisponível neste checkout")
        adicionadas = [ln for ln in res.stdout.splitlines()
                       if ln.startswith("+") and not ln.startswith("+++")]
        for ln in adicionadas:
            self.assertNotIn("ADD COLUMN", ln)
            self.assertNotIn("os.getenv", ln)
            self.assertNotIn("@app.get", ln)
            self.assertNotIn("@app.post", ln)


# ════════════════════════════════════════════════════════════════════════════
#  F. NOMENCLATURA DA CALIBRAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class NomenclaturaDaCalibracao(unittest.TestCase):

    def setUp(self):
        self.fonte = (BACKEND / "services" / "calibration_service.py").read_text()

    def test_novo_nome_existe_e_o_antigo_delega(self):
        self.assertTrue(hasattr(calib, "_load_shadow_pairs"))
        self.assertTrue(hasattr(calib, "_load_real_pairs"))
        legado = self.fonte.split("async def _load_real_pairs")[1].split("\nasync def ")[0]
        self.assertIn("return await _load_shadow_pairs()", legado)

    def test_wrapper_legado_nao_duplica_a_query(self):
        legado = self.fonte.split("async def _load_real_pairs")[1].split("\nasync def ")[0]
        for proibido in ("select(", "get_session", "RecommendationSnapshot"):
            self.assertNotIn(proibido, legado, proibido)
        self.assertEqual(self.fonte.count("RecommendationSnapshot.status.in_("), 1)

    def test_fonte_e_filtros_sao_exatamente_os_de_antes(self):
        novo = self.fonte.split("async def _load_shadow_pairs")[1].split("\nasync def ")[0]
        corpo = novo.split('"""')[2]
        for esperado in ("RecommendationSnapshot.status.in_(RESOLVED_STATUSES)",
                         "conds.append(_not_fast_void())",
                         "if LOOKBACK_DAYS > 0:",
                         "RecommendationSnapshot.outcome_at >= since",
                         "RecommendationSnapshot.score"):
            self.assertIn(esperado, corpo, esperado)

    def test_nenhuma_consulta_a_realtrade_foi_introduzida(self):
        """Grep sobre CÓDIGO — docstrings e comentários citam `RealTrade` de
        propósito, para dizer justamente que ele NÃO é lido aqui."""
        codigo = _codigo(self.fonte)
        self.assertNotIn("RealTrade", codigo)
        self.assertNotIn("real_trade", codigo)
        self.assertIn("RecommendationSnapshot", codigo)

    def test_caminho_de_calibracao_chama_o_nome_novo(self):
        self.assertIn("real_pairs = await _load_shadow_pairs()", self.fonte)


# ════════════════════════════════════════════════════════════════════════════
#  G. FRONTEND
# ════════════════════════════════════════════════════════════════════════════
class Frontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        base = BACKEND.parent / "frontend" / "src"
        cls.painel = (base / "components" / "SignalPanel" / "SignalPanel.tsx").read_text()
        cls.tipos = (base / "types" / "index.ts").read_text()

    def test_tipo_declara_canonico_e_legado(self):
        for campo in ("ema12?: number", "ema26?: number",
                      "ema9?: number", "ema21?: number"):
            self.assertIn(campo, self.tipos, campo)

    def test_painel_le_canonico_com_fallback_legado(self):
        self.assertIn("ind.ema12 ?? ind.ema9", self.painel)
        self.assertIn("ind.ema26 ?? ind.ema21", self.painel)
        self.assertIn("signal.indicators.ema12 ?? signal.indicators.ema9", self.painel)
        self.assertIn("signal.indicators.ema26 ?? signal.indicators.ema21", self.painel)
        # e o valor exibido no grid sai do canônico, não do alias
        self.assertIn("{fmt(ema12)}", self.painel)

    def test_painel_exibe_12_26(self):
        self.assertIn("EMA 12/26/50", self.painel)
        self.assertIn("(12 > 26 > 50)", self.painel)
        self.assertIn("(12 < 26 < 50)", self.painel)

    def test_correcao_textual_do_r06a_preservada(self):
        bloco = self.painel.split("AGUARDAR CONFLUÊNCIA")[1][:600]
        self.assertNotIn("Probabilidade", bloco)
        self.assertIn("Força do sinal", bloco)
        self.assertIn("não representa probabilidade de lucro", self.painel)

    def test_limite_e_botoes_intactos(self):
        self.assertIn("signal.confidence < 0.75", self.painel)
        self.assertIn("signal.confidence >= 0.75", self.painel)
        self.assertIn("<ConfidenceBar value={signal.confidence} />", self.painel)
        self.assertIn("Adicionar ao Gestor de Trades", self.painel)


# ════════════════════════════════════════════════════════════════════════════
#  H. ESCOPO DO PACOTE
# ════════════════════════════════════════════════════════════════════════════
class EscopoDoPacote(unittest.TestCase):

    def _diff(self, *caminhos):
        import subprocess
        res = subprocess.run(["git", "diff", "--name-only", "1f336a0f", "--", *caminhos],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 1f336a0f indisponível neste checkout")
        return [ln for ln in res.stdout.splitlines() if ln.strip()]

    def test_dist_do_frontend_nao_regerado(self):
        alterados = [ln for ln in self._diff("frontend/dist")
                     if not ln.endswith("index.html")]
        self.assertEqual(alterados, [], str(alterados))

    def test_execucao_risco_e_sizing_intactos(self):
        alterados = self._diff(
            "backend/services/shadow_trade_service.py",
            "backend/services/kill_switch_service.py",
            "backend/services/risk_service.py",
            "backend/services/financial_risk_service.py",
            "backend/services/trade_manager_service.py",
            "backend/services/execution_accounting_service.py",
            "backend/services/binance_signed_service.py",
        )
        self.assertEqual(alterados, [], str(alterados))

    def test_periodos_9_21_nao_foram_ativados(self):
        fonte = (BACKEND / "services" / "indicator_service.py").read_text()
        self.assertIn("EMAIndicator(close, window=12)", fonte)
        self.assertIn("EMAIndicator(close, window=26)", fonte)
        self.assertNotIn("EMAIndicator(close, window=9)", fonte)
        self.assertNotIn("EMAIndicator(close, window=21)", fonte)

    def test_kelly_e_sizing_nao_mudaram(self):
        fonte = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertIn("KELLY_FRACTION = 0.25", fonte)
        self.assertIn("ATR_REFERENCE_PCT = 0.02", fonte)
        self.assertIn('_TIER_WR_FALLBACK = {"A+": 0.62, "A": 0.55, "B": 0.50}', fonte)
        tamanho, motivo = rs._compute_dynamic_size(
            score=100.0, tier="A", risk_reward=3.0, prob_tp1=0.7, atr_pct=None)
        kelly = (0.7 * 3.0 - 0.3) / 3.0
        self.assertIn(f"Kelly {kelly*100:.1f}%", motivo)
        self.assertIsNotNone(tamanho)


# ── Fábrica de sinais sintéticos (sem rede, sem exchange) ───────────────────
def _sinal(ind: Indicator, *, timeframe="1h", conf_pct=70.0, risk_reward=2.0,
           mtf=None, derivatives=None, direction=SignalDirection.LONG,
           confidence=0.6) -> TradeSignal:
    confluence = None
    if conf_pct is not None:
        confluence = ConfluenceScore(total=conf_pct, max_total=100.0,
                                     pct=conf_pct, factors=[])
    return TradeSignal(
        symbol="TESTUSDT",
        timeframe=timeframe,
        direction=direction,
        trade_type=TradeType.DAY_TRADE,
        confidence=confidence,
        entry=100.0,
        stop_loss=98.0,
        tp1=102.0,
        tp2=104.0,
        tp3=106.0,
        risk_reward=risk_reward,
        patterns=[],
        indicators=ind,
        confluence=confluence,
        mtf=mtf,
        derivatives=derivatives,
        timestamp=0,
        signal_strength="moderate",
    )


import unittest.mock  # noqa: E402  (usado nos testes de proveniência)

if __name__ == "__main__":
    unittest.main()
