"""P05 — otimização de estratégia governada por evidência.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS (a suíte falha se o código
sob teste tentar sair para a rede, mesmo que capture a exceção). Nenhum acesso a
exchange, banco externo, Railway ou Vercel.
"""
from __future__ import annotations

import ast
import math
import unittest
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
        t = _cmp(_mk(50, 0.20), _mk(60, 0.15))
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
        row = _raw(features={"chase_atr": 0.5})
        a = p05.build_experiment_annotation(row, self.champ, {"PROXIMITY_MAX_ATR": 1.5},
                                            experiment_key="k1", candidate_hash="h1",
                                            active=["proximity"])
        b = p05.build_experiment_annotation(row, self.champ, {"PROXIMITY_MAX_ATR": 1.5},
                                            experiment_key="k1", candidate_hash="h1",
                                            active=["proximity"])
        for field in ("experiment_key", "candidate_hash", "challenger_status",
                      "champion_eligible", "challenger_eligible", "reason_code"):
            self.assertEqual(a[field], b[field])

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
        res = p05.evaluate_shadow_gate({"challenger_resolved": 5, "observed_days": 3,
                                        "coverage_pct": 100.0, "candidate": {"expectancy_r": 0.5}})
        self.assertEqual(res["verdict"], p05.STATUS_INSUFFICIENT)
        self.assertEqual(res["reason_code"], "AGUARDANDO_AMOSTRA")

    def test_shadow_gate_aprova(self):
        res = p05.evaluate_shadow_gate({
            "challenger_resolved": 40, "observed_days": 20, "coverage_pct": 95.0,
            "candidate": {"expectancy_r": 0.3}, "objective_still_met": True,
            "operational_incident": False, "safety_relaxed": False})
        self.assertEqual(res["verdict"], p05.STATUS_ELIGIBLE)

    def test_shadow_gate_rejeita_cobertura_baixa(self):
        res = p05.evaluate_shadow_gate({
            "challenger_resolved": 40, "observed_days": 20, "coverage_pct": 50.0,
            "candidate": {"expectancy_r": 0.3}, "objective_still_met": True})
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("cobertura_minima", res["detail"])

    def test_shadow_gate_rejeita_relaxamento_de_safety(self):
        res = p05.evaluate_shadow_gate({
            "challenger_resolved": 40, "observed_days": 20, "coverage_pct": 95.0,
            "candidate": {"expectancy_r": 0.3}, "objective_still_met": True,
            "safety_relaxed": True})
        self.assertEqual(res["verdict"], p05.STATUS_REJECTED)
        self.assertIn("sem_relaxamento_p01_p04", res["detail"])

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

    def test_promotion_plan_nao_ativa_nada(self):
        plan = p05.build_promotion_plan(self.champ, {"PROXIMITY_MAX_ATR": 1.25}, {})
        self.assertIn("env_atual", plan)
        self.assertIn("env_proposta", plan)
        self.assertIn("valores_rollback", plan)
        self.assertIn("plano_canario", plan)
        self.assertIn("NÃO foi ativado", plan["aviso"])


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
        """Não basta não citar: o serviço não pode LER como flag nem ESCREVER
        nenhuma chave de safety/live. Menção em texto (plano de rollback) é ok."""
        self.assertNotIn("os.environ[", self.src)
        for proibido in ("LIVE_SIZE_MULT", "MAKER_ENTRY_ENABLED", "TF_UPGRADE_ENABLED",
                         "PYRAMIDING_ENABLED", "KILL_SWITCH"):
            self.assertNotIn(f'getenv("{proibido}"', self.src, f"lê flag proibida: {proibido}")
            self.assertNotIn(f"{proibido} =", self.src, f"atribui flag proibida: {proibido}")
            self.assertNotIn(proibido, p05.KNOB_ALLOWLIST, f"knob proibido na allowlist: {proibido}")

    def test_live_size_mult_so_aparece_como_aviso_de_rollback(self):
        """A única citação permitida é no plano de canário — mantendo-o INALTERADO."""
        for line in self.src.splitlines():
            if "LIVE_SIZE_MULT" in line:
                self.assertIn("inalterado", line.lower(),
                              f"citação de LIVE_SIZE_MULT fora do aviso: {line.strip()}")

    def test_service_nao_escreve_env(self):
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, "setenv")
                if node.func.attr == "putenv":
                    self.fail("service não pode escrever env")

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

    def test_snapshot_usa_namespace_versionado_sem_alterar_schema(self):
        src = (BACKEND / "services/snapshot_service.py").read_text()
        self.assertIn('"p05_context": _p05_context(', src)
        self.assertIn("def _p05_context(", src)
        block = src.split("def _p05_context(")[1].split("\nasync def ")[0]
        self.assertIn('"schema_version": 1', block)
        self.assertIn("nunca inventa valor", block)

    def test_p05_context_nao_inventa_valor_ausente(self):
        src = (BACKEND / "services/snapshot_service.py").read_text()
        block = src.split("def _p05_context(")[1].split("\nasync def ")[0]
        self.assertNotIn("or 0", block)                 # não substitui ausência por zero
        self.assertNotIn("or False", block)

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


if __name__ == "__main__":
    unittest.main()
