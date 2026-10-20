"""Lote 02 §3 — coleta pré-seleção no ponto OFICIAL do scanner.

Tudo aqui passa pelo entrypoint real (`get_recommendations_via_vision`) com
`TradeSignal`/`Indicator`/`SignalDirection` REAIS; só a fonte de mercado é
mockada. Casos obrigatórios:

  • dois TFs válidos do mesmo símbolo: um vira best, os DOIS são observados;
  • veto macro `block_all`: nenhuma recomendação, candidatos existentes
    vetados e contabilizados;
  • retorno sem dado: não vira oportunidade (só cobertura);
  • modo inativo: zero trabalho observacional e lista/ordem do champion
    idênticas às do modo ligado.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from models.trade_signal import (ConfluenceScore, Indicator,      # noqa: E402
                                SignalDirection, TradeSignal)
from services import decision_observation_service as obs          # noqa: E402
from services import preselection_observation_service as pre      # noqa: E402
from services import recommendation_service as rs                 # noqa: E402

CANDLE_MS = 1_780_000_000_000


def sinal(*, timeframe: str, conf: float, symbol: str = "BTCUSDT",
          direction=SignalDirection.LONG, adx: float = 28.0) -> TradeSignal:
    """Sinal REAL do scanner (enum de direção e `Indicator` de verdade)."""
    sig = TradeSignal(
        symbol=symbol, timeframe=timeframe, direction=direction,
        trade_type="day_trade", confidence=0.7, entry=100.0, stop_loss=98.0,
        tp1=103.0, tp2=106.0, tp3=109.0, risk_reward=3.0, patterns=[],
        indicators=Indicator(adx=adx, atr=2.0, rsi=55.0, ema12=101.0,
                             ema26=99.0, volume_ratio=1.4),
        confluence=ConfluenceScore(total=conf, max_total=100, pct=conf,
                                   factors=[]),
        derivatives={"funding_rate_pct": 0.01}, spread_pct=0.03,
        timestamp=CANDLE_MS, signal_strength="strong")
    sig.data_freshness = {"candle": {"close_time_ms": CANDLE_MS},
                          "source": "server:test"}
    return sig


class FonteFalsa:
    """Única fronteira mockada: a fonte de mercado."""

    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.chamadas = 0

    async def fetch_top_volume_symbols(self, limit=None):
        self.chamadas += 1
        return list(self.symbols)[:limit or len(self.symbols)]


def rodar(*, modo: str, sinais, block_all: bool = False,
          blackout: bool = False, symbols=("BTCUSDT",)):
    """Executa o scanner REAL e devolve (recomendações, linhas, cobertura)."""
    fonte = FonteFalsa(symbols)
    pendentes_antes = set(obs._pending)
    pre.reset_coverage()

    async def analisar(_svc, symbol, tf):
        return sinais.get((symbol, tf))

    async def regime_status():
        return {"regime": "RISK_OFF" if block_all else "NORMAL",
                "block_all": block_all, "block_alt_longs": False,
                "downgrade_alt_longs": False, "downgrade_shorts": False,
                "reasons": ["teste"]}

    async def blackout_status():
        return {"active": blackout, "event": "CPI", "country": "US",
                "minutes_until_resume": 30}

    async def sem_cooldown(hours=6):
        return set()

    async def sem_learning():
        return {}

    from services import news_filter_service as nfs
    from services import regime_service as regime
    from services import snapshot_service as snaps
    from services import learning_service as learning
    with patch.dict(os.environ, {pre.MODE_ENV: modo}), \
            patch.object(rs, "_get_server_data_source",
                         return_value=(fonte, "test-source")), \
            patch.object(rs, "SCAN_TFS", ["1h", "4h"]), \
            patch.object(rs, "HIGH_TF_PATTERNS_ENABLED", False), \
            patch.object(rs, "_analyze_symbol_tf_server", side_effect=analisar), \
            patch.object(rs, "_attach_htf_ema_trend"), \
            patch.object(nfs, "get_blackout_status", side_effect=blackout_status), \
            patch.object(regime, "get_regime_status", side_effect=regime_status), \
            patch.object(snaps, "get_recently_stopped_symbols", side_effect=sem_cooldown), \
            patch.object(learning, "compute_auto_adjustments", side_effect=sem_learning):
        recs = asyncio.run(rs.get_recommendations_via_vision(top_n=len(symbols),
                                                             apply_guard=False))
    novas = [obs._pending[chave] for chave in obs._pending
             if chave not in pendentes_antes]
    for chave in list(obs._pending):
        if chave not in pendentes_antes:
            obs._pending.pop(chave, None)
    return recs, novas, pre.coverage_snapshot()


def payload(linha) -> dict:
    return (linha["frozen_config"] or {}).get("r09_pre_selection") or {}


class CapturaNoPontoOficial(unittest.TestCase):
    """O ponto de captura é ANTES da escolha de best e dos filtros."""

    @classmethod
    def setUpClass(cls):
        cls._dns = patch.object(socket, "getaddrinfo",
                                side_effect=AssertionError("DNS proibido"))
        cls._dns.start()
        cls.addClassCleanup(cls._dns.stop)

    def test_dois_tfs_validos_um_vira_best_os_dois_sao_observados(self):
        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=90.0),
                  ("BTCUSDT", "4h"): sinal(timeframe="4h", conf=78.0)}
        recs, linhas, cobertura = rodar(modo=pre.MODE_OBSERVE, sinais=sinais)
        self.assertEqual(len(recs), 1, "a seleção do champion continua uma só")
        self.assertEqual(len(linhas), 2, "os dois TFs avaliados viram observação")
        por_tf = {payload(l)["setup"]["timeframe"]: l for l in linhas}
        self.assertEqual(sorted(por_tf), ["1h", "4h"])
        vencedor = payload(por_tf[recs[0].timeframe])
        perdedor = payload(por_tf["4h" if recs[0].timeframe == "1h" else "1h"])
        self.assertEqual(vencedor["outcome"], pre.OUTCOME_ACCEPTED)
        self.assertEqual(perdedor["outcome"], pre.OUTCOME_VETOED)
        self.assertEqual(perdedor["funnel"]["first_blocker"], "SELECTION")
        self.assertEqual(perdedor["funnel"]["first_blocker_reason"],
                         "TIMEFRAME_NOT_SELECTED")
        # Bloco de avaliação: dois TFs avaliados, um escolhido.
        self.assertEqual(vencedor["evaluation"]["evaluated_count"], 2)
        self.assertTrue(vencedor["evaluation"]["is_selected_timeframe"])
        self.assertFalse(perdedor["evaluation"]["is_selected_timeframe"])
        self.assertEqual(vencedor["evaluation"]["selected_timeframe"],
                         recs[0].timeframe)
        # Etapas que NÃO rodaram para o perdedor não aparecem como PASSED.
        self.assertNotIn("MTF_REGIME", perdedor["funnel"]["blockers_observed"])
        self.assertIn("MTF_REGIME", perdedor["funnel"]["stages_not_evaluated"])
        self.assertEqual(cobertura["candidates_observed"], 2)

    def test_lado_e_features_sao_reais_e_ponto_no_tempo(self):
        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=85.0)}
        _recs, linhas, _cob = rodar(modo=pre.MODE_OBSERVE, sinais=sinais)
        dados = payload(linhas[0])
        # `SignalDirection.LONG` tem de virar "long" — não "signaldirection.long"
        # nem None (foi esse o defeito que a fixture SimpleNamespace mascarou).
        self.assertEqual(dados["setup"]["side"], "long")
        self.assertEqual(dados["schema_version"], pre.PRE_SCHEMA_VERSION_V2)
        features = dados["features"]
        self.assertEqual(features["atr"], 2.0)
        self.assertEqual(features["adx"], 28.0)
        self.assertEqual(features["volume_ratio"], 1.4)
        self.assertEqual(features["spread_pct"], 0.03)
        self.assertIsNotNone(features["score"])
        self.assertEqual(features["regime"], "NORMAL")
        # Ausência continua ausência: nada virou zero.
        self.assertIsNone(features["entry_distance_atr"])

    def test_features_de_estrutura_do_v3_ficam_declaradamente_ausentes(self):
        """LACUNA REAL declarada: o scan champion não calcula as features de
        estrutura/gatilho do Score V3. Elas ficam `None` (nunca 0) e derrubam a
        cobertura do modelo — por isso um estudo de SELEÇÃO sobre a captura do
        champion fica WAITING_DATA em vez de produzir decisão inventada."""
        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=85.0)}
        _recs, linhas, _cob = rodar(modo=pre.MODE_OBSERVE, sinais=sinais)
        features = payload(linhas[0])["features"]
        for campo in ("structure_quality", "level_distance_atr",
                      "trigger_body_ratio", "trigger_follow_through_atr"):
            self.assertIn(campo, features, campo)
            self.assertIsNone(features[campo], campo)
        # E o modelo real recusa decidir com essa cobertura.
        from services import score_v3_service as s3
        limpo = {chave: valor for chave, valor in features.items()
                 if isinstance(valor, (int, float))}
        self.assertEqual(s3.score(limpo, playbook="TREND_PULLBACK",
                                  side="long")["state"], s3.STATE_UNAVAILABLE)

    def test_veto_macro_bloqueia_recomendacao_mas_observa_candidatos(self):
        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=90.0),
                  ("BTCUSDT", "4h"): sinal(timeframe="4h", conf=78.0)}
        recs, linhas, cobertura = rodar(modo=pre.MODE_OBSERVE, sinais=sinais,
                                        block_all=True)
        self.assertEqual(recs, [], "veto macro não produz recomendação")
        self.assertEqual(len(linhas), 2, "candidatos existiam e foram vetados")
        for linha in linhas:
            dados = payload(linha)
            self.assertEqual(dados["outcome"], pre.OUTCOME_VETOED)
            self.assertEqual(dados["funnel"]["first_blocker"], "MTF_REGIME")
            self.assertEqual(dados["funnel"]["first_blocker_reason"],
                             "REGIME_BLOCK_ALL")
            self.assertEqual(dados["features"]["regime"], "RISK_OFF")
        self.assertIn(pre.COVERAGE_MACRO_BLOCK, cobertura["reasons"])

    def test_retorno_sem_dado_nao_vira_oportunidade(self):
        recs, linhas, cobertura = rodar(modo=pre.MODE_OBSERVE, sinais={})
        self.assertEqual(recs, [])
        self.assertEqual(linhas, [], "sem candidato não existe oportunidade")
        self.assertEqual(cobertura["reasons"].get(pre.COVERAGE_NO_CANDIDATE), 1)
        self.assertEqual(cobertura["candidates_observed"], 0)

    def test_blackout_registra_cobertura_sem_fabricar_nada(self):
        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=85.0)}
        recs, linhas, cobertura = rodar(modo=pre.MODE_OBSERVE, sinais=sinais,
                                        blackout=True)
        self.assertEqual(recs, [])
        self.assertEqual(linhas, [])
        self.assertEqual(cobertura["reasons"].get(pre.COVERAGE_BLACKOUT), 1)
        self.assertEqual(cobertura["last_state"], pre.COVERAGE_INCOMPLETE)

    def test_modo_inativo_nao_faz_trabalho_novo_e_preserva_o_champion(self):
        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=90.0),
                  ("BTCUSDT", "4h"): sinal(timeframe="4h", conf=78.0),
                  ("ETHUSDT", "1h"): sinal(symbol="ETHUSDT", timeframe="1h",
                                           conf=88.0),
                  ("ETHUSDT", "4h"): sinal(symbol="ETHUSDT", timeframe="4h",
                                           conf=80.0)}
        ligado, linhas_on, _cob_on = rodar(
            modo=pre.MODE_OBSERVE, sinais=sinais, symbols=("BTCUSDT", "ETHUSDT"))
        desligado, linhas_off, cobertura_off = rodar(
            modo=pre.MODE_INACTIVE, sinais=sinais, symbols=("BTCUSDT", "ETHUSDT"))
        # Paridade do champion: MESMA lista, MESMA ordem.
        self.assertEqual([(r.symbol, r.timeframe, r.tier, r.score) for r in ligado],
                         [(r.symbol, r.timeframe, r.tier, r.score) for r in desligado])
        self.assertTrue(linhas_on, "ligado observa")
        self.assertEqual(linhas_off, [], "desligado não constrói linha")
        self.assertEqual(cobertura_off["cycles"], 0,
                         "desligado não registra nem cobertura")

    def test_desligado_nao_pede_a_lista_de_avaliados(self):
        """Trabalho novo nem é solicitado: `evaluated_out` fica None."""
        pedidos = []
        real = rs._best_tf_for_symbol_server

        async def espiao(svc, symbol, evaluated_out=None):
            pedidos.append(evaluated_out)
            return await real(svc, symbol, evaluated_out=evaluated_out)

        sinais = {("BTCUSDT", "1h"): sinal(timeframe="1h", conf=85.0)}
        with patch.object(rs, "_best_tf_for_symbol_server", side_effect=espiao):
            rodar(modo=pre.MODE_INACTIVE, sinais=sinais)
        self.assertEqual(pedidos, [None])
        pedidos.clear()
        with patch.object(rs, "_best_tf_for_symbol_server", side_effect=espiao):
            rodar(modo=pre.MODE_OBSERVE, sinais=sinais)
        self.assertEqual(len(pedidos), 1)
        self.assertIsInstance(pedidos[0], list)


if __name__ == "__main__":
    unittest.main()
