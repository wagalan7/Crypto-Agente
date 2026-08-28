"""P05 — otimização de estratégia governada por evidência.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS (a suíte falha se o código
sob teste tentar sair para a rede, mesmo que capture a exceção). Nenhum acesso a
exchange, banco externo, Railway ou Vercel.
"""
from __future__ import annotations

import ast
import asyncio
import math
import os
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05 (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import strategy_evidence_service as p05  # noqa: E402


T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _raw(**kw) -> dict:
    """Linha CRUA (pré-normalização)."""
    base = {
        "dedupe_key": kw.pop("key", "k1"),
        "is_open": False,
        "realized_r": 1.0,
        "status": "won_tp2",
        "resolved_at": T0,
        "created_at": T0,
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "tier": "A",
        "direction": "long",
        "score": 60.0,
        "features": {},
    }
    base.update(kw)
    return base


def _rows(specs, *, start: datetime = T0, features=None) -> list:
    """Lista NORMALIZADA a partir de (r, status) — já ordenada no tempo."""
    raw = []
    for i, spec in enumerate(specs):
        r, status = spec if isinstance(spec, tuple) else (spec, "won_tp2" if spec > 0 else "lost")
        raw.append(_raw(key=f"k{i}", realized_r=r, status=status,
                        resolved_at=start + timedelta(hours=i),
                        features=dict(features or {})))
    rows, _dq = p05.normalize_outcomes(raw, source="TEST")
    return rows


# ════════════════════════════════════════════════════════════════════════════
#  DATASET
# ════════════════════════════════════════════════════════════════════════════
class DatasetTests(unittest.TestCase):
    def test_open_excluido_e_contabilizado(self):
        rows, dq = p05.normalize_outcomes(
            [_raw(key="a"), _raw(key="b", is_open=True, realized_r=None)], source="REAL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(dq["excluded_total"], 1)
        self.assertIn("aberto (não resolvido)", dq["excluded_by_reason"])

    def test_realized_r_none_nunca_vira_zero(self):
        rows, dq = p05.normalize_outcomes([_raw(key="a", realized_r=None)], source="REAL")
        self.assertEqual(rows, [])
        self.assertEqual(dq["excluded_by_reason"]["realized_r ausente"], 1)

    def test_nan_e_infinito_excluidos(self):
        rows, dq = p05.normalize_outcomes(
            [_raw(key="a", realized_r=float("nan")), _raw(key="b", realized_r=float("inf"))],
            source="REAL")
        self.assertEqual(rows, [])
        self.assertEqual(dq["excluded_by_reason"]["realized_r NaN/infinito"], 2)

    def test_dedupe_por_identidade(self):
        rows, dq = p05.normalize_outcomes([_raw(key="same"), _raw(key="same")], source="REAL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(dq["excluded_by_reason"]["duplicado"], 1)

    def test_duplicata_invalida_nao_elimina_valida(self):
        rows, dq = p05.normalize_outcomes(
            [_raw(key="same", realized_r=None), _raw(key="same", realized_r=1.0)],
            source="REAL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["realized_r"], 1.0)
        self.assertNotIn("duplicado", dq["excluded_by_reason"])

    def test_sem_timestamp_de_resolucao_excluido(self):
        rows, dq = p05.normalize_outcomes([_raw(key="a", resolved_at=None)], source="REAL")
        self.assertEqual(rows, [])
        self.assertIn("sem timestamp de resolução", dq["excluded_by_reason"])

    def test_timezone_naive_vira_utc_aware(self):
        rows, _ = p05.normalize_outcomes(
            [_raw(key="a", resolved_at=datetime(2026, 6, 1, 12, 0))], source="REAL")
        self.assertIsNotNone(rows[0]["resolved_at"].tzinfo)
        self.assertEqual(rows[0]["resolved_at"].utcoffset(), timedelta(0))

    def test_ordenacao_cronologica(self):
        raw = [_raw(key="b", resolved_at=T0 + timedelta(hours=5)),
               _raw(key="a", resolved_at=T0)]
        rows, _ = p05.normalize_outcomes(raw, source="REAL")
        self.assertEqual([r["dedupe_key"] for r in rows], ["a", "b"])

    def test_fontes_separadas_no_relatorio(self):
        _, dq_real = p05.normalize_outcomes([_raw(key="a")], source="REAL")
        _, dq_shadow = p05.normalize_outcomes([_raw(key="a")], source="SHADOW")
        _, dq_bt = p05.normalize_outcomes([_raw(key="a")], source="BACKTEST")
        self.assertEqual([dq_real["source"], dq_shadow["source"], dq_bt["source"]],
                         ["REAL", "SHADOW", "BACKTEST"])

    def test_loaders_usam_janela_correta(self):
        """REAL por closed_at; SHADOW por outcome_at — nunca opened_at/created_at."""
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        real = src.split("async def _load_real")[1].split("async def _load_shadow")[0]
        self.assertIn("RealTrade.closed_at >= since", real)
        self.assertNotIn("opened_at >= since", real)
        shadow = src.split("async def _load_shadow")[1].split("def _merged_features")[0]
        self.assertIn("RS.outcome_at >= since", shadow)
        self.assertIn("_not_fast_void()", shadow)      # void reaproveitado da calibração
        self.assertNotIn("created_at >= since", shadow)


# ════════════════════════════════════════════════════════════════════════════
#  MÉTRICAS
# ════════════════════════════════════════════════════════════════════════════
class MetricsTests(unittest.TestCase):
    def test_win_loss_breakeven_expired_separados(self):
        rows = _rows([(1.5, "won_tp2"), (-1.0, "lost"), (0.0, "closed_be"), (0.0, "expired")])
        m = p05.compute_evidence_metrics(rows)
        self.assertEqual((m["wins"], m["losses"], m["breakeven"], m["expired"]), (1, 1, 1, 1))

    def test_zero_nao_e_loss_e_expired_nao_e_win(self):
        self.assertEqual(p05.classify_outcome("closed_be", 0.0), "breakeven")
        self.assertEqual(p05.classify_outcome("expired", 0.0), "expired")
        self.assertEqual(p05.classify_outcome("expired", 1.0), "expired")

    def test_wilson_interval_conhecido(self):
        ci = p05.wilson_interval(50, 100)
        self.assertLess(ci["low_pct"], 50.0)
        self.assertGreater(ci["high_pct"], 50.0)
        self.assertIsNone(p05.wilson_interval(0, 0))

    def test_wilson_amostra_pequena_e_larga(self):
        small = p05.wilson_interval(2, 3)
        large = p05.wilson_interval(200, 300)
        self.assertGreater(small["high_pct"] - small["low_pct"],
                           large["high_pct"] - large["low_pct"])

    def test_expectancy_e_soma(self):
        m = p05.compute_evidence_metrics(_rows([1.0, -1.0, 2.0]))
        self.assertAlmostEqual(m["sum_r"], 2.0)
        self.assertAlmostEqual(m["expectancy_r"], 2.0 / 3, places=4)

    def test_bootstrap_deterministico(self):
        rows = _rows([1.0, -1.0, 2.0, 0.5, -0.5, 1.2])
        vals = [r["realized_r"] for r in rows]
        self.assertEqual(p05.bootstrap_mean_ci(vals), p05.bootstrap_mean_ci(vals))
        a, b = vals[:3], vals[3:]
        self.assertEqual(p05.bootstrap_delta_ci(a, b), p05.bootstrap_delta_ci(a, b))

    def test_bootstrap_pareado_deterministico(self):
        rows = _rows([1.0, -1.0, 2.0, 0.5], features={"chase_atr": 0.5})
        got = p05.bootstrap_paired_selection_delta_ci(
            rows, p05.discover_champion_config(), {"PROXIMITY_MAX_ATR": 1.5},
            ["proximity"], samples=100)
        self.assertEqual(got, p05.bootstrap_paired_selection_delta_ci(
            rows, p05.discover_champion_config(), {"PROXIMITY_MAX_ATR": 1.5},
            ["proximity"], samples=100))

    def test_bootstrap_amostra_insuficiente_retorna_none(self):
        self.assertIsNone(p05.bootstrap_mean_ci([1.0]))
        self.assertIsNone(p05.bootstrap_delta_ci([1.0], [2.0, 3.0]))

    def test_profit_factor(self):
        m = p05.compute_evidence_metrics(_rows([2.0, -1.0]))
        self.assertAlmostEqual(m["profit_factor"], 2.0)

    def test_profit_factor_sem_perdas_e_none_com_motivo(self):
        m = p05.compute_evidence_metrics(_rows([1.0, 2.0]))
        self.assertIsNone(m["profit_factor"])
        self.assertIn("profit_factor", m["unavailable"])

    def test_sharpe_nao_anualizado_e_none_quando_std_zero(self):
        m = p05.compute_evidence_metrics(_rows([1.0, 1.0, 1.0]))
        self.assertIsNone(m["sharpe_per_trade"])
        self.assertIn("sharpe_per_trade", m["unavailable"])
        self.assertIn("NÃO anualizado", m["sharpe_note"])

    def test_drawdown_r(self):
        self.assertAlmostEqual(p05.max_drawdown_r([1.0, -2.0, 0.5]), 2.0)
        self.assertAlmostEqual(p05.max_drawdown_r([1.0, 1.0]), 0.0)

    def test_pior_sequencia_de_losses(self):
        rows = _rows([(-1.0, "lost"), (-1.0, "lost"), (1.0, "won_tp2"), (-1.0, "lost")])
        self.assertEqual(p05.worst_loss_streak(rows), 2)

    def test_breakeven_nao_quebra_streak_de_forma_silenciosa(self):
        rows = _rows([(-1.0, "lost"), (0.0, "closed_be"), (-1.0, "lost")])
        self.assertEqual(p05.worst_loss_streak(rows), 1)

    def test_downside_deviation_ignora_ganhos(self):
        m = p05.compute_evidence_metrics(_rows([1.0, 1.0]))
        self.assertAlmostEqual(m["downside_deviation_r"], 0.0)

    def test_fees_sem_double_count(self):
        """pnl_usd já é LÍQUIDO — a soma não pode descontar fee de novo."""
        raw = [_raw(key="a", pnl_usd=10.0, entry_fee=1.0, exit_fee=2.0)]
        rows, _ = p05.normalize_outcomes(raw, source="REAL")
        m = p05.compute_evidence_metrics(rows)
        self.assertAlmostEqual(m["net_pnl_usd"], 10.0)
        self.assertAlmostEqual(m["entry_fees_usd"], 1.0)
        self.assertAlmostEqual(m["exit_fees_usd"], 2.0)
        self.assertIn("LÍQUIDO", m["net_pnl_note"])

    def test_amostra_vazia_devolve_none_com_motivo(self):
        m = p05.compute_evidence_metrics([])
        self.assertEqual(m["count"], 0)
        for k in ("win_rate_pct", "expectancy_r", "profit_factor", "sharpe_per_trade"):
            self.assertIsNone(m[k])
        self.assertIn("all", m["unavailable"])

    def test_slippage_cobertura(self):
        raw = [_raw(key="a", entry_slippage_pct=0.1), _raw(key="b")]
        rows, _ = p05.normalize_outcomes(raw, source="REAL")
        m = p05.compute_evidence_metrics(rows)
        self.assertAlmostEqual(m["avg_slippage_pct"], 0.1)
        self.assertAlmostEqual(m["slippage_coverage_pct"], 50.0)

    def test_trades_por_dia_none_em_janela_curta(self):
        m = p05.compute_evidence_metrics(_rows([1.0, 1.0]))
        self.assertIsNone(m["trades_per_day"])
        self.assertIn("trades_per_day", m["unavailable"])

    def test_tp_hit_none_quando_fonte_nao_informa(self):
        m = p05.compute_evidence_metrics(_rows([1.0, -1.0]))
        self.assertIsNone(m["tp1_hit_rate_pct"])
        self.assertIn("tp1_hit_rate_pct", m["unavailable"])


# ════════════════════════════════════════════════════════════════════════════
#  SEGMENTOS
# ════════════════════════════════════════════════════════════════════════════
class SegmentTests(unittest.TestCase):
    def _mixed(self):
        raw = []
        for i in range(12):
            raw.append(_raw(
                key=f"s{i}",
                realized_r=1.0 if i % 2 == 0 else -1.0,
                status="won_tp2" if i % 2 == 0 else "lost",
                resolved_at=T0 + timedelta(hours=i),
                tier="A" if i < 6 else "B",
                timeframe="1h" if i % 3 else "4h",
                direction="long" if i % 2 == 0 else "short",
                score=60.0 if i < 6 else 45.0,
                features={"hour_utc": i, "regime": "NORMAL", "atr_pct": 1.5,
                          "patterns": ["engulfing", "pinbar"] if i % 2 == 0 else []},
            ))
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        return rows

    def test_eixos_presentes(self):
        seg = p05.segment_rows(self._mixed())
        for axis in ("by_tier", "by_timeframe", "by_direction", "by_session_utc",
                     "by_regime", "by_atr_band", "by_score_bin", "by_pattern",
                     "by_tier_timeframe", "by_base", "by_day_of_week"):
            self.assertIn(axis, seg)

    def test_padroes_sobrepostos_sao_atribuicao(self):
        seg = p05.segment_rows(self._mixed())
        keys = {it["key"] for it in seg["by_pattern"]["items"]}
        self.assertEqual(keys, {"engulfing", "pinbar"})     # mesmo trade em 2 padrões
        self.assertIn("atribuição, não causalidade", seg["_note"])

    def test_confiabilidade_por_amostra(self):
        self.assertEqual(p05.reliability_label(5), p05.RELIABILITY_INSUFFICIENT)
        self.assertEqual(p05.reliability_label(15), p05.RELIABILITY_EARLY)
        self.assertEqual(p05.reliability_label(50), p05.RELIABILITY_USABLE)
        self.assertEqual(p05.reliability_label(150), p05.RELIABILITY_STRONG)

    def test_segmento_pequeno_nao_lidera_por_win_rate(self):
        """n=2 com 100% de acerto não pode ficar acima de um segmento forte."""
        raw = [_raw(key=f"big{i}", realized_r=0.4, status="won_tp2",
                    resolved_at=T0 + timedelta(hours=i), tier="A") for i in range(120)]
        raw += [_raw(key=f"tiny{i}", realized_r=5.0, status="won_tp2",
                     resolved_at=T0 + timedelta(hours=200 + i), tier="C") for i in range(2)]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        items = p05.segment_rows(rows)["by_tier"]["items"]
        self.assertEqual(items[0]["key"], "A")
        self.assertEqual(items[0]["reliability"], p05.RELIABILITY_STRONG)

    def test_feature_ausente_contabilizada(self):
        rows = _rows([1.0, -1.0])          # sem 'regime'
        seg = p05.segment_rows(rows)
        self.assertEqual(seg["by_regime"]["missing_feature"], 2)

    def test_feature_coverage(self):
        raw = [_raw(key="a", features={"chase_atr": 0.5}), _raw(key="b", features={})]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        cov = p05.feature_coverage(rows, ["chase_atr"])
        self.assertEqual(cov["chase_atr"]["coverage_pct"], 50.0)

    def test_atr_band_e_sessao(self):
        self.assertEqual(p05._atr_band(0.3), "<0.5%")
        self.assertEqual(p05._atr_band(3.5), ">=3%")
        self.assertIsNone(p05._atr_band(None))
        self.assertEqual(p05._session_utc(3), "asia")
        self.assertEqual(p05._session_utc(10), "europe")
        self.assertIsNone(p05._session_utc(None))


# ════════════════════════════════════════════════════════════════════════════
#  FUNIL / EVENTOS P04
# ════════════════════════════════════════════════════════════════════════════
class FunnelTests(unittest.TestCase):
    def test_funil_nao_afirma_candidatos_unicos(self):
        src = (BACKEND / "services/assertiveness_service.py").read_text()
        funnel = src.split("async def _funnel")[1].split("async def _per_coin_scorecard")[0]
        self.assertIn("gate_events", funnel)
        self.assertIn("candidates_estimated", funnel)
        self.assertIn("is_estimate", funnel)
        self.assertIn("NÃO é o número de candidatos ÚNICOS", funnel)

    def test_gate_events_documenta_que_sao_eventos(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def _load_gate_events")[1].split("_FEATURE_NAMES")[0]
        self.assertIn("não são oportunidades únicas", block)
        self.assertIn("não somar com executados", block)
        self.assertIn("P04A_entry_revalidation", block)
        self.assertIn("P04B_depth_vwap", block)
        self.assertIn("P04C_data_freshness", block)

    def test_bloqueio_nao_e_lucro_perdido(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        self.assertIn("bloqueio não é 'lucro perdido'", src)


# ════════════════════════════════════════════════════════════════════════════
#  CANDIDATOS
# ════════════════════════════════════════════════════════════════════════════
class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()

    def test_um_knob_por_candidato(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"SCORE_MIN": 60, "PROXIMITY_MAX_ATR": 1.2})

    def test_config_vazia_rejeitada(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {})

    def test_knob_fora_da_allowlist_rejeitado(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"FOO_BAR": 1})

    def test_knob_de_safety_ou_live_rejeitado(self):
        for knob in ("LIVE_SIZE_MULT", "KILL_MAX_DAILY_LOSS_PCT", "P04C_MAX_TICKER_AGE_MS",
                     "MAKER_ENTRY_ENABLED", "PORTFOLIO_GUARD_ENABLED",
                     "P04B_MAX_MARKET_SLIPPAGE_PCT", "LEARNING_AUTO_ADJUST"):
            with self.assertRaises(p05.CandidateValidationError, msg=knob):
                p05.validate_candidate_config(self.champ, {knob: 1})

    def test_nan_e_infinito_rejeitados(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(p05.CandidateValidationError):
                p05.validate_candidate_config(self.champ, {"PROXIMITY_MAX_ATR": bad})

    def test_limites_numericos(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"PROXIMITY_MAX_ATR": 99.0})
        with self.assertRaises(p05.CandidateValidationError):   # delta conservador
            p05.validate_candidate_config(self.champ, {"SCORE_MIN": self.champ["SCORE_MIN"] + 50})

    def test_boolean_e_string_invalidos(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"QUALITY_EDGE_GATE_ENABLED": "sim"})
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"MTF_ALIGNED_MODE": "turbo"})
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"MTF_ALIGNED_MODE": ""})

    def test_valor_igual_ao_champion_nao_e_candidato(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(self.champ, {"PROXIMITY_MAX_ATR": self.champ["PROXIMITY_MAX_ATR"]})

    def test_candidato_valido(self):
        cfg = p05.validate_candidate_config(self.champ, {"QUALITY_EDGE_GATE_ENABLED": True})
        self.assertEqual(cfg, {"QUALITY_EDGE_GATE_ENABLED": True})

    def test_maximo_12_candidatos_e_um_knob_cada(self):
        rows = _rows([1.0] * 80, features={"edge_score": 1.0, "mtf_aligned": 2,
                                           "chase_atr": 0.5, "struct_chase_atr": 2.0})
        accepted, _rej = p05.generate_candidates(self.champ, rows)
        self.assertLessEqual(len(accepted), 12)
        self.assertLessEqual(len(accepted), p05.P05_MAX_CANDIDATES)
        for cand in accepted:
            self.assertEqual(len(cand["config"]), 1)
            self.assertIn(cand["objective"], p05.OBJECTIVES)

    def test_sem_grid_search_combinatorio(self):
        rows = _rows([1.0] * 80, features={"edge_score": 1.0, "mtf_aligned": 2,
                                           "chase_atr": 0.5, "struct_chase_atr": 2.0})
        accepted, _ = p05.generate_candidates(self.champ, rows)
        knobs = [tuple(sorted(c["config"])) for c in accepted]
        self.assertTrue(all(len(k) == 1 for k in knobs))

    def test_feature_ausente_bloqueia_candidato(self):
        rows = _rows([1.0] * 80)                  # nenhuma feature persistida
        accepted, rejected = p05.generate_candidates(self.champ, rows)
        reasons = {r["reason"] for r in rejected}
        self.assertIn("MISSING_FEATURE_COVERAGE", reasons)
        for cand in accepted:                     # só knobs sem feature externa
            self.assertIn(cand["knob"], ("SCORE_MIN", "TF_MIN_TIER"))

    def test_cobertura_insuficiente_bloqueia(self):
        feats = [{"chase_atr": 0.5}] * 5 + [{}] * 95
        raw = [_raw(key=f"c{i}", resolved_at=T0 + timedelta(hours=i), features=f)
               for i, f in enumerate(feats)]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        self.assertLess(p05.component_coverage(rows, "proximity"), p05.P05_MIN_FEATURE_COVERAGE_PCT)
        _acc, rejected = p05.generate_candidates(self.champ, rows)
        prox = [r for r in rejected if r["knob"] == "PROXIMITY_MAX_ATR"]
        self.assertTrue(prox and prox[0]["reason"] == "MISSING_FEATURE_COVERAGE")

    def test_candidate_hash_estavel_e_ordenado(self):
        h1 = p05.canonical_hash({"a": 1, "b": 2})
        h2 = p05.canonical_hash({"b": 2, "a": 1})
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        self.assertNotEqual(h1, p05.canonical_hash({"a": 1, "b": 3}))

    def test_dataset_fingerprint_estavel(self):
        rows = _rows([1.0, -1.0, 0.5])
        self.assertEqual(p05.dataset_fingerprint(rows),
                         p05.dataset_fingerprint(list(reversed(rows))))
        self.assertNotEqual(p05.dataset_fingerprint(rows),
                            p05.dataset_fingerprint(_rows([1.0, -1.0, 0.6])))

    def test_experiment_key_identidade_logica(self):
        k1 = p05.build_experiment_key("c" * 64, "d" * 64, T0)
        k2 = p05.build_experiment_key("c" * 64, "d" * 64, T0)
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, p05.build_experiment_key("c" * 64, "d" * 64, T0 + timedelta(days=1)))

    def test_champion_descoberto_com_defaults_reais(self):
        champ = p05.discover_champion_config()
        self.assertIn(champ["SCORE_MIN"], (57.0, 72.0))
        self.assertEqual(champ["PROXIMITY_MAX_ATR"], 1.0)
        self.assertEqual(champ["MTF_ALIGNED_MODE"], "boost")
        self.assertFalse(champ["QUALITY_EDGE_GATE_ENABLED"])
        self.assertEqual(champ["TF_MIN_TIER"], "15m:A+,1h:A,4h:B")


# ════════════════════════════════════════════════════════════════════════════
#  CONTRAFACTUAL / ELEGIBILIDADE
# ════════════════════════════════════════════════════════════════════════════
class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()

    def test_unknown_preservado_quando_feature_ausente(self):
        row = _raw(features={})
        self.assertIsNone(p05.eligibility(row, self.champ, ["proximity"]))

    def test_score_min_bloqueia_e_libera(self):
        low = _raw(score=self.champ["SCORE_MIN"] - 5)
        high = _raw(score=self.champ["SCORE_MIN"] + 5)
        self.assertFalse(p05.eligibility(low, self.champ, ["score_min"]))
        self.assertTrue(p05.eligibility(high, self.champ, ["score_min"]))

    def test_score_ausente_e_unknown(self):
        self.assertIsNone(p05.eligibility(_raw(score=None), self.champ, ["score_min"]))

    def test_proximity_gate(self):
        cfg = dict(self.champ)
        self.assertTrue(p05.eligibility(_raw(features={"chase_atr": 0.5}), cfg, ["proximity"]))
        self.assertFalse(p05.eligibility(_raw(features={"chase_atr": 2.0}), cfg, ["proximity"]))

    def test_proximity_igual_bloqueia_e_retest_isenta(self):
        cfg = dict(self.champ, BREAKOUT_LANE_ENABLED=False)
        self.assertFalse(p05.eligibility(
            _raw(features={"chase_atr": cfg["PROXIMITY_MAX_ATR"]}), cfg, ["proximity"]))
        struct = dict(cfg, STRUCT_CHASE_GATE_ENABLED=True, STRUCT_CHASE_MAX_ATR=2.0)
        self.assertTrue(p05.eligibility(
            _raw(features={"struct_chase_atr": 2.0, "retest_armed": True}),
            struct, ["struct_chase"]))

    def test_tf_min_tier(self):
        cfg = dict(self.champ)
        self.assertTrue(p05.eligibility(_raw(tier="A", timeframe="1h"), cfg, ["tf_min_tier"]))
        self.assertFalse(p05.eligibility(_raw(tier="B", timeframe="1h"), cfg, ["tf_min_tier"]))
        self.assertTrue(p05.eligibility(_raw(tier="B", timeframe="4h"), cfg, ["tf_min_tier"]))

    def test_mtf_required_exige_alinhamento(self):
        cfg = dict(self.champ, MTF_ALIGNED_MODE="required", MTF_ALIGNED_MIN_COUNT=2)
        self.assertTrue(p05.eligibility(_raw(features={"mtf_aligned": 2}), cfg, ["mtf"]))
        self.assertFalse(p05.eligibility(_raw(features={"mtf_aligned": 1}), cfg, ["mtf"]))
        self.assertIsNone(p05.eligibility(_raw(features={}), cfg, ["mtf"]))

    def test_quality_edge_so_na_banda_marginal(self):
        smin = self.champ["SCORE_MIN"]
        cfg = dict(self.champ, QUALITY_EDGE_GATE_ENABLED=True, QUALITY_EDGE_MARGIN=6.0)
        marginal = _raw(score=smin + 1, features={"edge_score": 0})
        forte = _raw(score=smin + 20, features={"edge_score": 0})
        self.assertFalse(p05.eligibility(marginal, cfg, ["quality_edge"]))
        self.assertTrue(p05.eligibility(forte, cfg, ["quality_edge"]))

    def test_edge_tags_nao_substituem_edge_score(self):
        cfg = dict(self.champ, QUALITY_EDGE_GATE_ENABLED=True,
                   QUALITY_EDGE_MARGIN=6.0, QUALITY_EDGE_MIN=1)
        row = _raw(score=cfg["SCORE_MIN"] + 1,
                   features={"edge_score": 0, "edge_tags": ["forte"]})
        self.assertFalse(p05.eligibility(row, cfg, ["quality_edge"]))

    def test_comparacao_simetrica_exclui_unknown_dos_dois_lados(self):
        rows = _rows([1.0, -1.0, 1.0])              # sem chase_atr
        cmp = p05.compare_configs(rows, self.champ,
                                  {"PROXIMITY_MAX_ATR": 1.5}, ["proximity"])
        self.assertEqual(cmp["evaluable"], 0)
        self.assertEqual(cmp["unknown_excluded"], 3)

    def test_candidato_que_seleciona_subconjunto_fica_explicito(self):
        raw = [_raw(key=f"p{i}", resolved_at=T0 + timedelta(hours=i),
                    realized_r=1.0 if i % 2 else -1.0,
                    status="won_tp2" if i % 2 else "lost",
                    features={"chase_atr": 0.2 if i % 2 else 1.5}) for i in range(20)]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        champ = dict(self.champ, PROXIMITY_MAX_ATR=2.0)
        cmp = p05.compare_configs(rows, champ, {"PROXIMITY_MAX_ATR": 1.0}, ["proximity"])
        self.assertTrue(cmp["selects_subset"])
        self.assertEqual(cmp["avoided_ops"], 10)
        self.assertGreater(cmp["candidate"]["expectancy_r"], cmp["champion"]["expectancy_r"])


# ════════════════════════════════════════════════════════════════════════════
#  VALIDAÇÃO TEMPORAL
# ════════════════════════════════════════════════════════════════════════════
class TemporalTests(unittest.TestCase):
    def test_amostra_insuficiente(self):
        split = p05.temporal_split(_rows([1.0] * 10))
        self.assertFalse(split["ok"])
        self.assertEqual(split["reason"], "INSUFFICIENT_DATA")

    def test_folds_por_tamanho(self):
        self.assertEqual(p05.temporal_split(_rows([1.0] * 80))["folds"], 4)
        self.assertEqual(p05.temporal_split(_rows([1.0] * 150))["folds"], 6)

    def test_split_sem_overlap_e_cronologico(self):
        split = p05.temporal_split(_rows([1.0] * 100))
        train, valid, test = split["train"], split["validation"], split["test"]
        self.assertEqual(len(train) + len(valid) + len(test), 100)
        ids = lambda rows: {r["dedupe_key"] for r in rows}          # noqa: E731
        self.assertFalse(ids(train) & ids(valid))
        self.assertFalse(ids(valid) & ids(test))
        self.assertFalse(ids(train) & ids(test))
        self.assertLess(train[-1]["resolved_at"], valid[0]["resolved_at"])
        self.assertLess(valid[-1]["resolved_at"], test[0]["resolved_at"])

    def test_sem_leakage_folds_treino_no_passado(self):
        rows = _rows([1.0] * 120)
        for fold in p05.walkforward_folds(rows, 4):
            self.assertLess(fold["train"][-1]["resolved_at"], fold["test"][0]["resolved_at"])

    def test_teste_final_nao_ajusta_threshold(self):
        """O gate lê o teste apenas para CONFIRMAR — a seleção é na validação."""
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        gate = src.split("def evaluate_offline_gate")[1].split("def evaluate_shadow_gate")[0]
        self.assertIn("cand_v", gate)
        self.assertNotIn("threshold =", gate)
        split_fn = src.split("def temporal_split")[1].split("def walkforward_folds")[0]
        self.assertIn("INTOCADO", split_fn)


# ════════════════════════════════════════════════════════════════════════════
#  GATES OFFLINE
# ════════════════════════════════════════════════════════════════════════════
def _mk(count, expectancy, *, pf=2.0, dd=1.0, sum_r=None, ci_low=0.1):
    return {"count": count, "expectancy_r": expectancy, "profit_factor": pf,
            "max_drawdown_r": dd, "sum_r": sum_r if sum_r is not None else expectancy * count,
            "expectancy_ci": {"low": ci_low, "high": ci_low + 0.3}}


def _cmp(champ, cand, *, delta_low=0.1, added_exp=0.2):
    return {"champion": champ, "candidate": cand,
            "delta_expectancy_ci": {"low": delta_low, "high": delta_low + 0.3, "point": delta_low},
            "added_expectancy_r": added_exp, "avoided_expectancy_r": -0.5}


class OfflineGateTests(unittest.TestCase):
    def test_loss_reduction_aprova(self):
        v = _cmp(_mk(100, 0.10, pf=1.2, dd=5.0), _mk(80, 0.30, pf=1.8, dd=3.0))
        t = _cmp(_mk(50, 0.10), _mk(40, 0.28))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_OFFLINE_VALIDATED)

    def test_loss_reduction_rejeita_ci_do_delta_cruzando_zero(self):
        v = _cmp(_mk(100, 0.10), _mk(80, 0.30), delta_low=-0.05)
        t = _cmp(_mk(50, 0.10), _mk(40, 0.28))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("ci_delta_expectancy_acima_de_zero", res["detail"])

    def test_loss_reduction_rejeita_queda_de_operacoes(self):
        v = _cmp(_mk(100, 0.10, pf=1.2, dd=5.0), _mk(50, 0.40, pf=1.8, dd=3.0))  # 50% < 70%
        t = _cmp(_mk(50, 0.10), _mk(40, 0.30))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("operacoes_min_70pct", res["detail"])

    def test_loss_reduction_rejeita_drawdown_pior(self):
        v = _cmp(_mk(100, 0.10, pf=1.2, dd=3.0), _mk(80, 0.30, pf=1.8, dd=9.0))
        t = _cmp(_mk(50, 0.10), _mk(40, 0.28))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("drawdown_nao_pior", res["detail"])

    def test_more_operations_aprova(self):
        v = _cmp(_mk(100, 0.20, pf=1.5, dd=4.0, sum_r=20.0),
                 _mk(130, 0.18, pf=1.4, dd=4.2, sum_r=23.4))
        t = _cmp(_mk(50, 0.20), _mk(60, 0.20), added_exp=0.1)
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_MORE_OPERATIONS, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_OFFLINE_VALIDATED)

    def test_more_operations_rejeita_poucas_operacoes(self):
        v = _cmp(_mk(100, 0.20, sum_r=20.0), _mk(105, 0.19, sum_r=19.95))   # 105% < 110%
        t = _cmp(_mk(50, 0.20), _mk(55, 0.18))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_MORE_OPERATIONS, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("operacoes_min_110pct", res["detail"])

    def test_more_operations_rejeita_adicionais_negativas(self):
        v = _cmp(_mk(100, 0.20, sum_r=20.0), _mk(130, 0.17, sum_r=22.1), added_exp=-0.3)
        t = _cmp(_mk(50, 0.20), _mk(60, 0.15))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_MORE_OPERATIONS, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("operacoes_adicionais_positivas", res["detail"])

    def test_amostra_oos_insuficiente_e_honesta(self):
        v = _cmp(_mk(100, 0.10), _mk(80, 0.30))
        t = _cmp(_mk(20, 0.10), _mk(5, 0.28))          # < P05_MIN_OOS_RESOLVED
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_INSUFFICIENT)
        self.assertEqual(res["reason_code"], "OOS_SAMPLE_TOO_SMALL")

    def test_teste_final_negativo_rejeita(self):
        v = _cmp(_mk(100, 0.10, pf=1.2, dd=5.0), _mk(80, 0.30, pf=1.8, dd=3.0))
        t = _cmp(_mk(50, 0.10), _mk(40, -0.10))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)

    def test_teste_final_pf_ou_drawdown_ruim_rejeita(self):
        v = _cmp(_mk(100, 0.10, pf=1.2, dd=5.0), _mk(80, 0.30, pf=1.8, dd=3.0))
        t = _cmp(_mk(50, 0.10, pf=2.0, dd=1.0),
                 _mk(40, 0.30, pf=0.1, dd=100.0))
        self.assertEqual(
            p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)["verdict"],
            p05.STATUS_REJECTED)

    def test_nenhum_vencedor_forcado(self):
        """Candidato pior em tudo é REJEITADO — não existe 'melhor dos piores'."""
        v = _cmp(_mk(100, 0.30, pf=2.0, dd=2.0), _mk(60, -0.20, pf=0.5, dd=9.0), delta_low=-0.9)
        t = _cmp(_mk(50, 0.30), _mk(40, -0.20))
        res = p05.evaluate_offline_gate(p05.OBJECTIVE_LOSS_REDUCTION, v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)


# ════════════════════════════════════════════════════════════════════════════
#  SHADOW + DECISÃO
# ════════════════════════════════════════════════════════════════════════════
class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()

    def test_anotacao_idempotente_e_por_experimento(self):
        row = _raw(created_at=T0, features={"chase_atr": 0.5})
        a = p05.build_experiment_annotation(row, self.champ, {"PROXIMITY_MAX_ATR": 1.5},
                                            experiment_key="k1", candidate_hash="h1",
                                            active=["proximity"])
        b = p05.build_experiment_annotation(row, self.champ, {"PROXIMITY_MAX_ATR": 1.5},
                                            experiment_key="k1", candidate_hash="h1",
                                            active=["proximity"])
        self.assertEqual(a, b)
        self.assertEqual(a["evaluated_at"], T0.isoformat())
        self.assertEqual(a["champion_hash"], p05.canonical_hash(self.champ))

    def test_anotacao_prospectiva_respeita_inicio_e_nao_sobrescreve(self):
        context = {
            "experiment_key": "e1", "candidate_hash": "c1",
            "champion_hash": p05.canonical_hash(self.champ), "champion": self.champ,
            "candidate_config": {"PROXIMITY_MAX_ATR": 1.5},
            "active_components": ["proximity"], "shadow_started_at": T0,
        }
        before = p05.annotate_snapshot_features(
            {"chase_atr": 0.5}, _raw(created_at=T0 - timedelta(seconds=1)), context)
        self.assertNotIn("p05_experiment", before)
        after = p05.annotate_snapshot_features(
            {"chase_atr": 0.5}, _raw(created_at=T0), context)
        self.assertEqual(after["p05_experiment"]["experiment_key"], "e1")
        other = {"p05_experiment": {"experiment_key": "outro"}}
        self.assertEqual(p05.annotate_snapshot_features(other, _raw(created_at=T0), context), other)

    def test_unknown_nunca_vira_blocked_ou_eligible(self):
        ann = p05.build_experiment_annotation(_raw(features={}), self.champ,
                                              {"PROXIMITY_MAX_ATR": 1.5},
                                              experiment_key="k", candidate_hash="h",
                                              active=["proximity"])
        self.assertEqual(ann["challenger_status"], p05.CHALLENGER_UNKNOWN)
        self.assertIsNone(ann["challenger_eligible"])
        self.assertIn("chase_atr", ann["missing_features"])

    def test_mesmo_snapshot_avalia_champion_e_challenger(self):
        row = _raw(features={"chase_atr": 1.4})
        ann = p05.build_experiment_annotation(row, self.champ, {"PROXIMITY_MAX_ATR": 1.5},
                                              experiment_key="k", candidate_hash="h",
                                              active=["proximity"])
        self.assertFalse(ann["champion_eligible"])       # champion 1.0 bloqueia
        self.assertTrue(ann["challenger_eligible"])      # challenger 1.5 aceita
        self.assertEqual(ann["challenger_status"], p05.CHALLENGER_ELIGIBLE)

    def test_shadow_gate_aguarda_amostra(self):
        res = p05.evaluate_shadow_gate({"resolved": 5, "observed_days": 3,
                                        "coverage_pct": 100.0, "candidate": {"expectancy_r": 0.5},
                                        "operational_incident": False, "safety_relaxed": False})
        self.assertEqual(res["verdict"], p05.STATUS_INSUFFICIENT)
        self.assertEqual(res["reason_code"], "AGUARDANDO_AMOSTRA")

    def test_shadow_gate_aprova(self):
        res = p05.evaluate_shadow_gate({
            "resolved": 40, "challenger_resolved": 40, "observed_days": 20, "coverage_pct": 95.0,
            "candidate": {"expectancy_r": 0.3}, "objective_still_met": True,
            "operational_incident": False, "safety_relaxed": False})
        self.assertEqual(res["verdict"], p05.STATUS_ELIGIBLE)

    def test_shadow_gate_rejeita_cobertura_baixa(self):
        res = p05.evaluate_shadow_gate({
            "resolved": 40, "challenger_resolved": 40, "observed_days": 20, "coverage_pct": 50.0,
            "candidate": {"expectancy_r": 0.3}, "objective_still_met": True,
            "operational_incident": False, "safety_relaxed": False})
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("cobertura_minima", res["detail"])

    def test_shadow_gate_rejeita_relaxamento_de_safety(self):
        res = p05.evaluate_shadow_gate({
            "resolved": 40, "challenger_resolved": 40, "observed_days": 20, "coverage_pct": 95.0,
            "candidate": {"expectancy_r": 0.3}, "objective_still_met": True,
            "operational_incident": False, "safety_relaxed": True})
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("sem_relaxamento_p01_p04", res["detail"])

    def test_shadow_gate_fail_closed_em_incidente_ou_safety_unknown(self):
        base = {"resolved": 40, "observed_days": 20, "coverage_pct": 95.0,
                "candidate": {"expectancy_r": 0.3}, "objective_still_met": True}
        for incident, safety in ((True, False), (None, False), (False, None)):
            got = p05.evaluate_shadow_gate(dict(
                base, operational_incident=incident, safety_relaxed=safety))
            self.assertEqual(got["reason_code"], "SHADOW_SAFETY_NOT_PROVEN")

    def test_shadow_producao_aceita_somente_anotacao_exata(self):
        raw = []
        for i, key in enumerate(("ok", "wrong")):
            ann = {"experiment_key": key, "candidate_hash": "c", "champion_hash": "h",
                   "champion_eligible": True, "challenger_eligible": True}
            raw.append(_raw(key=key, created_at=T0, resolved_at=T0 + timedelta(days=15, hours=i),
                            features={"p05_experiment": ann}))
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        m = p05.compute_shadow_metrics(
            rows, self.champ, {}, p05.OBJECTIVE_MORE_OPERATIONS, [], started_at=T0,
            experiment_key="ok", candidate_hash="c", champion_hash="h",
            require_annotations=True, operational_incident=False, safety_relaxed=False)
        self.assertEqual(m["annotation_coverage_pct"], 50.0)
        self.assertEqual(m["unknown_excluded"], 1)

    def test_transicoes_validas(self):
        self.assertTrue(p05.can_transition(p05.STATUS_DRAFT, p05.STATUS_OFFLINE_VALIDATED))
        self.assertTrue(p05.can_transition(p05.STATUS_OFFLINE_VALIDATED, p05.STATUS_SHADOW))
        self.assertTrue(p05.can_transition(p05.STATUS_SHADOW, p05.STATUS_ELIGIBLE))
        self.assertTrue(p05.can_transition(p05.STATUS_SHADOW, p05.STATUS_REJECTED))

    def test_saltos_proibidos(self):
        self.assertFalse(p05.can_transition(p05.STATUS_DRAFT, p05.STATUS_SHADOW))
        self.assertFalse(p05.can_transition(p05.STATUS_DRAFT, p05.STATUS_ELIGIBLE))
        self.assertFalse(p05.can_transition(p05.STATUS_OFFLINE_VALIDATED, p05.STATUS_ELIGIBLE))

    def test_experimento_decidido_nao_reabre(self):
        for final in (p05.STATUS_REJECTED, p05.STATUS_ELIGIBLE, p05.STATUS_INSUFFICIENT):
            for target in (p05.STATUS_DRAFT, p05.STATUS_SHADOW, p05.STATUS_OFFLINE_VALIDATED):
                self.assertFalse(p05.can_transition(final, target))

    def test_shadow_metrics_avalia_os_dois_no_mesmo_dado(self):
        raw = [_raw(key=f"s{i}", resolved_at=T0 + timedelta(hours=i),
                    realized_r=1.0 if i % 2 else -1.0,
                    status="won_tp2" if i % 2 else "lost",
                    features={"chase_atr": 0.2 if i % 2 else 1.4}) for i in range(20)]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        m = p05.compute_shadow_metrics(rows, self.champ, {"PROXIMITY_MAX_ATR": 1.5},
                                       p05.OBJECTIVE_MORE_OPERATIONS, ["proximity"],
                                       started_at=T0)
        self.assertEqual(m["resolved"], 20)
        self.assertEqual(m["coverage_pct"], 100.0)
        self.assertEqual(m["added_ops"], 10)          # challenger aceita os chase 1.4
        self.assertIn("CONTRAFACTUAL", m["note"])

    def test_apenas_um_shadow_ativo_no_codigo(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def start_shadow")[1].split("async def evaluate_shadow")[0]
        self.assertIn("já existe um experimento SHADOW ativo", block)
        self.assertIn("with_for_update", block)
        self.assertIn("idempotent", block)
        self.assertIn("_P05_SHADOW_LOCK_KEY", block)
        self.assertIn("IntegrityError", block)

    def test_promotion_plan_nao_ativa_nada(self):
        plan = p05.build_promotion_plan(self.champ, {"PROXIMITY_MAX_ATR": 1.25}, {})
        self.assertIn("env_atual", plan)
        self.assertIn("env_proposta", plan)
        self.assertIn("valores_rollback", plan)
        self.assertIn("plano_canario", plan)
        self.assertIn("NÃO foi ativado", plan["aviso"])


class ShadowFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_flags_off_bloqueiam_antes_do_db(self):
        with patch.object(p05, "P05_CHALLENGER_SHADOW_ENABLED", False):
            start = await p05.start_shadow(1)
            evaluate = await p05.evaluate_shadow(1)
        self.assertEqual(start["reason_code"], "P05_SHADOW_DISABLED")
        self.assertTrue(evaluate["blocked"])


class SafetyGovernanceTests(unittest.TestCase):
    def test_fingerprint_deterministico_e_detecta_drift(self):
        with patch.dict(os.environ, {"LIVE_SIZE_MULT": "0.25"}, clear=False):
            a = p05.safety_guard()
            b = p05.safety_guard()
            self.assertEqual(a, b)
        with patch.dict(os.environ, {"LIVE_SIZE_MULT": "0.50"}, clear=False):
            c = p05.safety_guard()
        self.assertNotEqual(a["fingerprint"], c["fingerprint"])

    def test_snapshot_cobre_travas_criticas(self):
        snap = p05.safety_config_snapshot()
        expected = {"LIVE_SIZE_MULT", "MAKER_ENTRY_ENABLED", "TF_UPGRADE_ENABLED",
                    "PYRAMIDING_ENABLED", "P04A_ENTRY_REVALIDATION_ENABLED",
                    "P04B_MARKET_REVALIDATION_ENABLED", "P04B_MAKER_FALLBACK_ENABLED",
                    "P04C_DATA_FRESHNESS_ENABLED"}
        self.assertTrue(expected.issubset(snap))


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p05._DIAG_CACHE.clear()

    async def test_cache_singleflight(self):
        calls = 0
        async def fake(days):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"days": days}
        with patch.object(p05, "build_diagnosis", side_effect=fake):
            got = await asyncio.gather(*(p05.get_cached_diagnosis(30) for _ in range(5)))
        self.assertEqual(calls, 1)
        self.assertTrue(all(x == {"days": 30} for x in got))


# ════════════════════════════════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════════════════════════════════
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "main.py").read_text()
        cls.tree = ast.parse(cls.src)
        cls.routes = {}
        for node in ast.walk(cls.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                        if dec.args and isinstance(dec.args[0], ast.Constant):
                            path = dec.args[0].value
                            if isinstance(path, str) and "/p05" in path:
                                cls.routes[(dec.func.attr, path)] = node

    def test_endpoints_esperados_existem(self):
        expected = {
            ("get", "/api/strategy/p05/status"),
            ("get", "/api/strategy/p05/experiments"),
            ("get", "/api/strategy/p05/experiments/{exp_id}"),
            ("post", "/api/strategy/p05/evaluate"),
            ("post", "/api/strategy/p05/experiments/{exp_id}/start-shadow"),
            ("post", "/api/strategy/p05/experiments/{exp_id}/evaluate-shadow"),
        }
        self.assertTrue(expected.issubset(set(self.routes)), f"faltando: {expected - set(self.routes)}")

    def test_sem_endpoint_de_promote_apply_ou_live(self):
        for method, path in self.routes:
            for proibido in ("promote", "apply", "activate", "execute", "retry-now", "change-size"):
                self.assertNotIn(proibido, path.lower(), f"endpoint proibido: {path}")

    def test_post_exige_auth_admin(self):
        for (method, path), node in self.routes.items():
            if method != "post":
                continue
            body = ast.get_source_segment(self.src, node) or ""
            self.assertIn("_check_admin_token", body, f"POST sem auth: {path}")
            self.assertIn("x_admin_token", body, f"POST sem header admin: {path}")

    def test_get_nao_exige_auth(self):
        node = self.routes[("get", "/api/strategy/p05/status")]
        body = ast.get_source_segment(self.src, node) or ""
        self.assertNotIn("_check_admin_token", body)

    def test_clamp_de_parametros(self):
        status = ast.get_source_segment(self.src, self.routes[("get", "/api/strategy/p05/status")])
        self.assertIn("max(7, min(int(days), 365))", status)
        lista = ast.get_source_segment(self.src, self.routes[("get", "/api/strategy/p05/experiments")])
        self.assertIn("max(1, min(int(limit), 100))", lista)
        self.assertIn("max(0, int(offset))", lista)

    def test_listagem_sem_payload_gigante_por_default(self):
        from models.strategy_experiment import StrategyExperiment
        exp = StrategyExperiment(
            experiment_key="k", champion_hash="a", candidate_hash="b",
            status=p05.STATUS_DRAFT, objective=p05.OBJECTIVE_LOSS_REDUCTION,
            candidate_config={"SCORE_MIN": 59}, offline_metrics={"huge": [1] * 1000},
            shadow_metrics={"huge": [1] * 1000}, decision={"verdict": "REJECTED"})
        light = exp.to_dict(full=False)
        self.assertNotIn("offline_metrics", light)
        self.assertNotIn("shadow_metrics", light)
        self.assertIn("decision_summary", light)
        self.assertIn("offline_metrics", exp.to_dict(full=True))

    def test_status_invalido_rejeitado_na_listagem(self):
        node = self.routes[("get", "/api/strategy/p05/experiments")]
        body = ast.get_source_segment(self.src, node) or ""
        self.assertIn("status inválido", body)

    def test_assertiveness_ganhou_secao_p05(self):
        src = (BACKEND / "services/assertiveness_service.py").read_text()
        self.assertIn('"p05": p05', src)
        self.assertIn("async def _p05_section", src)

    def test_shadow_bloqueado_nao_retorna_200(self):
        for path in ("/api/strategy/p05/experiments/{exp_id}/start-shadow",
                     "/api/strategy/p05/experiments/{exp_id}/evaluate-shadow"):
            body = ast.get_source_segment(self.src, self.routes[("post", path)]) or ""
            self.assertIn('res.get("blocked")', body)
            self.assertIn("HTTPException(status_code=409", body)


# ════════════════════════════════════════════════════════════════════════════
#  ARQUITETURA / SEGURANÇA
# ════════════════════════════════════════════════════════════════════════════
class ArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "services/strategy_evidence_service.py").read_text()

    def test_nao_importa_sdk_de_exchange(self):
        for proibido in ("import ccxt", "binance_signed_service", "bybit_signed_service",
                         "import httpx", "import aiohttp", "import requests"):
            self.assertNotIn(proibido, self.src, f"import proibido: {proibido}")

    def test_nao_emite_nem_cancela_ordem(self):
        for proibido in ("place_order", "cancel_order", "place_protection_orders",
                         "cancel_algo_order", "_signed_request", "close_position"):
            self.assertNotIn(proibido, self.src, f"chamada proibida: {proibido}")

    def test_nao_altera_p01_a_p04_nem_defaults_do_live(self):
        """Safety pode ser lida para fingerprint; nunca escrita ou promovida."""
        for proibido in ("LIVE_SIZE_MULT", "MAKER_ENTRY_ENABLED", "TF_UPGRADE_ENABLED",
                         "PYRAMIDING_ENABLED", "KILL_SWITCH"):
            self.assertNotIn(proibido, p05.KNOB_ALLOWLIST, f"knob proibido na allowlist: {proibido}")

    def test_safety_e_somente_leitura(self):
        self.assertIn("def safety_config_snapshot", self.src)
        self.assertNotIn("os.environ[", self.src)

    def test_service_nao_escreve_env(self):
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, "setenv")
                if node.func.attr == "putenv":
                    self.fail("service não pode escrever env")
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Attribute)
                            and isinstance(target.value.value, ast.Name)
                            and target.value.value.id == "os"
                            and target.value.attr == "environ"):
                        self.fail("service não pode atribuir os.environ")

    def test_defaults_nao_ligam_shadow_challenger(self):
        self.assertIn('_env_bool("P05_CHALLENGER_SHADOW_ENABLED", "false")', self.src)

    def test_allowlist_nao_contem_knob_de_safety(self):
        for knob in p05.KNOB_ALLOWLIST:
            for token in ("LIVE", "SIZE", "QTY", "LEVERAGE", "KILL", "P04", "MAKER"):
                self.assertNotIn(token, knob.upper(), f"knob suspeito na allowlist: {knob}")

    def test_modelo_nao_altera_tabelas_existentes(self):
        src = (BACKEND / "models/strategy_experiment.py").read_text()
        self.assertIn('__tablename__ = "strategy_experiments"', src)
        for outra in ("recommendation_snapshots", "real_trades", "backtest_trades"):
            self.assertNotIn(f'__tablename__ = "{outra}"', src)

    def test_modelo_registrado_no_init_db(self):
        self.assertIn("from models import strategy_experiment",
                      (BACKEND / "db.py").read_text())

    def test_unico_shadow_garantido_no_banco(self):
        model = (BACKEND / "models/strategy_experiment.py").read_text()
        db = (BACKEND / "db.py").read_text()
        self.assertIn('"uq_strategy_exp_single_shadow"', model)
        self.assertIn("unique=True", model)
        self.assertIn("status = 'SHADOW'", model)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_exp_single_shadow", db)

    def test_snapshot_usa_namespace_versionado_sem_alterar_schema(self):
        src = (BACKEND / "services/snapshot_service.py").read_text()
        self.assertIn('"p05_context": _p05_context(', src)
        self.assertIn("def _p05_context(", src)
        block = src.split("def _p05_context(")[1].split("\nasync def ")[0]
        self.assertIn('"schema_version": 1', block)
        self.assertIn("nunca inventa valor", block)

    def test_pipeline_de_snapshot_aplica_anotacao_prospectiva(self):
        src = (BACKEND / "services/snapshot_service.py").read_text()
        save = src.split("async def save_recommendations")[1]
        self.assertIn("_load_p05_shadow_context(session)", save)
        self.assertIn("_annotate_p05_features(", save)
        self.assertLess(save.index("_annotate_p05_features("),
                        save.index("RecommendationSnapshot("))

    def test_p05_context_nao_inventa_valor_ausente(self):
        src = (BACKEND / "services/snapshot_service.py").read_text()
        block = src.split("def _p05_context(")[1].split("\nasync def ")[0]
        self.assertNotIn("or 0", block)                 # não substitui ausência por zero
        self.assertNotIn("or False", block)
        self.assertNotIn('or "binance"', block)
        self.assertNotIn('or "scan"', block)
        self.assertNotIn('verdict.get("code")', block)

    def test_read_paths_usam_cache(self):
        self.assertIn("await get_cached_diagnosis(days)", self.src)
        assertion = (BACKEND / "services/assertiveness_service.py").read_text()
        self.assertIn("await p05.get_cached_diagnosis(days)", assertion)
        self.assertIn('"P05_DIAG_CACHE_TTL_S"', self.src)

    def test_mae_mfe_declarado_indisponivel(self):
        self.assertIn('"status": "UNAVAILABLE"', self.src)
        self.assertIn("criaria afirmação falsa", self.src)


# ════════════════════════════════════════════════════════════════════════════
#  FRONTEND
# ════════════════════════════════════════════════════════════════════════════
class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = BACKEND.parent / "frontend/src/components/AssertivenessPanel.tsx"
        cls.src = cls.path.read_text()

    def test_secoes_p05_presentes(self):
        for marcador in ("Qualidade da evidência", "Onde ganha", "Champion",
                         "Challenger", "Bloqueios P04"):
            self.assertIn(marcador, self.src, f"seção ausente: {marcador}")

    def test_sem_botao_de_promocao(self):
        low = self.src.lower()
        for proibido in ("promover", "aplicar no live", "activate-live", "/promote", "/apply"):
            self.assertNotIn(proibido, low, f"ação proibida no front: {proibido}")

    def test_nao_chama_endpoint_inexistente_de_promocao(self):
        self.assertNotIn("start-shadow", self.src)      # painel é read-only
        self.assertNotIn("evaluate-shadow", self.src)

    def test_estados_loading_error_empty(self):
        self.assertIn("loading", self.src)
        self.assertIn("error", self.src)
        self.assertIn("p05", self.src)

    def test_nao_chama_lucro_perdido(self):
        self.assertNotIn("lucro perdido", self.src.lower())

    def test_eligible_permanece_visivel_apos_shadow(self):
        self.assertIn("p05?.shadow_experiment ?? p05?.eligible_experiment", self.src)
        backend = (BACKEND / "services/assertiveness_service.py").read_text()
        self.assertIn("eligible.to_dict(full=True)", backend)


# ════════════════════════════════════════════════════════════════════════════
#  P05.1 — candidatos CONTEXTUAIS (ANALYTICS_ONLY)
# ════════════════════════════════════════════════════════════════════════════
def _ctx_rule(axis="regime", value="ALT_DANGER", action="BLOCK", **kw) -> dict:
    rule = {"schema_version": 1, "axis": axis, "value": value, "action": action}
    if action == "SCORE_DELTA":
        rule["score_delta"] = -2.0
    rule.update(kw)
    return rule


class ContextRuleValidatorTests(unittest.TestCase):
    def test_regra_valida_block(self):
        cfg = p05.validate_contextual_candidate_config({"CONTEXT_RULE": _ctx_rule()})
        self.assertEqual(cfg["CONTEXT_RULE"]["action"], "BLOCK")
        self.assertNotIn("score_delta", cfg["CONTEXT_RULE"])

    def test_regra_valida_score_delta(self):
        cfg = p05.validate_contextual_candidate_config(
            {"CONTEXT_RULE": _ctx_rule(axis="entry_zone_type", value="limit_pullback",
                                       action="SCORE_DELTA")})
        self.assertEqual(cfg["CONTEXT_RULE"]["score_delta"], -2.0)

    def test_eixo_invalido(self):
        for axis in ("symbol", "base", "pattern", "direction", "hour_utc",
                     "day_of_week", "score_bin", "atr_band", "tier"):
            with self.assertRaises(p05.CandidateValidationError, msg=axis):
                p05.validate_context_rule(_ctx_rule(axis=axis))

    def test_valor_ausente_ou_vazio(self):
        for value in (None, "", "   ", 5):
            with self.assertRaises(p05.CandidateValidationError, msg=repr(value)):
                p05.validate_context_rule(_ctx_rule(value=value))

    def test_valor_nao_pode_ser_marcador_unknown(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_context_rule(_ctx_rule(value=p05.CONTEXT_UNKNOWN))

    def test_acao_invalida(self):
        for action in ("SIZE_DELTA", "ALLOW", "", None, "block"):
            with self.assertRaises(p05.CandidateValidationError, msg=repr(action)):
                p05.validate_context_rule(_ctx_rule(action=action))

    def test_block_nao_aceita_score_delta(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_context_rule(_ctx_rule(action="BLOCK", score_delta=-2.0))

    def test_score_delta_somente_menos_dois(self):
        for bad in (-1.0, -3.0, 2.0, 0.0):
            with self.assertRaises(p05.CandidateValidationError, msg=str(bad)):
                p05.validate_context_rule(
                    _ctx_rule(action="SCORE_DELTA", score_delta=bad))

    def test_score_delta_nan_infinito_e_bool(self):
        for bad in (float("nan"), float("inf"), True):
            with self.assertRaises(p05.CandidateValidationError, msg=repr(bad)):
                p05.validate_context_rule(
                    _ctx_rule(action="SCORE_DELTA", score_delta=bad))

    def test_score_delta_exige_score_delta(self):
        rule = {"schema_version": 1, "axis": "regime", "value": "NORMAL",
                "action": "SCORE_DELTA"}
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_context_rule(rule)

    def test_schema_version_obrigatoria(self):
        for bad in (None, 0, 2, "1", True):
            rule = _ctx_rule()
            rule["schema_version"] = bad
            with self.assertRaises(p05.CandidateValidationError, msg=repr(bad)):
                p05.validate_context_rule(rule)

    def test_campos_proibidos(self):
        for extra in ("size_mult", "leverage", "stop", "tp1", "enabled", "symbol"):
            rule = _ctx_rule()
            rule[extra] = 1
            with self.assertRaises(p05.CandidateValidationError, msg=extra):
                p05.validate_context_rule(rule)

    def test_dois_knobs_rejeitado(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_contextual_candidate_config(
                {"CONTEXT_RULE": _ctx_rule(), "SCORE_MIN": 55})

    def test_config_vazia_ou_sem_context_rule(self):
        for cfg in ({}, {"SCORE_MIN": 55}, None, []):
            with self.assertRaises(p05.CandidateValidationError, msg=repr(cfg)):
                p05.validate_contextual_candidate_config(cfg)

    def test_context_rule_fora_da_allowlist_global(self):
        """CONTEXT_RULE não pode entrar pelo validador de knob global do P05."""
        self.assertNotIn("CONTEXT_RULE", p05.KNOB_ALLOWLIST)
        champ = p05.discover_champion_config()
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config(champ, {"CONTEXT_RULE": _ctx_rule()})

    def test_hash_inclui_schema_completo(self):
        a = p05.canonical_hash({"CONTEXT_RULE": _ctx_rule(value="ALT_DANGER")})
        b = p05.canonical_hash({"CONTEXT_RULE": _ctx_rule(value="NORMAL")})
        c = p05.canonical_hash({"CONTEXT_RULE": _ctx_rule(axis="entry_zone_type",
                                                          value="ALT_DANGER")})
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, p05.canonical_hash({"CONTEXT_RULE": _ctx_rule()}))


class ContextValueTests(unittest.TestCase):
    def test_regime_presente(self):
        row = _raw(features={"regime": "ALT_DANGER"})
        self.assertEqual(p05.context_value(row, "regime"), "ALT_DANGER")

    def test_entry_zone_type_presente(self):
        row = _raw(features={"entry_zone_type": "limit_pullback"})
        self.assertEqual(p05.context_value(row, "entry_zone_type"), "limit_pullback")

    def test_feature_ausente_e_unknown(self):
        self.assertEqual(p05.context_value(_raw(features={}), "regime"),
                         p05.CONTEXT_UNKNOWN)
        self.assertEqual(p05.context_value(_raw(features={"regime": None}), "regime"),
                         p05.CONTEXT_UNKNOWN)
        self.assertEqual(p05.context_value(_raw(features={"regime": "  "}), "regime"),
                         p05.CONTEXT_UNKNOWN)

    def test_eixo_nao_permitido_e_unknown(self):
        row = _raw(features={"symbol": "BTC"}, symbol="BTC/USDT:USDT")
        self.assertEqual(p05.context_value(row, "symbol"), p05.CONTEXT_UNKNOWN)

    def test_axis_coverage(self):
        raw = [_raw(key="a", features={"regime": "NORMAL"}), _raw(key="b", features={})]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        self.assertEqual(p05.axis_coverage(rows, "regime"), 50.0)
        self.assertIsNone(p05.axis_coverage([], "regime"))


class ContextualEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()
        self.smin = self.champ["SCORE_MIN"]

    def _row(self, *, score=None, regime="ALT_DANGER", **feats):
        f = {"regime": regime}
        f.update(feats)
        return _raw(score=self.smin + 5 if score is None else score, features=f)

    def test_block_bloqueia_somente_contexto_correspondente(self):
        rule = _ctx_rule(value="ALT_DANGER")
        hit = self._row(regime="ALT_DANGER")
        miss = self._row(regime="NORMAL")
        self.assertFalse(p05.contextual_eligibility(hit, self.champ, rule, ["score_min"]))
        self.assertTrue(p05.contextual_eligibility(miss, self.champ, rule, ["score_min"]))

    def test_block_nao_altera_veredito_dos_demais_contextos(self):
        rule = _ctx_rule(value="ALT_DANGER")
        for regime in ("NORMAL", "RISK_OFF", "BTC_DOMINANT"):
            row = self._row(regime=regime, score=self.smin - 10)
            self.assertEqual(p05.contextual_eligibility(row, self.champ, rule, ["score_min"]),
                             p05.eligibility(row, self.champ, ["score_min"]))

    def test_unknown_nao_vira_blocked(self):
        rule = _ctx_rule(value="ALT_DANGER")
        row = _raw(score=self.smin + 5, features={})
        self.assertIsNone(p05.contextual_eligibility(row, self.champ, rule, ["score_min"]))

    def test_champion_unknown_permanece_unknown(self):
        rule = _ctx_rule(value="ALT_DANGER")
        row = _raw(score=None, features={"regime": "ALT_DANGER"})
        self.assertIsNone(p05.contextual_eligibility(row, self.champ, rule, ["score_min"]))

    def test_score_delta_aplica_menos_dois_somente_no_contexto(self):
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        # score 1 ponto abaixo do piso: entra só com o delta, e só no contexto.
        inside = self._row(regime="NORMAL", score=self.smin - 1)
        outside = self._row(regime="ALT_DANGER", score=self.smin - 1)
        self.assertTrue(p05.contextual_eligibility(inside, self.champ, rule, ["score_min"]))
        self.assertFalse(p05.contextual_eligibility(outside, self.champ, rule, ["score_min"]))

    def test_score_delta_nao_libera_alem_de_dois_pontos(self):
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        row = self._row(regime="NORMAL", score=self.smin - 3)
        self.assertFalse(p05.contextual_eligibility(row, self.champ, rule, ["score_min"]))

    def test_score_delta_usa_score_efetivo_com_adjusters(self):
        """O piso é comparado com o score AJUSTADO, igual ao executor."""
        champ = dict(self.champ, SCORE_ADJUSTERS_ENABLED=True, SCORE_ADJUSTER_CAP=20.0)
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        # atr_pct < 1.0 dá +6 no score efetivo → sozinho já passa sem o delta.
        boosted = _raw(score=self.smin - 5, features={"regime": "NORMAL", "atr_pct": 0.5})
        self.assertIsNotNone(p05._execution_score(boosted, champ))
        self.assertGreater(p05._execution_score(boosted, champ), boosted["score"])
        self.assertTrue(p05.contextual_eligibility(boosted, champ, rule, ["score_min"]))
        # atr_pct > 3 dá -8 → nem com o delta alcança o piso.
        damped = _raw(score=self.smin - 1, features={"regime": "NORMAL", "atr_pct": 5.0})
        self.assertLess(p05._execution_score(damped, champ), damped["score"])
        self.assertFalse(p05.contextual_eligibility(damped, champ, rule, ["score_min"]))

    def test_score_delta_nao_reativa_linha_barrada_por_outro_gate(self):
        """R:R / P(TP1) / liquidez chegam pelo bot_verdict; o delta não os libera."""
        champ = dict(self.champ)
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        row = _raw(score=self.smin - 1, features={
            "regime": "NORMAL", "bot_verdict_ok": False,
            "bot_verdict_blocked_by": "rr-gate"})
        active = ["score_min", "base_quality"]
        self.assertFalse(p05.eligibility(row, champ, active))
        self.assertFalse(p05.contextual_eligibility(row, champ, rule, active))

    def test_score_delta_nao_libera_proximity_nem_atr(self):
        champ = dict(self.champ, PROXIMITY_GATE_ENABLED=True, PROXIMITY_MAX_ATR=1.0,
                     ATR_GATE_ENABLED=True, ATR_BLOCK_THRESHOLD=3.0)
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        chased = _raw(score=self.smin - 1, features={
            "regime": "NORMAL", "chase_atr": 2.0, "atr_pct": 1.5})
        self.assertFalse(p05.contextual_eligibility(
            chased, champ, rule, ["score_min", "proximity", "atr_gate"]))
        volatile = _raw(score=self.smin - 1, features={
            "regime": "NORMAL", "chase_atr": 0.1, "atr_pct": 9.0})
        self.assertFalse(p05.contextual_eligibility(
            volatile, champ, rule, ["score_min", "proximity", "atr_gate"]))

    def test_score_delta_com_outro_gate_unknown_permanece_unknown(self):
        champ = dict(self.champ, PROXIMITY_GATE_ENABLED=True)
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        row = _raw(score=self.smin - 1, features={"regime": "NORMAL"})  # sem chase_atr
        self.assertIsNone(p05.contextual_eligibility(
            row, champ, rule, ["score_min", "proximity"]))

    def test_score_delta_sem_componente_de_score_e_unknown(self):
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        row = self._row(regime="NORMAL", score=self.smin - 1)
        self.assertIsNone(p05.contextual_eligibility(row, self.champ, rule, ["tf_min_tier"]))

    def test_score_delta_mantem_quem_ja_passava(self):
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        row = self._row(regime="NORMAL", score=self.smin + 10)
        self.assertTrue(p05.contextual_eligibility(row, self.champ, rule, ["score_min"]))

    def test_eligibility_original_intacta(self):
        """A `eligibility` do P05 global não muda de comportamento."""
        row = self._row(regime="ALT_DANGER", score=self.smin + 5)
        self.assertTrue(p05.eligibility(row, self.champ, ["score_min"]))
        rule = _ctx_rule(value="ALT_DANGER")
        p05.contextual_eligibility(row, self.champ, rule, ["score_min"])
        self.assertTrue(p05.eligibility(row, self.champ, ["score_min"]))


class ContextualCompareTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()
        self.smin = self.champ["SCORE_MIN"]

    def _mixed(self, n=40):
        """Metade em ALT_DANGER (perdedora), metade em NORMAL (vencedora)."""
        raw = []
        for i in range(n):
            alt = i % 2 == 0
            raw.append(_raw(
                key=f"c{i}",
                resolved_at=T0 + timedelta(hours=i),
                created_at=T0 + timedelta(hours=i),
                realized_r=-1.0 if alt else 1.0,
                status="lost" if alt else "won_tp2",
                score=self.smin + 5,
                features={"regime": "ALT_DANGER" if alt else "NORMAL"}))
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        return rows

    def test_block_evita_contexto_negativo_e_registra_evitadas(self):
        rows = self._mixed()
        cmp_ = p05.compare_contextual(rows, self.champ, _ctx_rule(value="ALT_DANGER"),
                                      ["score_min"])
        self.assertEqual(cmp_["avoided_ops"], 20)
        self.assertEqual(cmp_["added_ops"], 0)
        self.assertEqual(cmp_["affected_ops"], 20)
        self.assertLess(cmp_["avoided_expectancy_r"], 0)
        self.assertGreater(cmp_["candidate"]["expectancy_r"],
                           cmp_["champion"]["expectancy_r"])
        self.assertTrue(cmp_["selects_subset"])

    def test_unknown_excluido_dos_dois_lados(self):
        rows = self._mixed(20) + _rows([1.0, -1.0])     # 2 linhas sem regime
        cmp_ = p05.compare_contextual(rows, self.champ, _ctx_rule(value="ALT_DANGER"),
                                      ["score_min"])
        self.assertEqual(cmp_["unknown_excluded"], 2)
        self.assertEqual(cmp_["evaluable"], 20)

    def test_delta_e_pareado(self):
        cmp_ = p05.compare_contextual(self._mixed(), self.champ,
                                      _ctx_rule(value="ALT_DANGER"), ["score_min"])
        self.assertEqual(cmp_["delta_expectancy_ci"]["method"],
                         "paired-annotation-bootstrap")

    def test_ic_das_evitadas_e_das_adicionais(self):
        cmp_ = p05.compare_contextual(self._mixed(), self.champ,
                                      _ctx_rule(value="ALT_DANGER"), ["score_min"])
        self.assertIsNotNone(cmp_["avoided_expectancy_ci"])
        self.assertLess(cmp_["avoided_expectancy_ci"]["high"], 0)
        self.assertIsNone(cmp_["added_expectancy_ci"])       # BLOCK não adiciona

    def test_score_delta_adiciona_operacoes(self):
        raw = []
        for i in range(30):
            raw.append(_raw(key=f"s{i}", resolved_at=T0 + timedelta(hours=i),
                            created_at=T0 + timedelta(hours=i),
                            realized_r=1.0, status="won_tp2",
                            score=self.smin - 1,           # abaixo do piso do champion
                            features={"regime": "NORMAL"}))
        for i in range(30):
            raw.append(_raw(key=f"t{i}", resolved_at=T0 + timedelta(hours=100 + i),
                            created_at=T0 + timedelta(hours=100 + i),
                            realized_r=0.5, status="won_tp2",
                            score=self.smin + 5, features={"regime": "NORMAL"}))
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        rule = _ctx_rule(axis="regime", value="NORMAL", action="SCORE_DELTA")
        cmp_ = p05.compare_contextual(rows, self.champ, rule, ["score_min"])
        self.assertEqual(cmp_["champion"]["count"], 30)
        self.assertEqual(cmp_["candidate"]["count"], 60)
        self.assertEqual(cmp_["added_ops"], 30)
        self.assertEqual(cmp_["avoided_ops"], 0)


class ContextualGenerationTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()

    def _train(self, *, n_bad=40, n_good=40):
        raw = []
        for i in range(n_bad):
            raw.append(_raw(key=f"bad{i}", resolved_at=T0 + timedelta(hours=i),
                            created_at=T0 + timedelta(hours=i),
                            realized_r=-1.0, status="lost",
                            features={"regime": "ALT_DANGER",
                                      "entry_zone_type": "breakout"}))
        for i in range(n_good):
            raw.append(_raw(key=f"good{i}", resolved_at=T0 + timedelta(hours=200 + i),
                            created_at=T0 + timedelta(hours=200 + i),
                            realized_r=1.0, status="won_tp2",
                            features={"regime": "NORMAL",
                                      "entry_zone_type": "limit_pullback"}))
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        return rows

    def test_gera_block_para_contexto_negativo(self):
        accepted, _ = p05.generate_contextual_candidates(self.champ, self._train())
        blocks = [c for c in accepted if c["action"] == "BLOCK"]
        self.assertTrue(blocks)
        self.assertTrue(all(c["objective"] == p05.OBJECTIVE_LOSS_REDUCTION for c in blocks))
        self.assertIn("ALT_DANGER", {c["value"] for c in blocks})

    def test_gera_score_delta_para_contexto_positivo(self):
        accepted, _ = p05.generate_contextual_candidates(self.champ, self._train())
        deltas = [c for c in accepted if c["action"] == "SCORE_DELTA"]
        self.assertTrue(deltas)
        self.assertTrue(all(c["objective"] == p05.OBJECTIVE_MORE_OPERATIONS for c in deltas))
        self.assertTrue(all(c["rule"]["score_delta"] == -2.0 for c in deltas))

    def test_maximo_8_e_4_por_objetivo(self):
        accepted, _ = p05.generate_contextual_candidates(self.champ, self._train())
        self.assertLessEqual(len(accepted), 8)
        self.assertLessEqual(len(accepted), p05.P05_MAX_CANDIDATES)
        for objective in p05.OBJECTIVES:
            self.assertLessEqual(
                sum(1 for c in accepted if c["objective"] == objective), 4)

    def test_um_contexto_por_candidato_sem_grid_search(self):
        accepted, _ = p05.generate_contextual_candidates(self.champ, self._train())
        for cand in accepted:
            self.assertEqual(set(cand["config"]), {"CONTEXT_RULE"})
            rule = cand["config"]["CONTEXT_RULE"]
            self.assertIn(rule["axis"], p05.P051_CONTEXT_AXES)
            self.assertIsInstance(rule["value"], str)

    def test_ordem_deterministica(self):
        train = self._train()
        a = p05.generate_contextual_candidates(self.champ, train)[0]
        b = p05.generate_contextual_candidates(self.champ, train)[0]
        self.assertEqual([(c["axis"], c["value"], c["action"]) for c in a],
                         [(c["axis"], c["value"], c["action"]) for c in b])

    def test_somente_eixos_permitidos(self):
        accepted, _ = p05.generate_contextual_candidates(self.champ, self._train())
        axes = {c["axis"] for c in accepted}
        self.assertTrue(axes <= set(p05.P051_CONTEXT_AXES))
        for proibido in ("symbol", "base", "pattern", "direction", "hour_utc",
                         "day_of_week", "score_bin", "atr_band"):
            self.assertNotIn(proibido, axes)

    def test_segmento_pequeno_nao_gera_candidato(self):
        raw = [_raw(key=f"x{i}", resolved_at=T0 + timedelta(hours=i),
                    created_at=T0 + timedelta(hours=i),
                    realized_r=-1.0, status="lost",
                    features={"regime": "RARE"}) for i in range(5)]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        accepted, rejected = p05.generate_contextual_candidates(self.champ, rows)
        self.assertEqual(accepted, [])
        self.assertIn("TRAIN_SEGMENT_TOO_SMALL", {r.get("reason") for r in rejected})

    def test_cobertura_baixa_do_eixo_rejeita(self):
        raw = [_raw(key=f"y{i}", resolved_at=T0 + timedelta(hours=i),
                    created_at=T0 + timedelta(hours=i),
                    realized_r=-1.0, status="lost",
                    features={"regime": "ALT_DANGER"} if i < 5 else {})
               for i in range(60)]
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        accepted, rejected = p05.generate_contextual_candidates(self.champ, rows)
        self.assertEqual(accepted, [])
        self.assertIn("AXIS_COVERAGE_TOO_LOW", {r.get("reason") for r in rejected})

    def test_contexto_na_direcao_errada_nao_gera(self):
        _acc, rejected = p05.generate_contextual_candidates(self.champ, self._train())
        self.assertIn("CONTEXT_DIRECTION_MISMATCH", {r.get("reason") for r in rejected})

    def test_registra_evidencia_do_contexto(self):
        accepted, _ = p05.generate_contextual_candidates(self.champ, self._train())
        for cand in accepted:
            ev = cand["context_evidence"]
            for field in ("train_count", "train_expectancy_r", "reliability",
                          "axis_coverage_pct"):
                self.assertIn(field, ev)
            self.assertIn(ev["reliability"],
                          (p05.RELIABILITY_USABLE, p05.RELIABILITY_STRONG))
            self.assertTrue(cand["generation_reason"])


class ContextualLeakageTests(unittest.TestCase):
    def setUp(self):
        self.champ = p05.discover_champion_config()

    def _dataset(self, *, test_r=1.0):
        """Treino/validação fixos; só o TESTE muda com `test_r`."""
        raw = []
        for i in range(120):
            bad = i % 2 == 0
            raw.append(_raw(key=f"d{i}", resolved_at=T0 + timedelta(hours=i),
                            created_at=T0 + timedelta(hours=i),
                            realized_r=-1.0 if bad else 1.0,
                            status="lost" if bad else "won_tp2",
                            features={"regime": "ALT_DANGER" if bad else "NORMAL"}))
        for i in range(40):                        # cauda = holdout
            raw.append(_raw(key=f"h{i}", resolved_at=T0 + timedelta(hours=500 + i),
                            created_at=T0 + timedelta(hours=500 + i),
                            realized_r=test_r, status="won_tp2" if test_r > 0 else "lost",
                            features={"regime": "ALT_DANGER" if i % 2 == 0 else "NORMAL"}))
        rows, _ = p05.normalize_outcomes(raw, source="SHADOW")
        return rows

    def test_alterar_teste_nao_muda_candidatos_gerados(self):
        a = p05.temporal_split(self._dataset(test_r=1.0))
        b = p05.temporal_split(self._dataset(test_r=-5.0))
        ca, _ = p05.generate_contextual_candidates(self.champ, a["train"])
        cb, _ = p05.generate_contextual_candidates(self.champ, b["train"])
        self.assertEqual([(c["axis"], c["value"], c["action"]) for c in ca],
                         [(c["axis"], c["value"], c["action"]) for c in cb])

    def test_alterar_teste_nao_muda_hashes(self):
        a = p05.temporal_split(self._dataset(test_r=1.0))
        b = p05.temporal_split(self._dataset(test_r=-5.0))
        ha = [p05.canonical_hash(c["config"])
              for c in p05.generate_contextual_candidates(self.champ, a["train"])[0]]
        hb = [p05.canonical_hash(c["config"])
              for c in p05.generate_contextual_candidates(self.champ, b["train"])[0]]
        self.assertEqual(ha, hb)

    def test_holdout_fechado_na_selecao(self):
        rows = self._dataset()
        cand = p05.generate_contextual_candidates(
            self.champ, p05.temporal_split(rows)["train"])[0][0]
        preview = p05.evaluate_contextual_candidate_offline(
            rows, self.champ, cand, include_holdout=False)
        self.assertTrue(preview["test"].get("withheld"))
        self.assertFalse(preview["split"]["holdout_opened"])

    def test_holdout_aberto_somente_para_finalista(self):
        rows = self._dataset()
        cand = p05.generate_contextual_candidates(
            self.champ, p05.temporal_split(rows)["train"])[0][0]
        final = p05.evaluate_contextual_candidate_offline(
            rows, self.champ, cand, include_holdout=True)
        self.assertTrue(final["split"]["holdout_opened"])
        self.assertNotIn("withheld", final["test"])

    def test_componentes_congelados_no_treino(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("def evaluate_contextual_candidate_offline")[1].split(
            "def is_contextual_experiment")[0]
        self.assertIn('contextual_active_components(split["train"])', block)
        self.assertIn('axis_coverage(split["train"]', block)
        self.assertIn("CONGELADOS no treino", block)

    def test_geracao_usa_somente_treino(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def evaluate_contextual_candidates")[1].split(
            "# ═══")[0]
        self.assertIn('generate_contextual_candidates(champion, split["train"])', block)


def _ccmp(champ, cand, *, delta_low=0.1, affected=30, evaluable=200,
          avoided_exp=-0.4, avoided_ci_high=-0.1, added_exp=None,
          added_ci_low=None, regressions=None):
    return {
        "champion": champ, "candidate": cand,
        "evaluable": evaluable, "unknown_excluded": 0,
        "affected_ops": affected,
        "avoided_expectancy_r": avoided_exp,
        "avoided_expectancy_ci": ({"low": avoided_ci_high - 0.3, "high": avoided_ci_high}
                                  if avoided_ci_high is not None else None),
        "added_expectancy_r": added_exp,
        "added_expectancy_ci": ({"low": added_ci_low, "high": added_ci_low + 0.3}
                                if added_ci_low is not None else None),
        "delta_expectancy_ci": {"low": delta_low, "high": delta_low + 0.3,
                                "point": delta_low, "method": "paired-annotation-bootstrap"},
        "material_segment_regressions": regressions or [],
    }


class ContextualGateTests(unittest.TestCase):
    def _loss(self, **kw):
        champ = _mk(100, 0.05, pf=1.1, dd=6.0)
        cand = _mk(80, 0.30, pf=1.9, dd=3.0)
        v = _ccmp(champ, cand, **kw)
        t = _ccmp(_mk(50, 0.05, pf=1.1, dd=6.0), _mk(40, 0.28, pf=1.9, dd=3.0), **kw)
        return p05.evaluate_contextual_gate(p05.OBJECTIVE_LOSS_REDUCTION, "BLOCK", v, t)

    def _more(self, **kw):
        champ = _mk(100, 0.20, pf=1.5, dd=4.0, sum_r=20.0)
        cand = _mk(130, 0.18, pf=1.4, dd=4.2, sum_r=23.4)
        base = dict(added_exp=0.15, added_ci_low=0.05, avoided_exp=None,
                    avoided_ci_high=None)
        base.update(kw)
        v = _ccmp(champ, cand, **base)
        t = _ccmp(_mk(50, 0.20, pf=1.5, dd=4.0, sum_r=10.0),
                  _mk(60, 0.18, pf=1.4, dd=4.2, sum_r=10.8), **base)
        return p05.evaluate_contextual_gate(p05.OBJECTIVE_MORE_OPERATIONS, "SCORE_DELTA", v, t)

    def test_loss_reduction_valido(self):
        self.assertEqual(self._loss()["verdict"], p05.STATUS_OFFLINE_VALIDATED)

    def test_loss_reduction_rejeitado_por_ic_pareado(self):
        res = self._loss(delta_low=-0.05)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("ci_delta_pareado_acima_de_zero", res["detail"])

    def test_loss_reduction_rejeitado_por_drawdown(self):
        v = _ccmp(_mk(100, 0.05, pf=1.1, dd=4.0), _mk(80, 0.30, pf=1.9, dd=7.0))
        t = _ccmp(_mk(50, 0.05, pf=1.1, dd=4.0), _mk(40, 0.28, pf=1.9, dd=7.0))
        res = p05.evaluate_contextual_gate(p05.OBJECTIVE_LOSS_REDUCTION, "BLOCK", v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("drawdown_nao_pior", res["detail"])

    def test_loss_reduction_exige_contexto_bloqueado_negativo(self):
        res = self._loss(avoided_exp=0.4, avoided_ci_high=0.6)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("contexto_bloqueado_negativo", res["detail"])

    def test_loss_reduction_exige_ic_superior_das_evitadas_negativo(self):
        res = self._loss(avoided_exp=-0.2, avoided_ci_high=0.3)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("ci_superior_evitadas_abaixo_de_zero", res["detail"])

    def test_amostra_afetada_minima(self):
        res = self._loss(affected=5)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("amostra_afetada_minima", res["detail"])

    def test_more_operations_valido(self):
        self.assertEqual(self._more()["verdict"], p05.STATUS_OFFLINE_VALIDATED)

    def test_more_operations_rejeitado_por_menos_de_110pct(self):
        v = _ccmp(_mk(100, 0.20, sum_r=20.0), _mk(105, 0.19, sum_r=19.95),
                  added_exp=0.15, added_ci_low=0.05, avoided_exp=None, avoided_ci_high=None)
        t = _ccmp(_mk(50, 0.20, sum_r=10.0), _mk(55, 0.19, sum_r=10.45),
                  added_exp=0.15, added_ci_low=0.05, avoided_exp=None, avoided_ci_high=None)
        res = p05.evaluate_contextual_gate(p05.OBJECTIVE_MORE_OPERATIONS, "SCORE_DELTA", v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("operacoes_min_110pct", res["detail"])

    def test_more_operations_rejeitado_por_ic_pareado_negativo(self):
        res = self._more(delta_low=-0.2)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("ci_delta_pareado_nao_negativo", res["detail"])

    def test_more_operations_rejeitado_por_drawdown(self):
        v = _ccmp(_mk(100, 0.20, dd=4.0, sum_r=20.0), _mk(130, 0.18, dd=9.0, sum_r=23.4),
                  added_exp=0.15, added_ci_low=0.05, avoided_exp=None, avoided_ci_high=None)
        t = _ccmp(_mk(50, 0.20, dd=4.0, sum_r=10.0), _mk(60, 0.18, dd=9.0, sum_r=10.8),
                  added_exp=0.15, added_ci_low=0.05, avoided_exp=None, avoided_ci_high=None)
        res = p05.evaluate_contextual_gate(p05.OBJECTIVE_MORE_OPERATIONS, "SCORE_DELTA", v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("drawdown_max_110pct", res["detail"])

    def test_adicionais_sem_ic_positivo_rejeitadas(self):
        res = self._more(added_ci_low=-0.05)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("ci_adicionais_acima_de_zero", res["detail"])

    def test_adicionais_com_expectancy_negativa_rejeitadas(self):
        res = self._more(added_exp=-0.3, added_ci_low=-0.5)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("operacoes_adicionais_positivas", res["detail"])

    def test_regressao_material_rejeitada(self):
        res = self._loss(regressions=[{"axis": "tier", "segment": "B", "count": 20,
                                       "candidate_expectancy_r": -0.5}])
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("sem_segmento_material_negativo", res["detail"])

    def test_amostra_oos_insuficiente(self):
        v = _ccmp(_mk(100, 0.05), _mk(80, 0.30))
        t = _ccmp(_mk(50, 0.05), _mk(5, 0.28))
        res = p05.evaluate_contextual_gate(p05.OBJECTIVE_LOSS_REDUCTION, "BLOCK", v, t)
        self.assertEqual(res["verdict"], p05.STATUS_INSUFFICIENT)
        self.assertEqual(res["reason_code"], "OOS_SAMPLE_TOO_SMALL")

    def test_nao_forca_vencedor(self):
        v = _ccmp(_mk(100, 0.30, pf=2.0, dd=2.0), _mk(60, -0.20, pf=0.5, dd=9.0),
                  delta_low=-0.9)
        t = _ccmp(_mk(50, 0.30), _mk(40, -0.20), delta_low=-0.9)
        res = p05.evaluate_contextual_gate(p05.OBJECTIVE_LOSS_REDUCTION, "BLOCK", v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)

    def test_unknown_nao_aprova(self):
        v = _ccmp(_mk(100, 0.05), _mk(0, None, sum_r=0.0), evaluable=0)
        t = _ccmp(_mk(50, 0.05), _mk(40, 0.28))
        res = p05.evaluate_contextual_gate(p05.OBJECTIVE_LOSS_REDUCTION, "BLOCK", v, t)
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("sem_aprovacao_por_unknown", res["detail"])


class ContextualLifecycleTests(unittest.TestCase):
    def _exp(self, **kw):
        from models.strategy_experiment import StrategyExperiment
        base = dict(experiment_key="k", champion_hash="a", candidate_hash="b",
                    status=p05.STATUS_OFFLINE_VALIDATED,
                    objective=p05.OBJECTIVE_LOSS_REDUCTION,
                    candidate_config={"CONTEXT_RULE": _ctx_rule()},
                    offline_metrics={"phase": "P05.1", "execution_mode": "ANALYTICS_ONLY",
                                     "promotable": False, "shadow_supported": False})
        base.update(kw)
        return StrategyExperiment(**base)

    def test_reconhece_experimento_contextual(self):
        self.assertTrue(p05.is_contextual_experiment(self._exp()))

    def test_reconhece_por_marcadores_mesmo_sem_config(self):
        exp = self._exp(candidate_config={"SCORE_MIN": 55})
        self.assertTrue(p05.is_contextual_experiment(exp))

    def test_experimento_global_nao_e_contextual(self):
        exp = self._exp(candidate_config={"SCORE_MIN": 55},
                        offline_metrics={"phase": "P05", "shadow_supported": True})
        self.assertFalse(p05.is_contextual_experiment(exp))

    def test_start_shadow_bloqueia_p051(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def start_shadow")[1].split("async def evaluate_shadow")[0]
        self.assertIn("is_contextual_experiment(exp)", block)
        self.assertIn("P051_BLOCK_REASON", block)
        # o bloqueio precede a checagem de transição
        self.assertLess(block.index("is_contextual_experiment(exp)"),
                        block.index("can_transition(exp.status, STATUS_SHADOW)"))

    def test_evaluate_shadow_bloqueia_p051(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def evaluate_shadow")[1]
        self.assertIn("is_contextual_experiment(exp)", block)
        self.assertIn("P051_BLOCK_REASON", block)

    def test_reason_code_oficial(self):
        self.assertEqual(p05.P051_BLOCK_REASON, "P051_ANALYTICS_ONLY")
        self.assertEqual(p05.P051_PHASE, "P05.1")
        self.assertEqual(p05.P051_EXECUTION_MODE, "ANALYTICS_ONLY")

    def test_sem_promotion_plan_executavel_para_p051(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def evaluate_contextual_candidates")[1].split("# ═══")[0]
        self.assertNotIn("build_promotion_plan", block)
        self.assertIn("Não é aplicável ao LIVE", block)

    def test_offline_metrics_marcam_analytics_only(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def evaluate_contextual_candidates")[1].split("# ═══")[0]
        for marker in ('"phase": P051_PHASE', '"execution_mode": P051_EXECUTION_MODE',
                       '"promotable": False', '"shadow_supported": False'):
            self.assertIn(marker, block, f"marcador ausente: {marker}")

    def test_persistencia_reutiliza_upsert_e_lock(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def evaluate_contextual_candidates")[1].split("# ═══")[0]
        self.assertIn("_acquire_p05_lock(session, _P05_EVALUATE_LOCK_KEY)", block)
        self.assertIn("_upsert_experiment(", block)
        self.assertIn("build_experiment_key(", block)
        self.assertIn("dataset_fingerprint(rows)", block)

    def test_nao_cria_tabela_nova(self):
        src = (BACKEND / "models/strategy_experiment.py").read_text()
        self.assertEqual(src.count("__tablename__"), 1)
        self.assertIn('__tablename__ = "strategy_experiments"', src)
        db_src = (BACKEND / "db.py").read_text()
        self.assertNotIn("contextual", db_src.lower())


class ContextualApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "main.py").read_text()
        cls.tree = ast.parse(cls.src)
        cls.node = None
        for node in ast.walk(cls.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and dec.args
                            and isinstance(dec.args[0], ast.Constant)
                            and dec.args[0].value == "/api/strategy/p05/contextual-evaluate"):
                        cls.node = node
        cls.body = ast.get_source_segment(cls.src, cls.node) if cls.node else ""

    def test_endpoint_existe_e_e_post(self):
        self.assertIsNotNone(self.node, "endpoint contextual-evaluate ausente")
        self.assertIn("@app.post", self.src.split("p05_contextual_evaluate")[0][-400:])

    def test_exige_auth_admin(self):
        self.assertIn("_check_admin_token", self.body)
        self.assertIn("x_admin_token", self.body)
        self.assertIn("if gate:", self.body)

    def test_days_clamp(self):
        self.assertIn("max(7, min(int(days), 365))", self.body)

    def test_chama_avaliacao_contextual(self):
        self.assertIn("evaluate_contextual_candidates", self.body)

    def test_erro_sem_persistencia_parcial(self):
        self.assertIn("HTTPException", self.body)
        self.assertIn("500", self.body)

    def test_nao_altera_endpoint_p05_global(self):
        global_ep = self.src.split('@app.post("/api/strategy/p05/evaluate")')[1].split("@app.")[0]
        self.assertIn("evaluate_candidates(days=days)", global_ep)
        self.assertNotIn("contextual", global_ep)

    def test_sem_endpoints_proibidos(self):
        for proibido in ("enable-context", "/promote", "/apply", "activate-live",
                         "change-size", "retry-now", "/execute"):
            self.assertNotIn(f'"{proibido}"', self.src, f"endpoint proibido: {proibido}")

    def test_status_expoe_resumo_contextual(self):
        svc = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = svc.split("async def get_p05_status")[1]
        for field in ("contextual_experiments", "contextual_offline_validated",
                      "contextual_rejected", "contextual_analytics_only"):
            self.assertIn(field, block, f"campo ausente no status: {field}")


class ContextualArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        cls.block = cls.src.split("#  P05.1 — candidatos CONTEXTUAIS")[1].split(
            "#  P05C — champion × challenger")[0]

    def test_sem_sdk_rede_ou_provider(self):
        for proibido in ("import ccxt", "binance_signed_service", "bybit_signed_service",
                         "httpx", "aiohttp", "requests", "urllib"):
            self.assertNotIn(proibido, self.block, f"proibido no P05.1: {proibido}")

    def test_sem_ordem_nem_executor(self):
        for proibido in ("place_order", "cancel_order", "place_protection_orders",
                         "cancel_algo_order", "_signed_request", "shadow_trade_service",
                         "trade_manager", "real_trade_service"):
            self.assertNotIn(proibido, self.block, f"proibido no P05.1: {proibido}")

    def test_sem_alteracao_de_flag_live(self):
        self.assertNotIn("os.environ[", self.block)
        for proibido in ("LIVE_SIZE_MULT", "MAKER_ENTRY_ENABLED", "TF_UPGRADE_ENABLED",
                         "PYRAMIDING_ENABLED", "KILL_SWITCH"):
            self.assertNotIn(proibido, self.block, f"flag proibida: {proibido}")

    def test_sem_alteracao_de_sizing_risco_stop_tp(self):
        for proibido in ("qty", "leverage", "notional", "risk_pct", "stop_loss",
                         "planned_tp", "size_mult"):
            self.assertNotIn(proibido, self.block, f"termo de execução proibido: {proibido}")

    def test_nao_altera_score_nem_tier_real(self):
        """A regra só produz veredito contrafactual; nada é escrito no snapshot."""
        self.assertNotIn("row[\"score\"] =", self.block)
        self.assertNotIn("row['score'] =", self.block)
        self.assertNotIn("row[\"tier\"] =", self.block)
        self.assertNotIn(".score =", self.block)
        self.assertNotIn(".tier =", self.block)

    def test_reutiliza_nucleo_sem_duplicar(self):
        for reutilizado in ("eligibility(", "_execution_score(", "compute_evidence_metrics(",
                            "bootstrap_paired_membership_delta_ci(",
                            "_material_segment_regressions(", "temporal_split(",
                            "walkforward_folds(", "split_by_config("):
            self.assertIn(reutilizado, self.block, f"não reutilizou: {reutilizado}")

    def test_context_rule_fora_da_knob_allowlist(self):
        allowlist = self.src.split("KNOB_ALLOWLIST: Dict[str, Dict[str, Any]] = {")[1].split("}\n\n")[0]
        self.assertNotIn("CONTEXT_RULE", allowlist)

    def test_eixos_de_alta_cardinalidade_proibidos(self):
        self.assertEqual(tuple(p05.P051_CONTEXT_AXES), ("regime", "entry_zone_type"))

    def test_score_delta_fixo(self):
        self.assertEqual(p05.P051_SCORE_DELTA, -2.0)

    def test_sem_flags_novas(self):
        env = p05.env_snapshot()
        self.assertFalse([k for k in env if "P051" in k or "CONTEXT" in k.upper()],
                         "P05.1 não pode criar flag nova")

    def test_shadow_continua_desligado_por_default(self):
        self.assertIn('_env_bool("P05_CHALLENGER_SHADOW_ENABLED", "false")', self.src)


class ContextualFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND.parent / "frontend/src/components/AssertivenessPanel.tsx").read_text()

    def test_nao_renderiza_object_object(self):
        """CONTEXT_RULE é objeto: precisa de formatador dedicado."""
        self.assertIn("describeConfig", self.src)
        self.assertIn("CONTEXT_RULE", self.src)
        self.assertNotIn("`${k} → ${String(v)}`", self.src)

    def test_mostra_eixo_valor_e_acao(self):
        block = self.src.split("function describeConfig")[1].split("function SegRow")[0]
        self.assertIn("r.axis", block)
        self.assertIn("r.value", block)
        self.assertIn("r.action", block)
        self.assertIn("score_delta", block)

    def test_marca_somente_analise(self):
        self.assertIn("somente análise", self.src)
        self.assertIn("isContextual", self.src)

    def test_sem_acao_de_ativacao_ou_promocao(self):
        low = self.src.lower()
        for proibido in ("contextual-evaluate", "enable-context", "ativar regra",
                         "promover", "/apply", "activate-live"):
            self.assertNotIn(proibido, low, f"ação proibida no front: {proibido}")

    def test_dist_nao_editado(self):
        import subprocess
        out = subprocess.run(["git", "status", "--short", "frontend/dist"],
                             cwd=BACKEND.parent, capture_output=True, text=True).stdout
        # index.html já vinha sujo da baseline; nenhum asset novo pode aparecer
        self.assertNotIn("frontend/dist/assets", out)


if __name__ == "__main__":
    unittest.main()
