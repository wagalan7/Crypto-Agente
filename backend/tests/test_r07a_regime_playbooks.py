"""R07A — playbooks por regime: contratos e comparação offline.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, credencial, ordem ou escrita. Testes COMPORTAMENTAIS — grep aparece
só como reforço.

O pacote é ANALYTICS_ONLY: nada aqui promove estratégia, abre holdout, cria
experimento ou toca no executor.
"""
from __future__ import annotations

import json
import asyncio
from copy import deepcopy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste R07A (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import regime_playbook_service as r07                # noqa: E402
from services import regime_service as rg_svc                      # noqa: E402
from services import snapshot_service as snap                      # noqa: E402
from services import strategy_evidence_service as p05              # noqa: E402

AGORA = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
CT = {"enabled": True, "min_tfs": 1, "block": False, "select_penalty": 12.0}


def _macro(**over):
    base = {"regime": "NORMAL", "block_all": False, "block_alt_longs": False,
            "downgrade_alt_longs": False, "block_shorts": False,
            "downgrade_shorts": False, "filter_enabled": True, "quality": "FRESH",
            "observed_at_ms": int(AGORA.timestamp() * 1000) - 5000}
    base.update(over)
    return base


def _sig(*, tfs=(("4h", "bearish"), ("1d", "bearish")), patterns=(), timestamp=None,
         captured_at=AGORA):
    ms = int(captured_at.timestamp() * 1000)
    fresh = {"candle": {"quality": "FRESH", "source": "rest",
                         "close_time_ms": ms - 10_000,
                         "observed_at_ms": ms - 5_000}}
    return {"mtf": {"higher_tfs": [{"timeframe": tf, "ema_aligned": al,
                                    "data_freshness": deepcopy(fresh)}
                                   for tf, al in tfs]},
            "data_freshness": fresh,
            "patterns": list(patterns), "timestamp": ms - 1_000 if timestamp is None else timestamp}


def _ctx(direction="long", *, tfs=(("4h", "bearish"), ("1d", "bearish")),
         patterns=(), macro=None, is_major=False, captured_at=AGORA, ct=CT,
         symbol="SOLUSDT"):
    return r07.build_r07_context(
        {"direction": direction, "symbol": symbol, "entry_zone_type": "limit_pullback"},
        _sig(tfs=tfs, patterns=patterns, captured_at=captured_at),
        macro if macro is not None else _macro(
            observed_at_ms=int(captured_at.timestamp() * 1000) - 5_000),
        captured_at=captured_at, is_major=is_major, ct_brake=ct)


def _row(i, direction, status, r, ctx, *, created=None, resolved=None):
    context_created = (datetime.fromisoformat(ctx["captured_at"])
                       if isinstance(ctx, dict) and ctx.get("captured_at") else AGORA)
    created = created or context_created
    return {"id": i, "dedupe_key": f"snap:{i}", "symbol": "SOLUSDT",
            "timeframe": "4h", "tier": "A", "direction": direction, "score": 80.0,
            "status": status, "realized_r": r, "stop_distance_pct": 1.0,
            "created_at": created,
            "resolved_at": resolved or (created + timedelta(hours=1)),
            "features": {"regime": "NORMAL", "bot_verdict_ok": True,
                         "edge_score": 2, r07.R07_CONTEXT_KEY: ctx}}


def _stage(n=100, affected=20, *, at=AGORA, offset=0):
    """Instantes coerentes: 20 vetos perdedores + 70 wins/10 stops mantidos."""
    rows = []
    for i in range(n):
        veto = i < affected
        loss = veto or i >= max(affected, n - 10)
        ctx = _ctx("long", tfs=(("4h", "bearish" if veto else "bullish"),),
                   captured_at=at)
        rows.append(_row(offset+i, "long", "lost" if loss else "won_tp1",
                         -1.0 if loss else 2.0, ctx))
    return rows


# ════════════════════════════════════════════════════════════════════════════
#  A. CONTEXTO PROSPECTIVO
# ════════════════════════════════════════════════════════════════════════════
class ContextoProspectivo(unittest.TestCase):

    def test_allowlist_nao_guarda_o_sinal_inteiro(self):
        sig = _sig(patterns=[{"type": "bull_flag", "direction": "long",
                              "breakout_confirmed": True, "retest_active": False,
                              "points": [1, 2, 3], "lines": [[1, 2]]}])
        sig["indicators"] = {"ema12": 1.0, "rsi": 50.0}
        sig["trade_plan"] = {"segredo": "x"}
        ctx = r07.build_r07_context({"direction": "long", "symbol": "SOLUSDT"},
                                    sig, _macro(), captured_at=AGORA,
                                    is_major=False, ct_brake=CT)
        texto = json.dumps(ctx)
        for proibido in ("indicators", "trade_plan", "points", "lines", "segredo",
                         "realized_r", "outcome", "p05_path", "status"):
            self.assertNotIn(proibido, texto, proibido)
        self.assertEqual(ctx["patterns"][0],
                         {"type": "bull_flag", "direction": "long",
                          "breakout_confirmed": True, "retest_active": False})

    def test_tres_instantes_ficam_separados(self):
        ctx = _ctx()
        self.assertEqual(ctx["captured_at"], AGORA.isoformat())
        self.assertIsNotNone(ctx["macro"]["observed_at_ms"])
        self.assertNotIn("created_at", ctx)   # created_at é da linha, não daqui

    def test_valores_invalidos_nao_viram_numero_nem_flag(self):
        macro = _macro(block_all=1, downgrade_shorts="true",
                       observed_at_ms=float("nan"), quality="  ")
        ctx = r07.build_r07_context({"direction": "LONG", "symbol": "S"},
                                    _sig(), macro, captured_at=AGORA,
                                    is_major=False, ct_brake=CT)
        self.assertIsNone(ctx["macro"]["block_all"])          # 1 não é bool
        self.assertIsNone(ctx["macro"]["downgrade_shorts"])   # "true" não é bool
        self.assertIsNone(ctx["macro"]["observed_at_ms"])     # NaN não é número
        self.assertIsNone(ctx["macro"]["quality"])            # "  " não é texto
        self.assertEqual(ctx["direction"], "long")

    def test_lado_invalido_fica_ausente(self):
        for ruim in ("buy", "", None, 1, "LONGO"):
            with self.subTest(direction=repr(ruim)):
                ctx = r07.build_r07_context({"direction": ruim, "symbol": "S"},
                                            _sig(), _macro(), captured_at=AGORA,
                                            is_major=False, ct_brake=CT)
                self.assertIsNone(ctx["direction"])

    def test_retest_ausente_nao_vira_false(self):
        ctx = _ctx(patterns=[{"type": "bull_flag", "direction": "long"}])
        self.assertIsNone(ctx["patterns"][0]["retest_active"])
        self.assertIsNone(ctx["patterns"][0]["breakout_confirmed"])

    def test_macro_normal_com_unknown_ou_disabled_nao_vira_contexto_valido(self):
        for macro in (_macro(quality="UNKNOWN"), _macro(quality="DISABLED"),
                      _macro(filter_enabled=False), _macro(quality=None)):
            with self.subTest(quality=macro.get("quality"),
                              enabled=macro.get("filter_enabled")):
                cls = r07.classify_context(_ctx(macro=macro))
                self.assertEqual(cls["macro"], [r07.DIM_UNKNOWN])
                self.assertIn(r07.REASON_MACRO_NOT_OBSERVED, cls["reasons"])

    def test_observacao_posterior_nao_e_retrodatada(self):
        macro = _macro(observed_at_ms=int(AGORA.timestamp() * 1000) + 60_000)
        ctx = _ctx(macro=macro)
        # o valor gravado é preservado como veio — nada é reescrito
        self.assertGreater(ctx["macro"]["observed_at_ms"],
                           AGORA.timestamp() * 1000)
        cls = r07.classify_context(ctx)
        self.assertEqual(cls["macro"], [r07.DIM_UNKNOWN])
        self.assertIn(r07.REASON_MACRO_STALE_OBSERVATION, cls["reasons"])

    def test_macro_exige_fresh_timestamp_positivo_e_flags_explicitas(self):
        hyp = "H2_ABSTAIN_DOWNGRADED_SHORTS"
        cases = [("quality", x) for x in (None, "MYSTERY", "DEGRADED", "STALE")]
        cases += [("observed_at_ms", x) for x in
                  (None, 0, -1, True, "123", float("inf"), float("nan"))]
        cases += [("filter_enabled", x) for x in (None, 1, "true", False)]
        cases += [("downgrade_shorts", x) for x in (None, 0, "false")]
        for key, value in cases:
            with self.subTest(key=key, value=repr(value)):
                ctx = _ctx("short", macro=_macro(**{key: value}))
                self.assertEqual(r07.decide(hyp, ctx)["decision"], r07.UNKNOWN)
        ctx = _ctx("short")
        del ctx["macro"]["downgrade_shorts"]
        self.assertEqual(r07.decide(hyp, ctx)["decision"], r07.UNKNOWN)
        self.assertEqual(r07.decide(hyp, _ctx("short"))["decision"], r07.KEEP)

    def test_macro_e_captura_precisam_provar_instante(self):
        for value in (None, "", "invalid", "1970-01-01T00:00:00+00:00"):
            with self.subTest(captured_at=value):
                ctx = _ctx("short")
                ctx["captured_at"] = value
                self.assertEqual(r07.decide("H2_ABSTAIN_DOWNGRADED_SHORTS", ctx)
                                 ["decision"], r07.UNKNOWN)

    def test_allowlist_preserva_proveniencia_dos_tfs(self):
        sig = _sig()
        ctx = r07.build_r07_context({"direction": "long", "symbol": "SOLUSDT"},
                                    sig, _macro(), captured_at=AGORA,
                                    is_major=False, ct_brake=CT)
        actual = ctx["higher_tfs"][0]["data_freshness"]["candle"]
        expected = sig["mtf"]["higher_tfs"][0]["data_freshness"]["candle"]
        self.assertEqual({key: actual[key] for key in expected}, expected)
        self.assertNotIn("open", json.dumps(ctx["higher_tfs"]))

    def test_h1_recusa_tf_sem_freshness_ou_futuro(self):
        for key, value in (("quality", None), ("quality", "UNKNOWN"),
                           ("quality", "MYSTERY"), ("source", None),
                           ("observed_at_ms", None), ("close_time_ms", None),
                           ("observed_at_ms", int(AGORA.timestamp()*1000)+1),
                           ("close_time_ms", int(AGORA.timestamp()*1000)+1),
                           ("observed_at_ms", 0), ("close_time_ms", True)):
            with self.subTest(key=key, value=value):
                sig = _sig(tfs=(("4h", "bearish"),))
                sig["mtf"]["higher_tfs"][0]["data_freshness"]["candle"][key] = value
                ctx = r07.build_r07_context({"direction": "long"}, sig, _macro(),
                                            captured_at=AGORA, is_major=False,
                                            ct_brake=CT)
                self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", ctx)
                                 ["decision"], r07.UNKNOWN)
        sig = _sig(tfs=(("4h", "bearish"),))
        del sig["mtf"]["higher_tfs"][0]["data_freshness"]
        ctx = r07.build_r07_context({"direction": "long"}, sig, _macro(),
                                    captured_at=AGORA, is_major=False, ct_brake=CT)
        self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", ctx)["decision"],
                         r07.UNKNOWN)

    def test_anotacao_falha_sem_interromper_o_save(self):
        with patch.object(r07, "build_r07_context", side_effect=RuntimeError("boom")):
            feats = snap._extract_features(
                {"symbol": "SOLUSDT", "direction": "long", "signal": _sig()},
                AGORA, "NORMAL", _macro())
        self.assertNotIn(r07.R07_CONTEXT_KEY, feats)   # ausente, não corrompido
        self.assertIn("p05_context", feats)            # demais namespaces intactos
        self.assertIn("probability_contract", feats)
        self.assertEqual(feats["regime"], "NORMAL")

    def test_namespaces_preexistentes_preservados(self):
        feats = snap._extract_features(
            {"symbol": "SOLUSDT", "direction": "long", "signal": _sig()},
            AGORA, "NORMAL", _macro())
        for chave in ("p05_context", "probability_contract", "regime", "hour_utc"):
            self.assertIn(chave, feats, chave)
        self.assertIn(r07.R07_CONTEXT_KEY, feats)
        self.assertEqual(feats[r07.R07_CONTEXT_KEY]["schema_version"],
                         r07.R07_CONTEXT_SCHEMA_VERSION)

    def test_uma_unica_consulta_macro_por_batch(self):
        """`_current_regime_label` passou a delegar — sem consulta extra."""
        import asyncio

        chamadas = {"n": 0}

        async def _fake():
            chamadas["n"] += 1
            return _macro()

        with patch("services.regime_service.get_regime_status", _fake):
            rotulo, estado = asyncio.run(snap._current_regime_state())
            self.assertEqual(chamadas["n"], 1)
            self.assertEqual(rotulo, "NORMAL")
            self.assertIsInstance(estado, dict)
            # o wrapper legado continua funcionando e não duplica a consulta
            chamadas["n"] = 0
            self.assertEqual(asyncio.run(snap._current_regime_label()), "NORMAL")
            self.assertEqual(chamadas["n"], 1)

    def test_politica_de_majors_e_exatamente_a_do_regime_service(self):
        """Não recriamos lista de majors: delegamos, INCLUSIVE nas bordas.

        `is_btc_symbol` só reconhece major quando o símbolo traz separador
        (`BTC/USDT`, `BTC-USDT`); `BTCUSDT` colado devolve False. Essa é a
        política VIGENTE, e o R07 a reproduz em vez de divergir em silêncio —
        a limitação está documentada, não corrigida por conta própria.
        """
        for simbolo in ("BTC/USDT", "BTC-USDT", "ETH/USDT", "BTCUSDT",
                        "ETHUSDT", "SOLUSDT", "BTC/USDT:USDT"):
            with self.subTest(symbol=simbolo):
                self.assertEqual(snap._r07_is_major(simbolo),
                                 rg_svc.is_btc_symbol(simbolo))
        self.assertTrue(snap._r07_is_major("BTC/USDT"))
        self.assertFalse(snap._r07_is_major("SOLUSDT"))
        self.assertIsNone(snap._r07_is_major(None))


# ════════════════════════════════════════════════════════════════════════════
#  B. CLASSIFICAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class Classificacao(unittest.TestCase):

    def test_payload_legado_malformado_e_versao_desconhecida(self):
        casos = [
            (None, r07.REASON_NO_CONTEXT),
            ("texto", r07.REASON_MALFORMED),
            (42, r07.REASON_MALFORMED),
            ({"schema_version": 99}, r07.REASON_SCHEMA_UNSUPPORTED),
            ({}, r07.REASON_SCHEMA_UNSUPPORTED),
        ]
        for ctx, motivo in casos:
            with self.subTest(ctx=repr(ctx)):
                cls = r07.classify_context(ctx)
                self.assertEqual(cls["trend"], r07.DIM_UNKNOWN)
                self.assertIn(motivo, cls["reasons"])

    def test_aligned_count_alto_nao_substitui_a_ema(self):
        """`mtf.aligned_count` mede direção do SINAL; a tendência vem da EMA."""
        sig = _sig(tfs=(("4h", "bearish"), ("1d", "bearish")))
        sig["mtf"]["aligned_count"] = 3
        sig["mtf"]["alignment_score"] = 1.0
        ctx = r07.build_r07_context({"direction": "long", "symbol": "S"}, sig,
                                    _macro(), captured_at=AGORA, is_major=False,
                                    ct_brake=CT)
        self.assertNotIn("aligned_count", json.dumps(ctx))
        self.assertEqual(r07.classify_context(ctx)["trend"], r07.TREND_BEARISH)

    def test_tf_duplicado_nao_conta_duas_vezes(self):
        ctx = _ctx(tfs=(("4h", "bullish"), ("4h", "bullish"), ("4h", "bullish")))
        cls = r07.classify_context(ctx)
        ev = cls["evidence"]["trend"]
        self.assertEqual(ev["distinct_timeframes"], 1)
        self.assertEqual(ev["bullish_timeframes"], 1)

    def test_conflito_no_mesmo_tf_torna_incerto(self):
        ctx = _ctx(tfs=(("4h", "bullish"), ("4h", "bearish")))
        cls = r07.classify_context(ctx)
        self.assertEqual(cls["trend"], r07.TREND_UNCERTAIN)
        self.assertIn(r07.REASON_TF_CONFLICT, cls["reasons"])

    def test_tendencia_mista_nao_e_inequivoca(self):
        ctx = _ctx(tfs=(("4h", "bullish"), ("1d", "bearish")))
        self.assertEqual(r07.classify_context(ctx)["trend"], r07.TREND_MIXED)

    def test_schema_booleano_e_tf_mixed_conflitante_nao_passam(self):
        ctx = _ctx()
        ctx["schema_version"] = True
        self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", ctx)["decision"], r07.UNKNOWN)
        ctx = _ctx(tfs=(("4h", "bullish"), ("4h", "mixed")))
        self.assertEqual(r07.classify_context(ctx)["trend"], r07.TREND_UNCERTAIN)
        self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", ctx)["decision"], r07.UNKNOWN)

    def test_ausencia_de_tf_nao_vira_alinhamento_favoravel(self):
        for tfs in ((), (("4h", None),), (("4h", "neutral"),)):
            with self.subTest(tfs=tfs):
                cls = r07.classify_context(_ctx(tfs=tfs))
                self.assertEqual(cls["trend"], r07.DIM_UNKNOWN)
                self.assertNotIn(cls["trend"], (r07.TREND_BULLISH, r07.TREND_BEARISH))

    def test_config_do_freio_ausente_impede_classificar(self):
        ctx = _ctx(ct={})
        cls = r07.classify_context(ctx)
        self.assertEqual(cls["trend"], r07.DIM_UNKNOWN)
        self.assertIn(r07.REASON_CT_CONFIG_MISSING, cls["reasons"])

    def test_lado_long_e_short_espelhados(self):
        alta = r07.classify_context(_ctx("short", tfs=(("4h", "bullish"),)))
        baixa = r07.classify_context(_ctx("long", tfs=(("4h", "bearish"),)))
        self.assertEqual(alta["trend"], r07.TREND_BULLISH)
        self.assertEqual(baixa["trend"], r07.TREND_BEARISH)
        self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", _ctx(
            "short", tfs=(("4h", "bullish"),)))["decision"], r07.VETO)
        self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", _ctx(
            "long", tfs=(("4h", "bearish"),)))["decision"], r07.VETO)

    def test_horizontal_channel_nao_prova_range_intacto(self):
        cls = r07.classify_context(_ctx(patterns=[{"type": "horizontal_channel",
                                                   "direction": "long"}]))
        self.assertEqual(cls["structure"], [r07.STRUCT_HORIZONTAL_CHANNEL])
        ev = cls["evidence"]["structure"]
        # nada de limites, borda, volume ou confirmação inventados
        for inventado in ("range_high", "range_low", "at_edge", "volume", "confirmed"):
            self.assertNotIn(inventado, ev, inventado)
        self.assertFalse(ev["breakout_flag_present"])

    def test_nome_de_padrao_nao_prova_rompimento(self):
        cls = r07.classify_context(_ctx(patterns=[{"type": "bull_flag",
                                                   "direction": "long"}]))
        self.assertNotIn(r07.STRUCT_BREAKOUT_CONFIRMED, cls["structure"])

    def test_rompimento_confirmado_precisa_da_flag_e_da_direcao(self):
        alinhado = r07.classify_context(_ctx("long", patterns=[
            {"type": "bull_flag", "direction": "long", "breakout_confirmed": True}]))
        self.assertIn(r07.STRUCT_BREAKOUT_CONFIRMED, alinhado["structure"])
        self.assertTrue(alinhado["evidence"]["structure"]["aligned_breakout"])
        contra = r07.classify_context(_ctx("long", patterns=[
            {"type": "bear_flag", "direction": "short", "breakout_confirmed": True}]))
        self.assertIn(r07.STRUCT_BREAKOUT_CONFIRMED, contra["structure"])
        self.assertFalse(contra["evidence"]["structure"]["aligned_breakout"])

    def test_retest_so_com_flag_explicita(self):
        sem = r07.classify_context(_ctx(patterns=[{"type": "bull_flag",
                                                   "direction": "long"}]))
        self.assertNotIn(r07.STRUCT_RETEST_ACTIVE, sem["structure"])
        com = r07.classify_context(_ctx(patterns=[
            {"type": "bull_flag", "direction": "long", "retest_active": True}]))
        self.assertIn(r07.STRUCT_RETEST_ACTIVE, com["structure"])

    def test_sem_padrao_fica_unclassified(self):
        cls = r07.classify_context(_ctx(patterns=[]))
        self.assertEqual(cls["structure"], [r07.DIM_UNCLASSIFIED])
        self.assertIn(r07.REASON_NO_PATTERNS, cls["reasons"])

    def test_catalogo_e_informativo(self):
        cat = r07.scenario_catalog()
        self.assertGreaterEqual(len(cat), 4)
        for item in cat:
            self.assertIn("requires", item)
            self.assertIn("missing_without_it", item)
            for proibido in ("decide", "veto", "executable", "threshold"):
                self.assertNotIn(proibido, json.dumps(item).lower(), proibido)


# ════════════════════════════════════════════════════════════════════════════
#  C. HIPÓTESES E COMPARAÇÃO OFFLINE
# ════════════════════════════════════════════════════════════════════════════
class HipotesesOffline(unittest.TestCase):

    def test_exatamente_tres_hipoteses_com_hash_estavel(self):
        self.assertEqual(len(r07.R07_HYPOTHESES), 3)
        h1 = r07.hypothesis_hashes()
        self.assertEqual(h1, r07.hypothesis_hashes())
        self.assertEqual(len(set(h1.values())), 3)
        self.assertEqual(sorted(h1), [
            "H1_ABSTAIN_COUNTER_TREND", "H2_ABSTAIN_DOWNGRADED_ALT_LONGS"
            if False else "H2_ABSTAIN_DOWNGRADED_SHORTS",
            "H3_ABSTAIN_DOWNGRADED_ALT_LONGS"])

    def test_nenhuma_hipotese_de_range_ou_rompimento(self):
        texto = json.dumps([h["config"] for h in r07.R07_HYPOTHESES])
        for proibido in ("breakout", "retest", "horizontal", "range"):
            self.assertNotIn(proibido, texto.lower(), proibido)

    def test_h2_e_h3_respeitam_lado_e_majors(self):
        self.assertEqual(r07.decide("H2_ABSTAIN_DOWNGRADED_SHORTS", _ctx(
            "short", macro=_macro(downgrade_shorts=True)))["decision"], r07.VETO)
        self.assertEqual(r07.decide("H2_ABSTAIN_DOWNGRADED_SHORTS", _ctx(
            "long", macro=_macro(downgrade_shorts=True)))["decision"], r07.KEEP)
        self.assertEqual(r07.decide("H3_ABSTAIN_DOWNGRADED_ALT_LONGS", _ctx(
            "long", macro=_macro(downgrade_alt_longs=True),
            is_major=False))["decision"], r07.VETO)
        self.assertEqual(r07.decide("H3_ABSTAIN_DOWNGRADED_ALT_LONGS", _ctx(
            "long", macro=_macro(downgrade_alt_longs=True),
            is_major=True))["decision"], r07.KEEP)

    def test_major_desconhecido_vira_unknown_e_nao_keep(self):
        ctx = _ctx("long", macro=_macro(downgrade_alt_longs=True))
        ctx["is_major"] = None
        d = r07.decide("H3_ABSTAIN_DOWNGRADED_ALT_LONGS", ctx)
        self.assertEqual(d["decision"], r07.UNKNOWN)
        self.assertEqual(d["reason_code"], r07.REASON_MAJOR_UNKNOWN)

    def test_outcomes_nao_entram_no_classificador(self):
        """Poluir a linha com desfecho não muda a decisão da regra."""
        limpo = _ctx("long", tfs=(("4h", "bearish"),))
        sujo = dict(limpo, status="lost", realized_r=-1.0, outcome_at="x")
        self.assertEqual(r07.decide("H1_ABSTAIN_COUNTER_TREND", limpo),
                         r07.decide("H1_ABSTAIN_COUNTER_TREND", sujo))

    def _comparacao(self, linhas, hyp="H1_ABSTAIN_COUNTER_TREND"):
        cfg = p05.discover_champion_config()
        ativos, _ = r07._baseline_components(linhas)
        return r07.r07_rule_comparison(linhas, hyp, cfg, ativos)

    def test_unknown_excluido_simetricamente_e_contado(self):
        bons = [_row(i, "long", "lost", -1.0, _ctx("long", tfs=(("4h", "bearish"),)))
                for i in range(10)]
        desconhecidos = [_row(100 + i, "long", "won_tp1", 1.0,
                              _ctx("long", tfs=(("4h", "bullish"), ("4h", "bearish"))))
                         for i in range(5)]
        comp = self._comparacao(bons + desconhecidos)
        self.assertEqual(comp["evaluable"], 10)
        self.assertEqual(comp["excluded"]["rule_unknown"], 5)
        self.assertIn(r07.REASON_TF_CONFLICT, comp["unknown_reasons"])
        # cobertura reportada sobre o universo ORIGINAL
        self.assertAlmostEqual(comp["coverage_pct"], 66.7, places=1)

    def test_linhas_sem_contexto_saem_como_desconhecidas(self):
        linhas = [_row(i, "long", "lost", -1.0, None) for i in range(6)]
        comp = self._comparacao(linhas)
        self.assertEqual(comp["evaluable"], 0)
        self.assertEqual(comp["excluded"]["rule_unknown"], 6)
        self.assertIn(r07.REASON_NO_CONTEXT, comp["unknown_reasons"])

    def test_baseline_false_nunca_vira_operacao(self):
        """Setup que o baseline recusa não entra em nenhum dos dois lados."""
        recusado = _row(1, "long", "won_tp1", 2.0, _ctx("long", tfs=(("4h", "bullish"),)))
        recusado["features"]["bot_verdict_ok"] = False
        recusado["features"]["bot_verdict_blocked_by"] = "rr-gate"
        aceito = _row(2, "long", "won_tp1", 2.0, _ctx("long", tfs=(("4h", "bullish"),)))
        comp = self._comparacao([recusado, aceito])
        self.assertEqual(comp["evaluable"], 1)
        self.assertEqual(comp["excluded"]["baseline_blocked"], 1)
        self.assertEqual(comp["champion"]["exposure"], 1)

    def test_cobertura_nao_confunde_bloqueio_conhecido_com_unknown(self):
        rows = _stage()
        for row in rows[:75]:
            row["features"]["bot_verdict_ok"] = False
            row["features"]["bot_verdict_blocked_by"] = "rr-gate"
        out = self._comparacao(rows)
        self.assertEqual(out["coverage_pct"], 100.0)
        self.assertEqual(out["known_baseline_blocked"], 75)
        self.assertEqual(out["evaluable"], 25)
        self.assertEqual(out["excluded"]["baseline_unknown"], 0)
        self.assertEqual(out["excluded"]["rule_unknown"], 0)

    def test_comparacao_recusa_contexto_de_outro_lado_ou_instante(self):
        for kind in ("direction", "capture_future", "macro_future", "tf_future",
                     "missing_created", "signal_future"):
            with self.subTest(kind=kind):
                ctx = _ctx("long", tfs=(("4h", "bearish"),))
                row = _row(1, "long", "lost", -1.0, ctx)
                hyp = "H1_ABSTAIN_COUNTER_TREND"
                if kind == "direction":
                    row["direction"] = "short"
                elif kind == "capture_future":
                    row["created_at"] = AGORA - timedelta(seconds=10)
                elif kind == "macro_future":
                    ctx["macro"]["observed_at_ms"] = int(AGORA.timestamp()*1000)+1
                    hyp = "H2_ABSTAIN_DOWNGRADED_SHORTS"
                    row["direction"] = ctx["direction"] = "short"
                elif kind == "tf_future":
                    ctx["higher_tfs"][0].setdefault("data_freshness", {}).setdefault(
                        "candle", {})["observed_at_ms"] = int(AGORA.timestamp()*1000)+1
                elif kind == "missing_created":
                    row["created_at"] = None
                else:
                    ctx["signal_timestamp_ms"] = int(AGORA.timestamp()*1000)+1
                out = self._comparacao([row], hyp)
                self.assertEqual(out["evaluable"], 0)
                self.assertEqual(out["excluded"]["rule_unknown"], 1)

    def test_bloqueios_nao_escondem_falta_de_contexto_nas_elegiveis(self):
        train = _stage()
        valid = _stage(at=AGORA+timedelta(days=1), offset=1000)
        for group in (train, valid):
            for row in group[:99]:
                row["features"]["bot_verdict_ok"] = False
                row["features"]["bot_verdict_blocked_by"] = "rr-gate"
            group[-1]["features"][r07.R07_CONTEXT_KEY] = None
        out = r07.build_regime_playbooks(train, valid)["hypotheses"][0]
        self.assertEqual(out["train"]["coverage_pct"], 99.0)
        self.assertEqual(out["train"]["eligible_rule_coverage_pct"], 0.0)
        self.assertEqual(out["status"], r07.R07_INSUFFICIENT)

    def test_cobertura_de_decisoes_independe_da_qualidade_do_outcome(self):
        rows = _stage()
        original = self._comparacao(rows)
        damaged = deepcopy(rows)
        for row in damaged[:25]:
            row["realized_r"] = float("nan")
        current = self._comparacao(damaged)
        self.assertEqual(original["known_decisions"], current["known_decisions"])
        self.assertEqual(current["coverage_pct"], 100.0)
        self.assertEqual(current["eligible_rule_coverage_pct"], 100.0)
        self.assertEqual(current["metric_coverage_pct"], 75.0)
        self.assertEqual(current["excluded"]["metric_unavailable"], 25)

    def test_desconhecido_nao_le_outcome_para_decidir_exclusao(self):
        class SealedOutcome(dict):
            def get(self, key, *default):
                if key in ("realized_r", "status", "outcome_class"):
                    raise AssertionError("outcome consultado antes do membership")
                return super().get(key, *default)
        row = SealedOutcome(_row(1, "long", "lost", -1.0, None))
        comp = self._comparacao([row])
        self.assertEqual(comp["unknown_total"], 1)
        self.assertEqual(comp["evaluable"], 0)

    def test_stops_evitados_vem_acompanhados_dos_wins_removidos(self):
        vetados = ([_row(i, "long", "lost", -1.0, _ctx("long", tfs=(("4h", "bearish"),)))
                    for i in range(8)]
                   + [_row(50 + i, "long", "won_tp2", 2.0,
                           _ctx("long", tfs=(("4h", "bearish"),))) for i in range(3)])
        mantidos = [_row(100 + i, "long", "won_tp1", 1.0,
                         _ctx("long", tfs=(("4h", "bullish"),))) for i in range(10)]
        comp = self._comparacao(vetados + mantidos)
        self.assertEqual(comp["operations_removed"], 11)
        self.assertEqual(comp["stops_avoided"], 8)
        self.assertEqual(comp["wins_removed"], 3)   # o custo aparece junto

    def test_regra_sem_impacto_nao_recebe_credito(self):
        train = _stage(30, 0)
        valid = _stage(30, 0, at=AGORA+timedelta(days=1), offset=100)
        out = r07.build_regime_playbooks(train, valid)
        h1 = next(h for h in out["hypotheses"] if h["id"] == "H1_ABSTAIN_COUNTER_TREND")
        self.assertEqual(h1["status"], r07.R07_NO_INCREMENTAL_CHANGE)
        self.assertEqual(h1["validation"]["operations_removed"], 0)
        self.assertNotIn("stops_avoided", {k: v for k, v in h1.items() if v})

    def test_amostra_vazia_nao_vira_zero_nem_suporte(self):
        out = r07.build_regime_playbooks([], [])
        for h in out["hypotheses"]:
            self.assertEqual(h["status"], r07.R07_INSUFFICIENT)
        self.assertIsNone(out["scenarios"]["train"]["context_coverage_pct"])

    def test_validacao_purgada_nao_prova_ausencia_de_efeito(self):
        rows = _stage(100, 0)
        out = r07.build_regime_playbooks(rows, rows)
        self.assertEqual(out["temporal_purge"]["kept"], 0)
        self.assertTrue(all(h["status"] == r07.R07_INSUFFICIENT for h in out["hypotheses"]))

    def test_unknown_total_e_zero_impacto_sem_cobertura_sao_insuficientes(self):
        for total_unknown in (True, False):
            with self.subTest(total_unknown=total_unknown):
                train = _stage(100, 0)
                valid = _stage(100, 0, at=AGORA+timedelta(days=1), offset=1000)
                for group in (train, valid):
                    for row in group[:100 if total_unknown else 21]:
                        row["features"][r07.R07_CONTEXT_KEY] = None
                out = r07.build_regime_playbooks(train, valid)
                self.assertEqual(out["hypotheses"][0]["status"], r07.R07_INSUFFICIENT)

    def test_treino_insuficiente_nao_vira_suporte_por_validacao_forte(self):
        for n, affected in ((1, 0), (100, 19)):
            with self.subTest(train_n=n, affected=affected):
                train = _stage(n, affected)
                valid = _stage(at=AGORA+timedelta(days=1), offset=1000)
                out = r07.build_regime_playbooks(train, valid)["hypotheses"][0]
                self.assertEqual(out["status"], r07.R07_INSUFFICIENT)

    def test_preservacao_treino_obrigatoria_mesmo_validacao_forte(self):
        train = _stage(100, 40)
        valid = _stage(at=AGORA+timedelta(days=1), offset=1000)
        out = r07.build_regime_playbooks(train, valid)["hypotheses"][0]
        self.assertEqual(out["train"]["operations_preserved_pct"], 60.0)
        self.assertNotEqual(out["status"], r07.R07_VALIDATION_SUPPORTED)
        self.assertTrue(any(not c["passed"] and "treino" in c["name"]
                            and "preserv" in c["name"] for c in out["checks"]))

    def test_dois_estagios_fortes_podem_sustentar_sem_forcar_vencedor(self):
        train = _stage()
        valid = _stage(at=AGORA+timedelta(days=1), offset=1000)
        out = r07.build_regime_playbooks(train, valid)["hypotheses"][0]
        self.assertEqual(out["status"], r07.R07_VALIDATION_SUPPORTED,
                         [(c["name"], c["detail"]) for c in out["checks"] if not c["passed"]])

    def test_amostra_pequena_nao_sustenta(self):
        linhas = [_row(i, "long", "lost", -1.0, _ctx("long", tfs=(("4h", "bearish"),)))
                  for i in range(3)]
        linhas += [_row(50 + i, "long", "won_tp1", 1.0,
                        _ctx("long", tfs=(("4h", "bullish"),))) for i in range(3)]
        out = r07.build_regime_playbooks(linhas, linhas)
        h1 = next(h for h in out["hypotheses"] if h["id"] == "H1_ABSTAIN_COUNTER_TREND")
        self.assertNotEqual(h1["status"], r07.R07_VALIDATION_SUPPORTED)

    def test_hashes_nao_mudam_com_o_resultado(self):
        antes = r07.hypothesis_hashes()
        linhas = [_row(i, "long", "lost", -1.0, _ctx("long", tfs=(("4h", "bearish"),)))
                  for i in range(20)]
        out = r07.build_regime_playbooks(linhas, linhas)
        self.assertEqual(out["hypothesis_hashes"], antes)
        self.assertEqual(r07.hypothesis_hashes(), antes)

    def test_nada_e_promovido_nem_executavel(self):
        linhas = [_row(i, "long", "lost", -1.0, _ctx("long", tfs=(("4h", "bearish"),)))
                  for i in range(20)]
        out = r07.build_regime_playbooks(linhas, linhas)
        self.assertFalse(out["executable"])
        self.assertFalse(out["promotable"])
        self.assertFalse(out["opens_holdout"])
        self.assertEqual(out["holdout_status"], p05.HOLDOUT_SEALED)
        texto = json.dumps(out, default=str)
        for proibido in ("promotion_plan", "StrategyExperiment", "PROMOTED", "ACTIVE"):
            self.assertNotIn(proibido, texto, proibido)
        for h in out["hypotheses"]:
            self.assertIn(h["status"], r07.R07_STATUSES)

    def test_purga_temporal_exclui_o_que_nao_e_posterior(self):
        train = [_row(i, "long", "lost", -1.0, _ctx(),
                      created=AGORA - timedelta(days=20),
                      resolved=AGORA - timedelta(days=10)) for i in range(5)]
        depois = [_row(100 + i, "long", "lost", -1.0, _ctx(),
                       created=AGORA - timedelta(days=5),
                       resolved=AGORA - timedelta(days=1)) for i in range(4)]
        antes = [_row(200 + i, "long", "lost", -1.0, _ctx(),
                      created=AGORA - timedelta(days=15),
                      resolved=AGORA - timedelta(days=2)) for i in range(3)]
        sem_ts = [_row(300, "long", "lost", -1.0, _ctx())]
        sem_ts[0]["created_at"] = None
        mantidos, info = r07.temporal_purge(train, depois + antes + sem_ts)
        self.assertEqual(len(mantidos), 4)
        self.assertEqual(info["purged"], 4)
        self.assertEqual(info["purged_missing_created_at"], 1)
        self.assertTrue(info["applied"])

    def test_purga_sem_borda_completa_do_treino_falha_fechado(self):
        valid = _stage(at=AGORA+timedelta(days=1))
        for train in ([], _stage(2, 1)):
            if train:
                train[0]["resolved_at"] = None
            with self.subTest(train_size=len(train)):
                kept, info = r07.temporal_purge(train, valid)
                self.assertEqual(kept, [])
                self.assertEqual(info["purged"], len(valid))


# ════════════════════════════════════════════════════════════════════════════
#  D. HOLDOUT
# ════════════════════════════════════════════════════════════════════════════
class Holdout(unittest.TestCase):

    def _run_real_loader(self, sealed_value):
        """Executa loader de produção; a sessão só aceita SQL com ids selados."""
        from sqlalchemy.sql import operators, visitors

        owner = self
        calls, materialized = [], []
        allowed = set(range(1, 10))
        boundary = AGORA + timedelta(days=7)
        index = [SimpleNamespace(id=i, outcome_at=(AGORA+timedelta(days=i)
                                                  if i < 7 else boundary))
                 for i in range(1, 13)]

        class SealedRow:
            def __init__(self, row_id):
                self.id = row_id
                self.stored_but_sealed = sealed_value

            def __getattr__(self, name):
                raise AssertionError(f"holdout materializado: {self.id}.{name}")

        details = {}
        for item in index:
            if item.id not in allowed:
                details[item.id] = SealedRow(item.id)
                continue
            ctx = _ctx(captured_at=AGORA)
            details[item.id] = SimpleNamespace(
                id=item.id, symbol="SOLUSDT", timeframe="4h", tier="A",
                direction="long", score=80.0, status="lost", realized_r=-1.0,
                features={r07.R07_CONTEXT_KEY: ctx, "bot_verdict_ok": True},
                stop_distance_pct=1.0, created_at=AGORA, outcome_at=item.outcome_at)

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, stmt):
                columns = tuple(c.key for c in stmt.selected_columns)
                calls.append(columns)
                if len(calls) == 1:
                    owner.assertEqual(columns, ("id", "outcome_at"))
                    return SimpleNamespace(all=lambda: index)
                owner.assertEqual(len(calls), 2, "loader não deve criar consulta extra")
                owner.assertIn("realized_r", columns)
                owner.assertIn("features", columns)
                id_sets = []
                for predicate in stmt._where_criteria:
                    for node in visitors.iterate(predicate):
                        if (getattr(node, "operator", None) is operators.in_op
                                and getattr(getattr(node, "left", None), "key", None) == "id"):
                            id_sets.append(set(node.right.value))
                owner.assertEqual(id_sets, [allowed],
                                  "IDs devem ser limitados no SELECT, antes de .all()")
                # Inspeciona binds compilados do statement real, não string construída no teste.
                owner.assertTrue(any(isinstance(v, (list, tuple, set)) and set(v) == allowed
                                     for v in stmt.compile().params.values()))
                requested = set.intersection(*id_sets)
                owner.assertTrue(requested.isdisjoint({10, 11, 12}))

                def all_details():
                    for row_id in sorted(requested):
                        row = details[row_id]
                        for col in columns:
                            getattr(row, col)  # sentinela falha mesmo se filtro posterior descartasse
                        materialized.append(row_id)
                    return [details[i] for i in sorted(requested)]
                return SimpleNamespace(all=all_details)

        with patch("db.get_session", return_value=FakeSession()):
            loaded = asyncio.run(p05.load_stop_shadow_split(days=365))
        self.assertEqual(len(calls), 2)
        self.assertEqual(materialized, sorted(allowed))
        return loaded

    def test_select_de_detalhes_filtra_por_id_antes_de_materializar(self):
        """O predicado por id tem que estar NA CONSULTA, não só no laço."""
        fonte = _fonte_de_funcao("services/strategy_evidence_service.py",
                                 "load_stop_shadow_split")
        select_detalhe = fonte.split("detail = (await session.execute(")[1].split(")).all()")[0]
        self.assertIn("RS.id.in_(allowed_ids)", select_detalhe)
        # e o filtro está DENTRO do select, antes da materialização
        depois = fonte.split(")).all()")[-1]
        self.assertNotIn("RS.id.in_(allowed_ids)", depois)
        # o split e os demais filtros continuam
        for mantido in ("RS.outcome_at <= boundary", "RS.outcome_at >= since",
                        "_calib._not_fast_void()"):
            self.assertIn(mantido, select_detalhe, mantido)
        # a defesa por id no retorno continua existindo
        self.assertIn("if row.id not in allowed_ids:", fonte)

    def test_empate_na_borda_nao_materializa_o_teste(self):
        loaded = self._run_real_loader(sealed_value=-999)
        self.assertEqual([r["id"] for r in loaded["train"]], list(range(1, 7)))
        self.assertEqual([r["id"] for r in loaded["validation"]], [7, 8, 9])
        self.assertEqual(loaded["test_count"], 3)
        self.assertEqual(loaded["holdout_status"], p05.HOLDOUT_SEALED)
        self.assertFalse(loaded["holdout_outcomes_read"])
        self.assertEqual(loaded["test_bounds"]["first_resolved_at"],
                         loaded["validation"][-1]["resolved_at"].isoformat())

    def test_alterar_o_holdout_nao_muda_a_saida_r07(self):
        first = self._run_real_loader(sealed_value=-999)
        changed = self._run_real_loader(sealed_value=999)
        self.assertEqual(first, changed)
        a = r07.build_regime_playbooks(first["train"], first["validation"])
        b = r07.build_regime_playbooks(changed["train"], changed["validation"])
        self.assertEqual(a, b)

    def test_r07_nao_usa_loaders_que_reabrem_dados(self):
        fonte = (BACKEND / "services" / "regime_playbook_service.py").read_text()
        for proibido in ("_load_shadow", "evaluate_candidate_offline",
                         "load_stop_shadow_split", "get_session", "session.execute",
                         "select("):
            self.assertNotIn(proibido, fonte, proibido)

    def test_r07_nao_escreve_no_banco(self):
        fonte = (BACKEND / "services" / "regime_playbook_service.py").read_text()
        for proibido in ("session.add", "update(", "delete(", "commit("):
            self.assertNotIn(proibido, fonte, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  E. INTEGRAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class Integracao(unittest.TestCase):

    def test_erro_interno_de_hipotese_nao_e_cacheavel(self):
        original = r07.r07_rule_comparison

        def partly_failed(rows, hyp, config, components):
            if hyp == "H1_ABSTAIN_COUNTER_TREND":
                raise ValueError("failure sintética")
            return original(rows, hyp, config, components)

        with patch.object(r07, "r07_rule_comparison", side_effect=partly_failed):
            section = r07.build_regime_playbooks(
                _stage(), _stage(at=AGORA+timedelta(days=1), offset=1000))
        self.assertEqual(section["hypotheses"][0]["status"], r07.R07_UNAVAILABLE)
        self.assertFalse(p05._stop_diagnosis_cacheable({"shadow": {"total_resolved": 200},
                                                       "regime_playbooks": section}))
        # Seções independentes sobrevivem: o erro não deve eliminar H2/H3.
        self.assertEqual(len(section["hypotheses"]), 3)

    def test_erro_da_secao_nao_derruba_o_diagnostico_nem_envenena_o_cache(self):
        ruim = {"shadow": {"total_resolved": 5},
                "regime_playbooks": {"status": "UNAVAILABLE", "error": "x"}}
        self.assertFalse(p05._stop_diagnosis_cacheable(ruim))
        bom = {"shadow": {"total_resolved": 5},
               "regime_playbooks": {"status": "NOT_SUPPORTED"}}
        self.assertTrue(p05._stop_diagnosis_cacheable(bom))

    def test_secao_entra_no_diagnostico_com_os_mesmos_dados(self):
        fonte = _fonte_de_funcao("services/strategy_evidence_service.py",
                                 "build_stop_diagnosis")
        self.assertIn("build_regime_playbooks(train, validation)", fonte)
        # nenhum loader/consulta/cache paralelo
        self.assertNotIn("load_stop_shadow_split(days)", fonte.split(
            "regime_playbook_service")[1])

    def test_nao_cria_rota_endpoint_worker_ou_flag(self):
        fonte = (BACKEND / "services" / "regime_playbook_service.py").read_text()
        for proibido in ("@app.", "APIRouter", "asyncio.create_task", "os.getenv",
                         "ADD COLUMN", "Column(", "mapped_column"):
            self.assertNotIn(proibido, fonte, proibido)

    def test_frontend_mostra_o_essencial(self):
        painel = (BACKEND.parent / "frontend" / "src" / "components"
                  / "AssertivenessPanel.tsx").read_text()
        for texto in ("Playbooks por regime (R07A)", "Cobertura de contexto",
                      "stops removidos", "wins removidos",
                      "desconhecidos excluídos",
                      "Somente análise — nenhuma estratégia foi alterada",
                      "não são operações evitadas"):
            self.assertIn(texto, painel, texto)

    def test_frontend_trata_vazio_e_erro(self):
        painel = (BACKEND.parent / "frontend" / "src" / "components"
                  / "AssertivenessPanel.tsx").read_text()
        bloco = painel.split("Playbooks por regime (R07A)")[1][:5200]
        self.assertIn("Seção indisponível nesta execução", bloco)
        self.assertIn("Nenhuma hipótese pôde ser avaliada", bloco)
        # sem botão e sem ação
        for proibido in ("<button", "onClick", "disabled"):
            self.assertNotIn(proibido, bloco, proibido)

    def test_avaliacao_nao_toca_rede(self):
        linhas = [_row(i, "long", "lost", -1.0, _ctx()) for i in range(5)]
        r07.build_regime_playbooks(linhas, linhas)
        self.assertEqual(_NET_ATTEMPTS, [])

    def test_gravacao_prospectiva_so_no_insert_existente(self):
        fonte = (BACKEND / "services" / "snapshot_service.py").read_text()
        self.assertEqual(fonte.count("_r07_annotation("), 3)   # def + 2 returns
        # a anotação vive dentro do features do insert, não num update
        self.assertNotIn("values(features=_r07", fonte)

    def test_p05_e_readiness_nao_dependem_do_r07(self):
        for nome in ("build_stop_readiness", "build_stop_offline_lab",
                     "evaluate_stop_hypothesis"):
            fonte = _fonte_de_funcao("services/strategy_evidence_service.py", nome)
            self.assertNotIn("regime_playbook", fonte, nome)
            self.assertNotIn("r07", fonte.lower(), nome)


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
