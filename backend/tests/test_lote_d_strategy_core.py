"""Bloco D — núcleo puro de estratégia e os três playbooks (R07+R08).

Cobre bull/bear/range/conflito, vela aberta, ausência, finitude, geometria,
monotonicidade, arbitragem determinística e a fronteira com o executor real.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import strategy_core_service as core  # noqa: E402

BAR_MS = 3_600_000
T0 = 1_700_000_000_000


def _bars(count, *, open_=100.3, high=100.6, low=100.2, close=100.4, volume=100.0,
          start_index=0):
    return [core.Candle(open_time_ms=T0 + (start_index + i) * BAR_MS, open=open_,
                        high=high, low=low, close=close, volume=volume)
            for i in range(count)]


def bull_pullback_state(**override):
    """Tendência de alta, recuo até a EMA rápida e retomada em vela fechada."""
    trigger = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.3, high=101.2,
                          low=100.2, close=101.0, volume=90.0)
    payload = dict(
        symbol="BTCUSDT", timeframe="1h", bar_ms=BAR_MS, as_of_ms=T0 + 80 * BAR_MS,
        bars=tuple(_bars(79) + [trigger]), atr=1.0, ema_fast=100.0, ema_slow=98.0,
        adx=30.0, rsi=62.0, volume_ma=100.0, target_levels=(102.5, 103.5),
        higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="long",
            confirmed=True, strength=0.8),))
    payload.update(override)
    return core.MarketState(**payload)


def bear_pullback_state(**override):
    """Espelho exato do cenário de alta."""
    trigger = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=99.7, high=99.8,
                          low=98.8, close=99.0, volume=90.0)
    payload = dict(
        symbol="BTCUSDT", timeframe="1h", bar_ms=BAR_MS, as_of_ms=T0 + 80 * BAR_MS,
        bars=tuple(_bars(79, open_=99.7, high=99.8, low=99.4, close=99.6) + [trigger]),
        atr=1.0, ema_fast=100.0, ema_slow=102.0, adx=30.0, rsi=38.0, volume_ma=100.0,
        target_levels=(97.5, 96.5),
        higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="short",
            confirmed=True, strength=0.8),))
    payload.update(override)
    return core.MarketState(**payload)


def breakout_state(**override):
    """Rompimento acima da máxima da janela, com volume."""
    trigger = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.5, high=102.2,
                          low=100.4, close=102.0, volume=300.0)
    payload = dict(
        symbol="ETHUSDT", timeframe="1h", bar_ms=BAR_MS, as_of_ms=T0 + 80 * BAR_MS,
        bars=tuple(_bars(79) + [trigger]), atr=1.0, ema_fast=100.0, ema_slow=98.0,
        adx=30.0, volume_ma=100.0, target_levels=(105.5, 107.0),
        higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="long",
            confirmed=True, strength=0.8),))
    payload.update(override)
    return core.MarketState(**payload)


def range_state(**override):
    """Range provado, toque na borda inferior e rejeição confirmada."""
    trigger = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.5, high=102.0,
                          low=100.1, close=101.8, volume=120.0)
    payload = dict(
        symbol="SOLUSDT", timeframe="1h", bar_ms=BAR_MS, as_of_ms=T0 + 80 * BAR_MS,
        bars=tuple(_bars(79, open_=105.0, high=105.5, low=104.5, close=105.0) + [trigger]),
        atr=2.0, ema_fast=105.0, ema_slow=105.2, adx=15.0, volume_ma=100.0,
        range_high=110.0, range_low=100.0, range_touches=5, regime_label="NORMAL")
    payload.update(override)
    return core.MarketState(**payload)


class Pureza(unittest.TestCase):
    def test_imports_sem_io(self):
        """O núcleo não importa banco, rede, provider nem serviço de produção."""
        tree = ast.parse((BACKEND / "services" / "strategy_core_service.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"dataclasses", "hashlib", "json", "math", "os", "typing"})

    def test_executor_nao_importa_o_candidato(self):
        """A rota real continua inacessível ao candidato."""
        executores = ("shadow_trade_service.py", "trade_manager_service.py",
                      "signal_service.py", "recommendation_service.py",
                      "entry_planner.py", "real_trade_service.py")
        for nome in executores:
            texto = (BACKEND / "services" / nome).read_text()
            self.assertNotIn("strategy_core_service", texto, nome)
            self.assertNotIn("score_v3_service", texto, nome)

    def test_modo_default_inativo(self):
        anterior = os.environ.pop(core.MODE_ENV, None)
        try:
            self.assertEqual(core.selected_mode(), core.MODE_INACTIVE)
            self.assertFalse(core.core_active())
            os.environ[core.MODE_ENV] = "live"
            self.assertEqual(core.selected_mode(), core.MODE_INACTIVE)
            os.environ[core.MODE_ENV] = "simulation"
            self.assertEqual(core.selected_mode(), core.MODE_SIMULATION)
        finally:
            os.environ.pop(core.MODE_ENV, None)
            if anterior is not None:
                os.environ[core.MODE_ENV] = anterior


class ValidacaoDeEstado(unittest.TestCase):
    def test_estado_valido(self):
        verdict = core.validate_state(bull_pullback_state())
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["trigger_candle_ms"], T0 + 79 * BAR_MS)

    def test_vela_ainda_aberta_nao_decide(self):
        aberta = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.3, high=101.2,
                             low=100.2, close=101.0, volume=90.0, closed=False)
        state = bull_pullback_state(bars=tuple(_bars(79) + [aberta]))
        verdict = core.validate_state(state)
        self.assertIn(core.BAR_NOT_CLOSED, verdict["reason_codes"])
        self.assertEqual(core.decide(state)["state"], core.STATE_UNKNOWN)

    def test_decisao_antes_do_fechamento_da_vela(self):
        state = bull_pullback_state(as_of_ms=T0 + 80 * BAR_MS - 1)
        self.assertIn(core.DECISION_BEFORE_BAR_CLOSE,
                      core.validate_state(state)["reason_codes"])

    def test_estado_velho_demais(self):
        state = bull_pullback_state(as_of_ms=T0 + 84 * BAR_MS)
        self.assertIn(core.STATE_STALE, core.validate_state(state)["reason_codes"])

    def test_barras_fora_de_ordem_e_com_buraco(self):
        fora = list(_bars(79))
        fora[10], fora[11] = fora[11], fora[10]
        trigger = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.3, high=101.2,
                              low=100.2, close=101.0, volume=90.0)
        state = bull_pullback_state(bars=tuple(fora + [trigger]))
        self.assertIn(core.BARS_UNORDERED, core.validate_state(state)["reason_codes"])

        salteadas = _bars(40) + _bars(40, start_index=41)
        state = bull_pullback_state(bars=tuple(salteadas), as_of_ms=T0 + 81 * BAR_MS)
        self.assertIn(core.BARS_GAPPED, core.validate_state(state)["reason_codes"])

    def test_amostra_insuficiente(self):
        state = bull_pullback_state(bars=tuple(_bars(10)), as_of_ms=T0 + 10 * BAR_MS)
        self.assertIn(core.BARS_INSUFFICIENT, core.validate_state(state)["reason_codes"])

    def test_evidencia_htf_do_futuro(self):
        state = bull_pullback_state(higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 90 * BAR_MS, direction="long",
            confirmed=True, strength=0.9),))
        self.assertIn(core.HTF_EVIDENCE_UNPROVEN,
                      core.validate_state(state)["reason_codes"])

    def test_dados_nao_finitos_sao_recusados(self):
        with self.assertRaises(ValueError):
            core.Candle(open_time_ms=T0, open=float("nan"), high=1.0, low=0.5, close=0.8)
        with self.assertRaises(ValueError):
            bull_pullback_state(atr=float("inf"))
        with self.assertRaises(ValueError):
            core.Candle(open_time_ms=T0, open=1.0, high=0.5, low=0.9, close=0.8)


class TrendPullback(unittest.TestCase):
    def test_bull_elegivel_com_geometria_estrutural(self):
        decision = core.evaluate_trend_pullback(bull_pullback_state())
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)
        self.assertEqual(decision["side"], "long")
        levels = decision["levels"]
        self.assertAlmostEqual(levels["entry"], 101.0)
        self.assertAlmostEqual(levels["stop_loss"], 99.9)
        self.assertAlmostEqual(levels["tp1"], 102.5)
        self.assertAlmostEqual(levels["tp2"], 103.5)
        self.assertGreaterEqual(decision["evidence"]["rr2"], 1.8)
        self.assertIsNotNone(decision["invalidation"])

    def test_bear_e_espelho_do_bull(self):
        decision = core.evaluate_trend_pullback(bear_pullback_state())
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)
        self.assertEqual(decision["side"], "short")
        levels = decision["levels"]
        self.assertAlmostEqual(levels["entry"], 99.0)
        self.assertAlmostEqual(levels["stop_loss"], 100.1)
        self.assertLess(levels["tp1"], levels["entry"])
        self.assertLess(levels["tp2"], levels["tp1"])

    def test_oscilador_nao_inverte_o_lado(self):
        """RSI extremo é filtro: não vira short dentro de tendência de alta."""
        for rsi in (5.0, 95.0):
            decision = core.evaluate_trend_pullback(bull_pullback_state(rsi=rsi))
            self.assertEqual(decision["side"], "long", rsi)

    def test_sem_tendencia_confirmada(self):
        decision = core.evaluate_trend_pullback(bull_pullback_state(adx=10.0))
        self.assertEqual(decision["state"], core.STATE_INELIGIBLE)
        self.assertEqual(decision["reason_codes"], (core.TREND_NOT_CONFIRMED,))

    def test_indicador_ausente_vira_unknown(self):
        decision = core.evaluate_trend_pullback(bull_pullback_state(adx=None))
        self.assertEqual(decision["state"], core.STATE_UNKNOWN)
        self.assertEqual(decision["reason_codes"], (core.TREND_UNKNOWN,))
        self.assertEqual(decision["missing_features"], ("adx",))

        decision = core.evaluate_trend_pullback(bull_pullback_state(atr=None))
        self.assertEqual(decision["state"], core.STATE_UNKNOWN)
        self.assertEqual(decision["missing_features"], ("atr",))

    def test_preco_longe_da_estrutura(self):
        state = bull_pullback_state(ema_fast=90.0, ema_slow=85.0)
        decision = core.evaluate_trend_pullback(state)
        self.assertEqual(decision["reason_codes"], (core.PULLBACK_NOT_AT_STRUCTURE,))

    def test_sem_gatilho_de_retomada(self):
        sem_gatilho = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.3,
                                  high=100.5, low=100.2, close=100.45, volume=90.0)
        state = bull_pullback_state(bars=tuple(_bars(79) + [sem_gatilho]))
        decision = core.evaluate_trend_pullback(state)
        self.assertEqual(decision["reason_codes"], (core.TRIGGER_ABSENT,))


class TrendBreakout(unittest.TestCase):
    def test_rompimento_confirmado(self):
        decision = core.evaluate_trend_breakout(breakout_state())
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)
        self.assertAlmostEqual(decision["levels"]["entry"], 102.0)
        self.assertAlmostEqual(decision["levels"]["stop_loss"], 100.3)
        self.assertAlmostEqual(decision["evidence"]["reference"], 100.6)

    def test_nome_de_padrao_nao_prova_rompimento(self):
        sem_rompimento = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.3,
                                     high=100.7, low=100.2, close=100.65, volume=500.0)
        state = breakout_state(bars=tuple(_bars(79) + [sem_rompimento]),
                               patterns=("ascending_triangle_breakout", "bull_flag"))
        decision = core.evaluate_trend_breakout(state)
        self.assertEqual(decision["state"], core.STATE_INELIGIBLE)
        self.assertEqual(decision["reason_codes"], (core.BREAKOUT_NOT_CONFIRMED,))

    def test_volume_ausente_vira_unknown_e_volume_fraco_reprova(self):
        sem_volume = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=100.5, high=102.2,
                                 low=100.4, close=102.0)
        decision = core.evaluate_trend_breakout(
            breakout_state(bars=tuple(_bars(79) + [sem_volume])))
        self.assertEqual(decision["state"], core.STATE_UNKNOWN)
        self.assertEqual(decision["reason_codes"], (core.VOLUME_UNKNOWN,))
        self.assertEqual(decision["missing_features"], ("bar_volume",))

        decision = core.evaluate_trend_breakout(breakout_state(volume_ma=1000.0))
        self.assertEqual(decision["reason_codes"], (core.VOLUME_INSUFFICIENT,))

    def test_reteste_exigido(self):
        config = core.CoreConfig(require_retest=True)
        decision = core.evaluate_trend_breakout(breakout_state(), config)
        self.assertEqual(decision["state"], core.STATE_UNKNOWN)
        self.assertEqual(decision["reason_codes"], (core.RETEST_UNKNOWN,))

        decision = core.evaluate_trend_breakout(
            breakout_state(retest_confirmed=False), config)
        self.assertEqual(decision["reason_codes"], (core.RETEST_MISSING,))

        decision = core.evaluate_trend_breakout(
            breakout_state(retest_confirmed=True), config)
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)


class RangeReversion(unittest.TestCase):
    def test_reversao_na_borda_inferior(self):
        decision = core.evaluate_range_reversion(range_state())
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)
        self.assertEqual(decision["side"], "long")
        levels = decision["levels"]
        self.assertAlmostEqual(levels["stop_loss"], 99.4)
        self.assertLess(levels["tp2"], 110.0)  # alvo compatível com o range
        self.assertGreater(levels["tp2"], levels["tp1"])

    def test_regime_normal_nao_prova_lateralidade(self):
        state = range_state(range_high=None, range_low=None, range_touches=None,
                            regime_label="NORMAL")
        decision = core.evaluate_range_reversion(state)
        self.assertEqual(decision["state"], core.STATE_UNKNOWN)
        self.assertEqual(decision["reason_codes"], (core.RANGE_UNKNOWN,))

    def test_ausencia_de_tendencia_nao_cria_reversao(self):
        """ADX baixo sem range provado não vira RANGE_REVERSION."""
        state = range_state(range_touches=1)
        decision = core.evaluate_range_reversion(state)
        self.assertEqual(decision["state"], core.STATE_INELIGIBLE)
        self.assertEqual(decision["reason_codes"], (core.RANGE_NOT_PROVEN,))

    def test_rejeicao_precisa_ser_confirmada(self):
        sem_rejeicao = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=101.5,
                                   high=101.6, low=100.1, close=100.2, volume=120.0)
        state = range_state(bars=tuple(
            _bars(79, open_=105.0, high=105.5, low=104.5, close=105.0) + [sem_rejeicao]))
        decision = core.evaluate_range_reversion(state)
        self.assertEqual(decision["reason_codes"], (core.REJECTION_NOT_CONFIRMED,))

    def test_sem_borda_disponivel(self):
        meio = core.Candle(open_time_ms=T0 + 79 * BAR_MS, open=105.0, high=105.5,
                           low=104.5, close=105.2, volume=120.0)
        state = range_state(bars=tuple(
            _bars(79, open_=105.0, high=105.5, low=104.5, close=105.0) + [meio]))
        decision = core.evaluate_range_reversion(state)
        self.assertEqual(decision["reason_codes"], (core.BORDER_UNAVAILABLE,))

    def test_range_estreito_reprova(self):
        decision = core.evaluate_range_reversion(range_state(atr=8.0))
        self.assertEqual(decision["reason_codes"], (core.RANGE_NOT_PROVEN,))


class Contratendencia(unittest.TestCase):
    def test_conflito_htf_bloqueia_continuacao(self):
        state = bull_pullback_state(higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="short",
            confirmed=True, strength=0.9),))
        decision = core.evaluate_trend_pullback(state)
        self.assertEqual(decision["state"], core.STATE_INELIGIBLE)
        self.assertEqual(decision["reason_codes"], (core.HTF_CONFLICT_BLOCKED,))

    def test_range_nao_tem_excecao_contra_htf_forte(self):
        state = range_state(higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="short",
            confirmed=True, strength=0.95),))
        decision = core.evaluate_range_reversion(state)
        self.assertEqual(decision["state"], core.STATE_INELIGIBLE)
        self.assertEqual(decision["reason_codes"], (core.HTF_CONFLICT_BLOCKED,))

    def test_htf_desconhecido_nao_prova_alinhamento(self):
        decision = core.evaluate_trend_pullback(bull_pullback_state(higher_timeframes=()))
        self.assertEqual(decision["state"], core.STATE_UNKNOWN)
        self.assertEqual(decision["reason_codes"], (core.HTF_UNKNOWN,))

    def test_forca_htf_desconhecida_e_tratada_como_conflito(self):
        state = bull_pullback_state(higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="short",
            confirmed=True, strength=None),))
        self.assertEqual(core.evaluate_trend_pullback(state)["reason_codes"],
                         (core.HTF_CONFLICT_BLOCKED,))

    def test_htf_nao_confirmado_nao_alinha_nem_bloqueia(self):
        state = bull_pullback_state(higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * BAR_MS, direction="short",
            confirmed=False, strength=0.9),))
        decision = core.evaluate_trend_pullback(state)
        self.assertEqual(decision["reason_codes"], (core.HTF_UNKNOWN,))


class Geometria(unittest.TestCase):
    def test_stop_sem_estrutura(self):
        verdict = core.structural_stop(side="long", entry=100.0, atr=1.0,
                                       levels=[101.0, 102.0], buffer_atr=0.3)
        self.assertIsNone(verdict["price"])
        self.assertEqual(verdict["reason_code"], core.STOP_STRUCTURE_UNAVAILABLE)

    def test_alvos_exigem_estrutura_e_separacao(self):
        vazio = core.structural_targets(side="long", entry=100.0, atr=1.0, levels=(),
                                        min_separation_atr=0.5)
        self.assertEqual(vazio["reason_code"], core.TARGET_STRUCTURE_UNAVAILABLE)

        colado = core.structural_targets(side="long", entry=100.0, atr=1.0,
                                         levels=(101.0, 101.2),
                                         min_separation_atr=0.5)
        self.assertEqual(colado["reason_code"], core.TP_SEPARATION_BELOW_FLOOR)
        self.assertIsNone(colado["tp2"])

    def test_rr_insuficiente_reprova_sem_alargar_stop(self):
        state = bull_pullback_state(target_levels=(101.6, 102.2))
        decision = core.evaluate_trend_pullback(state)
        self.assertEqual(decision["state"], core.STATE_INELIGIBLE)
        self.assertEqual(decision["reason_codes"], (core.RR_BELOW_FLOOR,))
        # O stop continua sendo o estrutural: não foi alargado para caber o R:R.
        self.assertAlmostEqual(decision["levels"]["stop_loss"], 99.9)

    def test_tp_impossivel_nao_e_criado(self):
        decision = core.evaluate_trend_pullback(bull_pullback_state(target_levels=(99.0,)))
        self.assertEqual(decision["reason_codes"], (core.TARGET_STRUCTURE_UNAVAILABLE,))

    def test_monotonicidade_do_rr(self):
        anterior = None
        for alvo in (103.5, 104.5, 105.5, 106.5):
            decision = core.evaluate_trend_pullback(
                bull_pullback_state(target_levels=(102.5, alvo)))
            rr2 = decision["evidence"]["rr2"]
            if anterior is not None:
                self.assertGreater(rr2, anterior)
            anterior = rr2

    def test_risco_nao_positivo_e_geometria_invalida(self):
        verdict = core.rr_verdict(side="long", entry=100.0, stop=100.0, tp1=101.0,
                                  tp2=102.0)
        self.assertEqual(verdict["reason_code"], core.GEOMETRY_INVALID)


class Arbitragem(unittest.TestCase):
    def _fake(self, playbook, state, side):
        return {"playbook": playbook, "playbook_version": core.PLAYBOOK_VERSIONS[playbook],
                "state": state, "side": side, "reason_codes": (core.OK,),
                "missing_features": (), "levels": None, "invalidation": None,
                "evidence": {}}

    def test_lados_opostos_bloqueiam(self):
        verdict = core.arbitrate([
            self._fake(core.PLAYBOOK_TREND_PULLBACK, core.STATE_ELIGIBLE, "long"),
            self._fake(core.PLAYBOOK_RANGE_REVERSION, core.STATE_ELIGIBLE, "short"),
        ])
        self.assertIsNone(verdict["selected"])
        self.assertEqual(verdict["reason_code"], core.ARBITRATION_SIDE_CONFLICT)
        self.assertEqual(verdict["conflicting_sides"], ("long", "short"))

    def test_mesmo_lado_resolve_por_prioridade(self):
        decisoes = [
            self._fake(core.PLAYBOOK_TREND_BREAKOUT, core.STATE_ELIGIBLE, "long"),
            self._fake(core.PLAYBOOK_TREND_PULLBACK, core.STATE_ELIGIBLE, "long"),
        ]
        self.assertEqual(core.arbitrate(decisoes)["selected"]["playbook"],
                         core.PLAYBOOK_TREND_PULLBACK)
        invertida = core.CoreConfig(playbook_priority=(
            core.PLAYBOOK_TREND_BREAKOUT, core.PLAYBOOK_RANGE_REVERSION,
            core.PLAYBOOK_TREND_PULLBACK))
        self.assertEqual(core.arbitrate(decisoes, invertida)["selected"]["playbook"],
                         core.PLAYBOOK_TREND_BREAKOUT)

    def test_arbitragem_e_deterministica(self):
        decisoes = [
            self._fake(core.PLAYBOOK_RANGE_REVERSION, core.STATE_ELIGIBLE, "long"),
            self._fake(core.PLAYBOOK_TREND_BREAKOUT, core.STATE_ELIGIBLE, "long"),
            self._fake(core.PLAYBOOK_TREND_PULLBACK, core.STATE_ELIGIBLE, "long"),
        ]
        escolhas = {core.arbitrate(list(reversed(decisoes)))["selected"]["playbook"],
                    core.arbitrate(decisoes)["selected"]["playbook"]}
        self.assertEqual(escolhas, {core.PLAYBOOK_TREND_PULLBACK})

    def test_sem_elegivel(self):
        verdict = core.arbitrate([
            self._fake(core.PLAYBOOK_TREND_PULLBACK, core.STATE_INELIGIBLE, None)])
        self.assertEqual(verdict["reason_code"], core.NO_ELIGIBLE_PLAYBOOK)


class DecisaoCompleta(unittest.TestCase):
    def test_decisao_bull_completa(self):
        decision = core.decide(bull_pullback_state())
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)
        self.assertEqual(decision["playbook"], core.PLAYBOOK_TREND_PULLBACK)
        self.assertEqual(decision["playbook_version"], "TREND_PULLBACK_V1")
        self.assertFalse(decision["executable"])
        self.assertEqual(len(decision["considered"]), 3)
        self.assertEqual(decision["trigger_candle_ms"], T0 + 79 * BAR_MS)
        self.assertIn("validation", decision["trace"])

    def test_decisao_range_completa(self):
        decision = core.decide(range_state())
        self.assertEqual(decision["state"], core.STATE_ELIGIBLE)
        self.assertEqual(decision["playbook"], core.PLAYBOOK_RANGE_REVERSION)

    def test_mesma_oportunidade_gera_uma_identidade(self):
        """Snapshots distintos da MESMA vela produzem a mesma oportunidade."""
        primeiro = core.decide(bull_pullback_state())
        segundo = core.decide(bull_pullback_state(as_of_ms=T0 + 81 * BAR_MS))
        self.assertEqual(primeiro["opportunity_key"], segundo["opportunity_key"])
        outro_lado = core.opportunity_key(symbol="BTCUSDT", timeframe="1h",
                                          side="short", trigger_candle_ms=T0 + 79 * BAR_MS)
        self.assertNotEqual(primeiro["opportunity_key"], outro_lado)

    def test_vocabulario_de_motivos_fechado(self):
        estados = [bull_pullback_state(), bear_pullback_state(), breakout_state(),
                   range_state(), bull_pullback_state(adx=None),
                   bull_pullback_state(higher_timeframes=())]
        for state in estados:
            decision = core.decide(state)
            self.assertIn(decision["state"], core.DECISION_STATES)
            for code in decision["reason_codes"]:
                self.assertIn(code, core.REASON_CODES)
            for item in decision["considered"]:
                for code in item["reason_codes"]:
                    self.assertIn(code, core.REASON_CODES)


class AdaptadorDeSelecao(unittest.TestCase):
    def setUp(self):
        self.anterior = os.environ.pop(core.MODE_ENV, None)

    def tearDown(self):
        os.environ.pop(core.MODE_ENV, None)
        if self.anterior is not None:
            os.environ[core.MODE_ENV] = self.anterior

    def test_inativo_por_padrao(self):
        envelope = core.selection_adapter(core.decide(bull_pullback_state()))
        self.assertEqual(envelope["route"], "NONE")
        self.assertEqual(envelope["reason_code"], core.CORE_INACTIVE)
        self.assertIsNone(envelope["candidate"])
        self.assertFalse(envelope["executable"])

    def test_simulacao_entrega_candidato_nao_executavel(self):
        decision = core.decide(bull_pullback_state())
        envelope = core.selection_adapter(decision, mode="simulation")
        self.assertEqual(envelope["route"], "SIMULATION")
        self.assertEqual(envelope["live_route"], "UNAVAILABLE")
        self.assertFalse(envelope["executable"])
        candidato = envelope["candidate"]
        self.assertEqual(candidato["playbook"], core.PLAYBOOK_TREND_PULLBACK)
        for proibido in ("client_order_id", "quantity", "qty", "leverage", "order_type"):
            self.assertNotIn(proibido, candidato)

    def test_modo_live_nao_abre_rota(self):
        envelope = core.selection_adapter(core.decide(bull_pullback_state()), mode="live")
        self.assertEqual(envelope["route"], "NONE")
        self.assertEqual(envelope["mode"], core.MODE_INACTIVE)


class Manifest(unittest.TestCase):
    def test_config_congelada_com_procedencia(self):
        manifest = core.core_manifest()
        self.assertFalse(manifest["outcomes_consulted"])
        self.assertFalse(manifest["optimized"])
        self.assertFalse(manifest["approved_for_production"])
        parametros = manifest["parameters"]
        self.assertEqual(set(parametros), set(core.DEFAULT_CONFIG.as_dict()))
        for nome, spec in parametros.items():
            self.assertIn(spec["origin"], ("production_equivalent", "engineering_choice"), nome)
            if spec["origin"] == "production_equivalent":
                self.assertTrue(spec["source"], nome)
            else:
                self.assertTrue(spec["rationale"], nome)

    def test_hash_muda_com_a_config(self):
        base = core.CoreConfig().config_hash()
        self.assertEqual(base, core.CoreConfig().config_hash())
        self.assertNotEqual(base, core.CoreConfig(min_rr_tp2=2.5).config_hash())

    def test_config_invalida_e_recusada(self):
        for kwargs in ({"min_bars": 2}, {"adx_range_max": 40.0},
                       {"range_target_fraction": 1.5}, {"min_rr_tp2": 0.5},
                       {"playbook_priority": ("TREND_PULLBACK",)}):
            with self.assertRaises(ValueError, msg=kwargs):
                core.CoreConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
