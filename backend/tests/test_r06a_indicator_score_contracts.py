"""R06A — auditoria de EMAs e semântica de scores/probabilidades.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, credencial ou outcome de holdout. Somente séries sintéticas
determinísticas.

NATUREZA DESTES TESTES: vários deles são de CARACTERIZAÇÃO — documentam o
comportamento ATUAL, inclusive onde ele está errado. Um teste verde aqui NÃO
significa que o defeito foi corrigido; significa que o defeito está travado e
descrito. Os pontos marcados `DEFEITO` estão detalhados em
`docs/R06A_EMA_SCORE_AUDIT.md` com a correção proposta para o R06B.
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
    raise RuntimeError(f"REDE BLOQUEADA no teste R06A (hermético): {a[:1]}")


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
from models.trade_signal import Indicator                      # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
#  Referência INDEPENDENTE de EMA
# ════════════════════════════════════════════════════════════════════════════
def ema_reference(values, window: int, *, seed: str = "recursive") -> list:
    """EMA de referência, implementada aqui e não importada da lib auditada.

        alpha = 2 / (N + 1)

    Duas convenções de INICIALIZAÇÃO, ambas legítimas:

    - `recursive`: recursão desde o primeiro ponto (`ewm(adjust=False)`), que é
      a convenção da biblioteca `ta` instalada;
    - `sma_seed`: semente igual à SMA dos primeiros N pontos, a convenção
      "clássica" de manual.

    Divergência ENTRE convenções não é bug — é escolha. O que importa aqui é
    qual PERÍODO cada campo carrega.
    """
    alpha = 2.0 / (window + 1.0)
    vals = [float(v) for v in values]
    if seed == "sma_seed":
        if len(vals) < window:
            return []
        out = [sum(vals[:window]) / window]
        for v in vals[window:]:
            out.append(out[-1] + alpha * (v - out[-1]))
        return out
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + alpha * (v - out[-1]))
    return out


def _df(closes, *, high_pad=1.0, low_pad=1.0, volume=1000.0) -> pd.DataFrame:
    """OHLCV sintético determinístico a partir de uma série de fechamentos."""
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
    return subida + [topo - (i + 1) * passo for i in range(n - meio)]


def _oscilacao(n=120, base=100.0, amp=6.0, periodo=18):
    """Onda cujo período separa CLARAMENTE médias de 9/21 e de 12/26."""
    return [base + amp * math.sin(2 * math.pi * i / periodo) for i in range(n)]


def _precos_pequenos(n=120, base=0.00000123):
    return [base * (1 + 0.01 * math.sin(i / 3)) for i in range(n)]


# ════════════════════════════════════════════════════════════════════════════
#  A. QUAIS PERÍODOS O SISTEMA REALMENTE CALCULA
# ════════════════════════════════════════════════════════════════════════════
class PeriodosDeEma(unittest.TestCase):

    def test_convencao_da_lib_e_recursiva_desde_o_inicio(self):
        """A lib `ta` usa `ewm(adjust=False)`, não semente SMA. Isso é escolha,
        não defeito — mas muda o valor e precisa estar documentado."""
        import ta

        closes = _tendencia(60)
        serie = pd.Series(closes)
        lib = ta.trend.EMAIndicator(serie, window=5).ema_indicator()
        rec = ema_reference(closes, 5, seed="recursive")
        sma = ema_reference(closes, 5, seed="sma_seed")
        # a lib segue a recursão desde o índice 0, ponto a ponto
        self.assertAlmostEqual(float(lib.iloc[-1]), rec[-1], places=8)
        self.assertAlmostEqual(float(lib.iloc[4]), rec[4], places=10)
        # e NÃO a semente SMA — a diferença é máxima logo após o warm-up e
        # decai com o tempo (numa tendência linear longa elas convergem)
        self.assertNotAlmostEqual(rec[4], sma[0], places=3)
        # warm-up: os N-1 primeiros vêm mascarados como NaN
        self.assertTrue(all(pd.isna(v) for v in lib[:4]))
        self.assertFalse(pd.isna(lib.iloc[4]))

    def test_campo_ema12_carrega_periodo_12(self):
        """R06B1: o campo canônico `ema12` carrega a EMA de 12 períodos."""
        closes = _oscilacao(120)
        out = ind_svc.calculate_indicators(_df(closes))
        self.assertAlmostEqual(out.ema12, round(ema_reference(closes, 12)[-1], 6), places=5)
        self.assertNotAlmostEqual(out.ema12, ema_reference(closes, 9)[-1], places=3)

    def test_campo_ema26_carrega_periodo_26(self):
        """R06B1: o campo canônico `ema26` carrega a EMA de 26 períodos."""
        closes = _oscilacao(120)
        out = ind_svc.calculate_indicators(_df(closes))
        self.assertAlmostEqual(out.ema26, round(ema_reference(closes, 26)[-1], 6), places=5)
        self.assertNotAlmostEqual(out.ema26, ema_reference(closes, 21)[-1], places=3)

    def test_alias_legado_espelha_o_canonico(self):
        """R06B1: `ema9`/`ema21` sobrevivem como ALIAS — mesmo valor, sem
        recálculo. Continuam NÃO sendo EMA(9)/EMA(21)."""
        closes = _oscilacao(120)
        out = ind_svc.calculate_indicators(_df(closes))
        self.assertEqual(out.ema9, out.ema12)
        self.assertEqual(out.ema21, out.ema26)

    def test_ema50_e_ema200_conferem_com_o_nome(self):
        closes = _oscilacao(260)
        out = ind_svc.calculate_indicators(_df(closes))
        self.assertAlmostEqual(out.ema50, round(ema_reference(closes, 50)[-1], 6), places=5)
        self.assertAlmostEqual(out.ema200, round(ema_reference(closes, 200)[-1], 6), places=5)

    def test_macd_usa_12_26_9_e_isso_esta_correto(self):
        """Os períodos 12/26/9 do MACD são legítimos — não confundir com as EMAs."""
        fonte = (BACKEND / "services" / "indicator_service.py").read_text()
        self.assertIn("MACD(close, window_slow=26, window_fast=12, window_sign=9)",
                      fonte)

    def test_ema200_indisponivel_com_menos_de_200_candles(self):
        out = ind_svc.calculate_indicators(_df(_tendencia(150)))
        self.assertIsNone(out.ema200)
        self.assertIsNotNone(out.ema50)

    def test_menos_de_50_candles_devolve_indicador_vazio(self):
        for n in (0, 1, 20, 49):
            out = ind_svc.calculate_indicators(_df(_tendencia(max(n, 1))[:n] or [1.0]))
            if n < 50:
                self.assertIsNone(out.ema12, f"n={n}")
                self.assertIsNone(out.rsi, f"n={n}")

    def test_serie_constante_produz_emas_iguais(self):
        out = ind_svc.calculate_indicators(_df(_constante(120, 250.0)))
        for campo in ("ema12", "ema26", "ema50"):
            self.assertAlmostEqual(getattr(out, campo), 250.0, places=6, msg=campo)

    def test_precos_muito_pequenos_nao_colapsam(self):
        closes = _precos_pequenos(120)
        out = ind_svc.calculate_indicators(_df(closes, high_pad=1e-9, low_pad=1e-9))
        self.assertIsNotNone(out.ema12)
        self.assertGreater(out.ema12, 0.0)
        # o arredondamento de saída não pode zerar um preço de 1e-6
        self.assertNotEqual(out.ema12, 0.0)

    def test_CORRIGIDO_nan_no_ultimo_close_falha_fechado(self):
        """R06B1: o defeito F3 do R06A está corrigido.

        Antes, um `close` NaN no candle mais recente era ignorado — `ewm`
        carregava o último valor válido e o indicador saía com cara de atual.
        Agora o contrato é vazio: nada de indicador para candle corrompido.
        """
        closes = _tendencia(120)
        closes[-1] = float("nan")
        out = ind_svc.calculate_indicators(_df(closes))
        self.assertIsNone(out.ema12)
        self.assertIsNone(out.ema9)
        self.assertIsNone(out.rsi)
        self.assertIsNone(out.atr)
        # e NÃO o valor da série sem o candle corrompido (o antigo comportamento)
        limpo = ind_svc.calculate_indicators(_df(_tendencia(120)[:-1]))
        self.assertIsNotNone(limpo.ema12)

    def test_CORRIGIDO_safe_rejeita_infinito(self):
        """R06B1: o defeito F2 do R06A está corrigido — `_safe` só aceita finito."""
        self.assertIsNone(ind_svc._safe(pd.Series([1.0, float("nan")])))
        self.assertIsNone(ind_svc._safe(None))
        self.assertIsNone(ind_svc._safe(pd.Series([], dtype=float)))
        self.assertIsNone(ind_svc._safe(pd.Series([1.0, float("inf")])))
        self.assertIsNone(ind_svc._safe(pd.Series([1.0, float("-inf")])))


# ════════════════════════════════════════════════════════════════════════════
#  B. OS CONSUMIDORES ESPERAM ESSES PERÍODOS?
# ════════════════════════════════════════════════════════════════════════════
class ConsumidoresDeEma(unittest.TestCase):

    def test_alinhamento_usa_os_campos_canonicos(self):
        """R06B1: consumidores internos migraram para ema12/ema26."""
        fonte = (BACKEND / "services" / "indicator_service.py").read_text()
        self.assertIn("if ind.ema12 > ind.ema26 > ind.ema50:", fonte)
        for arquivo in ("mtf_service.py", "confluence_service.py",
                        "recommendation_backtest.py"):
            texto = (BACKEND / "services" / arquivo).read_text()
            self.assertIn("ind.ema12", texto, arquivo)
            self.assertIn("ind.ema26", texto, arquivo)

    def test_documentacao_do_regime_afirma_12_26_50(self):
        """R06B1: o comentário do `regime_service` agora descreve o que o código
        realmente empilha."""
        texto = (BACKEND / "services" / "regime_service.py").read_text()
        self.assertIn("EMA12/26/50", texto)
        self.assertNotIn("EMA9/21/50", texto)

    def test_entry_planner_ancora_no_campo_ema26(self):
        """R06B1: a zona de pullback passa a se chamar pelo período que usa."""
        texto = (BACKEND / "services" / "entry_planner.py").read_text()
        self.assertIn("ema26 = ind.ema26", texto)
        self.assertIn("Pullback à EMA26", texto)
        self.assertIn("# EMA26 pullback", texto)

    def _rotulo(self, e_curta, e_media, e_longa):
        if e_curta > e_media > e_longa:
            return "bullish"
        if e_curta < e_media < e_longa:
            return "bearish"
        return "mixed"

    def test_alinhamento_atual_difere_de_9_21_50_em_serie_sintetica(self):
        """Comparação SOMENTE em teste: quantos rótulos mudariam com 9/21/50.

        Não infere rentabilidade — apenas mede que a decisão de alinhamento
        NÃO é invariante à troca de períodos.
        """
        divergencias = 0
        total = 0
        for gerador in (_tendencia, _reversao, _oscilacao):
            closes = gerador(220)
            for corte in range(60, len(closes) + 1, 5):
                janela = closes[:corte]
                atual = self._rotulo(ema_reference(janela, 12)[-1],
                                     ema_reference(janela, 26)[-1],
                                     ema_reference(janela, 50)[-1])
                proposto = self._rotulo(ema_reference(janela, 9)[-1],
                                        ema_reference(janela, 21)[-1],
                                        ema_reference(janela, 50)[-1])
                total += 1
                if atual != proposto:
                    divergencias += 1
        self.assertGreater(total, 50)
        self.assertGreater(divergencias, 0,
                           "a troca de períodos precisa mudar algum rótulo")
        # Ordem de grandeza registrada no doc; aqui só travamos que é material.
        self.assertLess(divergencias, total,
                        "os rótulos também não podem divergir sempre")


# ════════════════════════════════════════════════════════════════════════════
#  C. SEMÂNTICA DE confidence
# ════════════════════════════════════════════════════════════════════════════
class SemanticaConfidence(unittest.TestCase):

    def test_confidence_e_fracao_0_a_1(self):
        from models.trade_signal import DetectedPattern, SignalDirection
        from services import signal_service

        ind = Indicator(rsi=25.0, macd=1.0, macd_signal=0.5, ema12=3.0,
                        ema26=2.0, ema50=1.0, bb_upper=110.0, bb_lower=90.0,
                        stoch_k=10.0, stoch_d=10.0, adx=30.0)
        valor = signal_service.calculate_confidence(
            ind, [], SignalDirection.LONG, 89.0)
        self.assertGreaterEqual(valor, 0.0)
        self.assertLessEqual(valor, 1.0)

    def test_confidence_sem_indicadores_cai_para_0_5(self):
        from models.trade_signal import SignalDirection
        from services import signal_service

        valor = signal_service.calculate_confidence(
            Indicator(), [], SignalDirection.LONG, 100.0)
        self.assertEqual(valor, 0.5)

    def test_confidence_e_derivada_de_confluence_no_build(self):
        """No caminho principal, `confidence = confluence.pct / 100` — uma
        pontuação heurística reescalada, NÃO uma probabilidade calibrada."""
        fonte = (BACKEND / "services" / "signal_service.py").read_text()
        self.assertIn("confidence = round(confluence.pct / 100, 2)", fonte)

    def test_confidence_nao_vem_da_calibracao(self):
        fonte = (BACKEND / "services" / "signal_service.py").read_text()
        self.assertNotIn("prob_tp1", fonte)
        self.assertNotIn("calibration_service", fonte)


# ════════════════════════════════════════════════════════════════════════════
#  C. SEMÂNTICA DE score
# ════════════════════════════════════════════════════════════════════════════
class SemanticaScore(unittest.TestCase):

    def test_score_v2_renormaliza_sobre_o_que_existe(self):
        from services import recommendation_service as rs

        cheio = rs._compute_score_v2(80.0, 25.0, 0.01)
        so_conf = rs._compute_score_v2(80.0, None, None)
        self.assertIsNotNone(cheio)
        self.assertEqual(so_conf, 80.0)
        for valor in (cheio, so_conf):
            self.assertGreaterEqual(valor, 0.0)
            self.assertLessEqual(valor, 100.0)

    def test_score_v2_none_quando_nada_e_computavel(self):
        from services import recommendation_service as rs
        self.assertIsNone(rs._compute_score_v2(None, None, None))

    def test_fallback_para_legado_deixou_de_ser_silencioso(self):
        """R06B1: V2 e legado seguem em escalas diferentes (isso é o R06B2),
        mas o fallback agora REGISTRA qual fórmula produziu o número."""
        fonte = (BACKEND / "services" / "recommendation_service.py").read_text()
        bloco = fonte.split("def compute_score_with_provenance(")[1].split("\ndef ")[0]
        self.assertIn("if v2 is not None:", bloco)
        self.assertIn("SCORE_FORMULA_V2", bloco)
        self.assertIn("formula_effective", bloco)
        self.assertIn("score_provenance: Optional[dict] = None", fonte)

    def test_bins_da_calibracao_seguem_a_mesma_flag(self):
        fonte = (BACKEND / "services" / "calibration_service.py").read_text()
        self.assertIn("SCORE_BINS = SCORE_BINS_V2 if _SCORE_FORMULA_V2 else SCORE_BINS_LEGACY",
                      fonte)
        self.assertNotEqual(calib.SCORE_BINS_V2, calib.SCORE_BINS_LEGACY)

    def test_score_legado_fora_dos_bins_v2(self):
        """Um score legado (55–100) cai FORA dos bins V2 no topo da faixa."""
        v2_hi = calib.SCORE_BINS_V2[-1][1]
        self.assertLess(v2_hi, 100.0)
        fora = [s for s in (80.0, 90.0, 99.0)
                if not any(lo <= s < hi for lo, hi in calib.SCORE_BINS_V2)]
        self.assertEqual(len(fora), 3)


# ════════════════════════════════════════════════════════════════════════════
#  C. SEMÂNTICA DE prob_tp1 / prob_tp2
# ════════════════════════════════════════════════════════════════════════════
class SemanticaProbabilidades(unittest.TestCase):

    def setUp(self):
        calib.invalidate_cache()

    def tearDown(self):
        calib.invalidate_cache()

    def _pares(self, n=200):
        """Pares (score, status) sintéticos: score alto ganha mais."""
        pares = []
        for i in range(n):
            lo, hi = calib.SCORE_BINS[i % len(calib.SCORE_BINS)]
            score = lo + (hi - lo) / 2
            idx = i % len(calib.SCORE_BINS)
            if idx >= 6:
                status = "won_tp2"
            elif idx >= 3:
                status = "won_tp1"
            else:
                status = "lost"
            pares.append((score, status))
        return pares

    def test_evento_de_prob_tp1_e_atingir_tp1(self):
        self.assertEqual(calib.WIN_STATUSES, ("won_tp1", "won_tp1_be", "won_tp2"))
        self.assertEqual(calib.TP2_WIN_STATUSES, ("won_tp2",))

    def test_tp2_nunca_excede_tp1_na_mesma_populacao(self):
        out = calib.compute_calibration_from_pairs(self._pares(), source="teste")
        self.assertIsNotNone(out)
        for b in out["bins"]:
            self.assertLessEqual(b["p_tp2_observed"], b["p_observed"],
                                 f'bin {b["label"]}')
        self.assertLessEqual(out["p_tp2_global"], out["p_global"])

    def test_probabilidades_ficam_em_0_a_1(self):
        out = calib.compute_calibration_from_pairs(self._pares(), source="teste")
        for b in out["bins"]:
            for chave in ("p_observed", "p_shrunk", "p_calibrated",
                          "p_tp2_observed", "p_tp2_calibrated"):
                self.assertGreaterEqual(b[chave], 0.0, chave)
                self.assertLessEqual(b[chave], 1.0, chave)

    def test_monotonicidade_apos_isotonica(self):
        out = calib.compute_calibration_from_pairs(self._pares(), source="teste")
        cal = [b["p_calibrated"] for b in out["bins"]]
        self.assertEqual(cal, sorted(cal), "p_calibrated deve ser não-decrescente")

    def test_amostra_vazia_devolve_none_nao_zero(self):
        self.assertIsNone(calib.compute_calibration_from_pairs([], source="teste"))

    def test_cache_vazio_devolve_none_nao_zero(self):
        calib.invalidate_cache()
        self.assertIsNone(calib.prob_tp1_for_score_sync(60.0))
        self.assertIsNone(calib.prob_tp2_for_score_sync(60.0))
        self.assertIsNone(calib.prob_tp1_for_score_sync(None))

    def test_bordas_dos_bins_sao_semiabertas(self):
        for i, (lo, hi) in enumerate(calib.SCORE_BINS):
            self.assertEqual(calib._bin_index(lo), i, f"lo={lo}")
            if i + 1 < len(calib.SCORE_BINS):
                self.assertEqual(calib._bin_index(hi), i + 1, f"hi={hi}")

    def test_CORRIGIDO_fora_do_range_nao_cai_mais_no_global(self):
        """R06B2: o defeito de contrato do R06A está corrigido.

        Antes, score sem bin compatível devolvia `p_global` — uma estatística
        AGREGADA usada como probabilidade INDIVIDUAL, que seguia para os gates
        e para o sizing. Agora devolve ausência.
        """
        self.assertEqual(calib._bin_index(calib.SCORE_BINS[0][0] - 1), -1)
        self.assertEqual(calib._bin_index(calib.SCORE_BINS[-1][1] + 1), -1)
        calib._cache["data"] = {"bins": [{"p_calibrated": 0.9,
                                          "p_tp2_calibrated": 0.5}],
                                "p_global": 0.42, "p_tp2_global": 0.21}
        self.assertIsNone(calib.prob_tp1_for_score_sync(-999.0))
        self.assertIsNone(calib.prob_tp2_for_score_sync(-999.0))
        # p_global segue existindo como estatística agregada da calibração
        self.assertEqual(calib._cache["data"]["p_global"], 0.42)

    def test_minimo_de_amostra_e_do_chamador(self):
        self.assertEqual(calib.MIN_SAMPLE_TOTAL, 30)
        # o núcleo puro NÃO aplica o mínimo — quem aplica é quem carrega os pares
        out = calib.compute_calibration_from_pairs([(60.0, "lost")], source="teste")
        self.assertIsNotNone(out)
        self.assertEqual(out["total_resolved"], 1)

    def test_CORRIGIDO_fonte_shadow_tem_nome_shadow(self):
        """R06B1: o defeito F6 do R06A está corrigido — o loader diz SHADOW,
        e continua lendo `RecommendationSnapshot` (nunca `RealTrade`)."""
        fonte = (BACKEND / "services" / "calibration_service.py").read_text()
        bloco = fonte.split("async def _load_shadow_pairs")[1].split("\nasync def ")[0]
        self.assertIn("RecommendationSnapshot", bloco)
        self.assertIn("SHADOW", bloco)
        self.assertNotIn("trades REAIS", bloco)
        # a query em si não toca no financeiro
        query = bloco.split('"""')[2]
        self.assertNotIn("RealTrade", query)


# ════════════════════════════════════════════════════════════════════════════
#  D. UNIDADE, FALLBACK E CONSUMO NO SIZING
# ════════════════════════════════════════════════════════════════════════════
class UnidadesEConsumo(unittest.TestCase):

    def test_prob_e_fracao_e_confidence_tambem(self):
        """Ambos 0–1 no backend; a multiplicação por 100 é só apresentação."""
        fonte = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertIn("prob_tp1: Optional[float] = None", fonte)
        self.assertIn("0..1", fonte.split("prob_tp1: Optional[float] = None")[1][:60])

    def test_sizing_usa_prob_tp1_com_fallback_por_tier(self):
        from services import recommendation_service as rs

        tamanho, motivo = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=0.7, atr_pct=0.02)
        self.assertIsNotNone(tamanho)
        self.assertIn("p=70%", motivo)
        # None NÃO vira zero: cai no fallback por tier
        t2, m2 = rs._compute_dynamic_size(
            score=80.0, tier="A", risk_reward=2.0, prob_tp1=None, atr_pct=0.02)
        self.assertIsNotNone(t2)
        self.assertNotEqual(m2, motivo)

    def test_DEFEITO_kelly_mistura_evento_tp1_com_rr_do_alvo_final(self):
        """DEFEITO (semântico): Kelly usa `b = risk_reward` (RR até o alvo
        final) com `p = prob_tp1` (probabilidade de tocar o TP1).

        Kelly pressupõe que `p` é a chance de ganhar `b` unidades. Como
        P(TP1) >= P(alvo final), a fração resultante é OTIMISTA. O teste
        caracteriza a fórmula atual; NÃO altera o sizing.
        """
        from services import recommendation_service as rs

        p, b = 0.7, 3.0
        kelly_esperado = (p * b - (1 - p)) / b
        tamanho, motivo = rs._compute_dynamic_size(
            score=100.0, tier="A", risk_reward=b, prob_tp1=p, atr_pct=None)
        self.assertIn(f"Kelly {kelly_esperado*100:.1f}%", motivo)
        self.assertIsNotNone(tamanho)
        # se o evento fosse o alvo final, p seria menor e Kelly cairia
        p_alvo_final = 0.45
        kelly_correto = (p_alvo_final * b - (1 - p_alvo_final)) / b
        self.assertGreater(kelly_esperado, kelly_correto)

    def test_kelly_negativo_nao_vira_tamanho_zero(self):
        from services import recommendation_service as rs
        tamanho, motivo = rs._compute_dynamic_size(
            score=50.0, tier="C", risk_reward=1.0, prob_tp1=0.2, atr_pct=0.02)
        self.assertIsNone(tamanho)
        self.assertIn("Kelly negativo", motivo)


# ════════════════════════════════════════════════════════════════════════════
#  APRESENTAÇÃO — regressão do texto corrigido
# ════════════════════════════════════════════════════════════════════════════
class ApresentacaoFrontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.painel = (BACKEND.parent / "frontend" / "src" / "components"
                      / "SignalPanel" / "SignalPanel.tsx")
        cls.src = cls.painel.read_text()

    def test_confidence_nao_e_mais_chamado_de_probabilidade(self):
        bloco = self.src.split("AGUARDAR CONFLUÊNCIA")[1][:600]
        self.assertNotIn("Probabilidade", bloco)
        self.assertIn("Força do sinal", bloco)
        self.assertIn("não representa probabilidade de lucro", self.src)

    def test_valor_numerico_e_limite_preservados(self):
        self.assertIn("signal.confidence < 0.75", self.src)
        self.assertIn("signal.confidence >= 0.75", self.src)
        self.assertIn("<ConfidenceBar value={signal.confidence} />", self.src)
        self.assertIn("(signal.confidence * 100).toFixed(0)", self.src)

    def test_botao_e_ordenacao_intactos(self):
        self.assertIn("Adicionar ao Gestor de Trades", self.src)
        self.assertIn("onAddToManager", self.src)

    def test_referencia_em_escala_de_pontos(self):
        bloco = self.src.split("AGUARDAR CONFLUÊNCIA")[1][:600]
        self.assertIn("/100", bloco)


# ════════════════════════════════════════════════════════════════════════════
#  ESCOPO DO PACOTE — nada operacional foi alterado
# ════════════════════════════════════════════════════════════════════════════
class EscopoDoPacote(unittest.TestCase):

    def test_calculo_dos_indicadores_intacto(self):
        import subprocess
        for caminho in ("backend/services/indicator_service.py",
                        "backend/services/signal_service.py",
                        "backend/services/recommendation_service.py",
                        "backend/services/calibration_service.py",
                        "backend/services/confluence_service.py",
                        "backend/services/mtf_service.py",
                        "backend/services/regime_service.py"):
            # Escopo do R06A = o RANGE de commits do R06A (61265ed1..1f336a0f).
            # Comparar com a árvore de trabalho faria fases posteriores
            # autorizadas (R06B1) quebrarem esta garantia retroativamente.
            res = subprocess.run(["git", "diff", "--name-only", "61265ed1", "1f336a0f",
                                  "--", caminho],
                                 cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest("baseline 61265ed1 indisponível neste checkout")
            self.assertEqual(res.stdout.strip(), "",
                             f"arquivo operacional alterado: {caminho}")

    def test_dist_do_frontend_intacto(self):
        import subprocess
        res = subprocess.run(["git", "diff", "--name-only", "61265ed1", "1f336a0f",
                              "--", "frontend/dist"],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline indisponível")
        alterados = [ln for ln in res.stdout.splitlines()
                     if ln.strip() and not ln.endswith("index.html")]
        self.assertEqual(alterados, [], str(alterados))


if __name__ == "__main__":
    unittest.main()
