"""P05.2L — Execution Latency Telemetry (observação pura).

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco
externo, Railway, SDK ou credencial real. Nenhum POST é emitido.
"""
from __future__ import annotations

import asyncio
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
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.2L (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import snapshot_service as ss                # noqa: E402
from services import strategy_evidence_service as p05      # noqa: E402


T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _trace(*, created=T0, decision=None, started=None, returned=None,
           persisted=None):
    """Trace com marcas monotônicas controladas (sem dormir)."""
    tr = ss.new_execution_trace(snapshot_id=7, snapshot_created_at=created,
                                now=decision or T0 + timedelta(seconds=30))
    tr["_mono"]["decision_at"] = 100.0
    if started is not None:
        ss.exec_trace_mark(tr, "attempt_started_at",
                           now=T0 + timedelta(seconds=30, milliseconds=started))
        tr["_mono"]["attempt_started_at"] = 100.0 + started / 1000.0
    if returned is not None:
        ss.exec_trace_mark(tr, "attempt_returned_at",
                           now=T0 + timedelta(seconds=30, milliseconds=returned))
        tr["_mono"]["attempt_returned_at"] = 100.0 + returned / 1000.0
    if persisted is not None:
        ts = T0 + timedelta(seconds=30, milliseconds=persisted)
        ss.exec_trace_mark(tr, "real_trade_persisted_at", now=ts)
        tr["_mono"]["real_trade_persisted_at"] = 100.0 + persisted / 1000.0
    return tr


def _full_trace():
    return _trace(started=50, returned=850, persisted=1000)


def _order(**kw):
    base = {"ok": True, "result": {"avgPrice": "100.5", "executedQty": "2"},
            "executed_qty": 2.0}
    result = kw.pop("result", None)
    base.update(kw)
    if result is not None:
        base["result"] = result
    return base


# ════════════════════════════════════════════════════════════════════════════
#  Schema e tempos
# ════════════════════════════════════════════════════════════════════════════
class SchemaETempos(unittest.TestCase):

    def test_schema_versionado_completo(self):
        p = ss.build_execution_path(_full_trace(), status=ss.EXEC_OPEN_PERSISTED,
                                    route="maker", fill_confirmed=True,
                                    fill_price_source="avgPrice")
        for key in ("schema_version", "source", "status", "route", "quality",
                    "snapshot_created_at", "decision_at", "attempt_started_at",
                    "attempt_returned_at", "real_trade_persisted_at",
                    "exchange_event_at", "decision_to_attempt_ms",
                    "attempt_roundtrip_ms", "attempt_to_persist_ms",
                    "end_to_end_ms", "fill_confirmed", "fill_price_source",
                    "reason_code", "missing_fields", "recorded_at", "limitations"):
            self.assertIn(key, p)
        self.assertEqual(p["schema_version"], 1)
        self.assertEqual(p["source"], "LIVE_ENTRY_CALLER")

    def test_timestamps_em_utc(self):
        p = ss.build_execution_path(_full_trace(), status=ss.EXEC_OPEN_PERSISTED)
        for key in ("decision_at", "attempt_started_at", "attempt_returned_at",
                    "real_trade_persisted_at", "recorded_at"):
            self.assertTrue(p[key].endswith("+00:00"), key)

    def test_duracoes_pelo_relogio_monotonico(self):
        p = ss.build_execution_path(_full_trace(), status=ss.EXEC_OPEN_PERSISTED,
                                    fill_confirmed=True)
        self.assertAlmostEqual(p["decision_to_attempt_ms"], 50.0, places=1)
        self.assertAlmostEqual(p["attempt_roundtrip_ms"], 800.0, places=1)
        self.assertAlmostEqual(p["attempt_to_persist_ms"], 150.0, places=1)

    def test_end_to_end_usa_timestamps_duraveis(self):
        p = ss.build_execution_path(_full_trace(), status=ss.EXEC_OPEN_PERSISTED,
                                    fill_confirmed=True)
        # snapshot criado em T0, persistência em T0+30s+1000ms
        self.assertAlmostEqual(p["end_to_end_ms"], 31000.0, places=1)

    def test_duracao_negativa_vira_null(self):
        tr = _trace(started=800, returned=50)
        p = ss.build_execution_path(tr, status=ss.EXEC_SUBMISSION_UNKNOWN)
        self.assertIsNone(p["attempt_roundtrip_ms"])
        self.assertIn("attempt_roundtrip_ms", p["missing_fields"])
        self.assertNotEqual(p["attempt_roundtrip_ms"], 0)

    def test_nan_e_infinito_viram_null(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            tr = _full_trace()
            tr["_mono"]["attempt_returned_at"] = bad
            p = ss.build_execution_path(tr, status=ss.EXEC_SUBMISSION_UNKNOWN)
            self.assertIsNone(p["attempt_roundtrip_ms"])

    def test_timestamp_invertido_no_end_to_end_vira_null(self):
        tr = _trace(created=T0 + timedelta(days=1), started=10, returned=20,
                    persisted=30)
        p = ss.build_execution_path(tr, status=ss.EXEC_OPEN_PERSISTED,
                                    fill_confirmed=True)
        self.assertIsNone(p["end_to_end_ms"])
        self.assertIn("end_to_end_ms", p["missing_fields"])

    def test_marca_ausente_nao_vira_zero(self):
        p = ss.build_execution_path(_trace(), status=ss.EXEC_NOT_SUBMITTED)
        self.assertIsNone(p["attempt_started_at"])
        self.assertIsNone(p["attempt_roundtrip_ms"])
        self.assertEqual(p["quality"], "COMPLETE")     # NOT_SUBMITTED não espera

    def test_qualidade_partial_e_unavailable(self):
        p = ss.build_execution_path(_trace(started=10),
                                    status=ss.EXEC_SUBMISSION_UNKNOWN)
        self.assertEqual(p["quality"], "PARTIAL")
        p2 = ss.build_execution_path({}, status="STATUS_INVENTADO")
        self.assertEqual(p2["status"], "UNAVAILABLE")
        self.assertEqual(p2["quality"], "UNAVAILABLE")

    def test_mark_e_no_op_sem_trace(self):
        ss.exec_trace_mark(None, "attempt_started_at")
        tr = _full_trace()
        antes = dict(tr)
        ss.exec_trace_mark(tr, "campo_inexistente")
        self.assertEqual(tr["attempt_started_at"], antes["attempt_started_at"])

    def test_nada_secreto_no_payload(self):
        res = _order(result={"avgPrice": "100.5", "executedQty": "2",
                             "orderId": 999, "signature": "SEGREDO",
                             "apiKey": "CHAVE"})
        info = ss.classify_execution_attempt(res, attempted=True)
        p = ss.build_execution_path(_full_trace(), status=info["status"],
                                    fill_confirmed=info["fill_confirmed"],
                                    fill_price_source=info["fill_price_source"],
                                    reason_code=info["reason_code"])
        blob = repr(p)
        for segredo in ("SEGREDO", "CHAVE", "signature", "apiKey", "orderId",
                        "999"):
            self.assertNotIn(segredo, blob)


# ════════════════════════════════════════════════════════════════════════════
#  Classificação do resultado
# ════════════════════════════════════════════════════════════════════════════
class Classificacao(unittest.TestCase):

    def test_not_submitted_por_preflight(self):
        info = ss.classify_execution_attempt(
            {"ok": False, "preflight_failed": True,
             "reason_code": "EXEC_PREFLIGHT_BLOCKED"}, attempted=True)
        self.assertEqual(info["status"], "NOT_SUBMITTED")
        self.assertEqual(info["reason_code"], "EXEC_PREFLIGHT_BLOCKED")
        self.assertIs(info["fill_confirmed"], False)

    def test_not_submitted_sem_tentativa(self):
        info = ss.classify_execution_attempt({}, attempted=False)
        self.assertEqual(info["status"], "NOT_SUBMITTED")
        self.assertEqual(info["reason_code"], "ENTRY_NOT_ATTEMPTED")

    def test_no_fill(self):
        info = ss.classify_execution_attempt(
            {"ok": False, "no_fill": True}, attempted=True)
        self.assertEqual(info["status"], "NO_FILL")
        self.assertIs(info["fill_confirmed"], False)

    def test_submission_unknown_por_estado_critico(self):
        for flag in ("manual_intervention_required", "quarantine_required",
                     "emergency_close_attempted", "pending_entry_order",
                     "final_fill_qty_unknown"):
            info = ss.classify_execution_attempt(
                {"ok": True, flag: True}, attempted=True)
            self.assertEqual(info["status"], "SUBMISSION_UNKNOWN", flag)

    def test_fill_confirmed_exige_evidencia(self):
        info = ss.classify_execution_attempt(_order(), attempted=True)
        self.assertEqual(info["status"], "FILL_CONFIRMED")
        self.assertIs(info["fill_confirmed"], True)
        self.assertEqual(info["fill_price_source"], "avgPrice")

    def test_fill_nao_e_inferido_de_ok_true(self):
        info = ss.classify_execution_attempt({"ok": True, "result": {}},
                                             attempted=True)
        self.assertEqual(info["status"], "SUBMISSION_UNKNOWN")
        self.assertIsNone(info["fill_confirmed"])
        self.assertEqual(info["reason_code"], "FILL_NOT_AUDITABLE")

    def test_avg_sem_qty_executada_fica_unknown(self):
        info = ss.classify_execution_attempt(
            {"ok": True, "result": {"avgPrice": "100"}}, attempted=True)
        self.assertIsNone(info["fill_confirmed"])
        self.assertEqual(info["status"], "SUBMISSION_UNKNOWN")

    def test_avg_fill_price_tambem_conta(self):
        info = ss.classify_execution_attempt(
            {"ok": True, "executed_qty": 3,
             "result": {"avgFillPrice": "50.2", "executedQty": "3"}},
            attempted=True)
        self.assertEqual(info["fill_price_source"], "avgFillPrice")
        self.assertIs(info["fill_confirmed"], True)

    def test_open_persisted(self):
        info = ss.classify_execution_attempt(_order(), attempted=True,
                                             trade_persisted=True)
        self.assertEqual(info["status"], "OPEN_PERSISTED")

    def test_persistence_failed_so_com_fill_conhecido(self):
        info = ss.classify_execution_attempt(_order(), attempted=True,
                                             trade_persisted=False)
        self.assertEqual(info["status"], "PERSISTENCE_FAILED")
        semfill = ss.classify_execution_attempt({"ok": True, "result": {}},
                                                attempted=True,
                                                trade_persisted=False)
        self.assertEqual(semfill["status"], "SUBMISSION_UNKNOWN")

    def test_unavailable_para_status_invalido(self):
        p = ss.build_execution_path(_full_trace(), status="QUALQUER_COISA")
        self.assertEqual(p["status"], "UNAVAILABLE")

    def test_exchange_timestamp_ausente_permanece_null(self):
        self.assertIsNone(ss.exchange_event_at_from({"result": {}}))
        self.assertIsNone(ss.exchange_event_at_from({"result": {"updateTime": 5}}))
        self.assertIsNone(ss.exchange_event_at_from({}))

    def test_exchange_timestamp_valido_e_convertido(self):
        ms = int(T0.timestamp() * 1000)
        out = ss.exchange_event_at_from({"result": {"updateTime": ms}})
        self.assertEqual(out, T0.isoformat())

    def test_route_maker_market_e_fallback(self):
        self.assertEqual(ss.execution_route("maker", {}), "maker")
        self.assertEqual(ss.execution_route("market", {}), "market")
        self.assertEqual(ss.execution_route("maker", {"was_maker": True}), "maker")
        self.assertEqual(
            ss.execution_route("maker", {"fell_back_to_market": True}),
            "maker_fallback_market")
        self.assertEqual(ss.execution_route(None, {}), "unknown")


# ════════════════════════════════════════════════════════════════════════════
#  Merge JSONB sem perda
# ════════════════════════════════════════════════════════════════════════════
class MergeSemPerda(unittest.TestCase):

    def _payload(self, status=ss.EXEC_OPEN_PERSISTED):
        return ss.build_execution_path(_full_trace(), status=status,
                                       fill_confirmed=True)

    def test_merge_preserva_p05_path_e_p05_context(self):
        feats = {"p05_path": {"mae_r": 0.4, "observed_candles": 12},
                 "p05_context": {"regime": "TREND"}, "rsi": 55}
        out = ss.merge_execution_path(feats, self._payload())
        self.assertEqual(out["p05_path"]["mae_r"], 0.4)
        self.assertEqual(out["p05_context"]["regime"], "TREND")
        self.assertEqual(out["rsi"], 55)
        self.assertIn("p05_execution_path", out)
        self.assertNotIn("p05_execution_path", feats)   # não muta a entrada

    def test_p051t_preserva_execution_path(self):
        feats = {"p05_execution_path": self._payload()}
        snap = SimpleNamespace(entry=100.0, stop_loss=98.0, direction="long",
                               features=feats, created_at=T0, id=1)
        trace = ss.new_path_trace(snap)
        ss.path_observe_candle(trace, int((T0 + timedelta(minutes=5)).timestamp() * 1000),
                               101.0, 99.5)
        out = ss.merge_p05_path(snap.features, trace)
        self.assertIn("p05_execution_path", out)
        self.assertIn("p05_path", out)
        self.assertIsNotNone(out["p05_path"]["mfe_r"])

    def test_ambas_sobrevivem_independentemente_da_ordem(self):
        payload = self._payload()
        snap = SimpleNamespace(entry=100.0, stop_loss=98.0, direction="long",
                               features={}, created_at=T0, id=1)
        trace = ss.new_path_trace(snap)
        ss.path_observe_candle(trace, int((T0 + timedelta(minutes=5)).timestamp() * 1000),
                               101.0, 99.5)
        # ordem A: telemetria de execução primeiro
        a = ss.merge_p05_path(ss.merge_execution_path({}, payload), trace)
        # ordem B: MAE/MFE primeiro
        b = ss.merge_execution_path(ss.merge_p05_path({}, trace), payload)
        for out in (a, b):
            self.assertIn("p05_path", out)
            self.assertIn("p05_execution_path", out)
        self.assertEqual(a["p05_execution_path"]["status"],
                         b["p05_execution_path"]["status"])
        self.assertEqual(a["p05_path"]["mfe_r"], b["p05_path"]["mfe_r"])

    def test_trace_incompleto_nao_apaga_completo(self):
        completo = self._payload(ss.EXEC_OPEN_PERSISTED)
        incompleto = ss.build_execution_path(_trace(), status=ss.EXEC_NOT_SUBMITTED)
        out = ss.merge_execution_path({"p05_execution_path": completo}, incompleto)
        self.assertEqual(out["p05_execution_path"]["status"], "OPEN_PERSISTED")

    def test_trace_mais_completo_substitui(self):
        incompleto = ss.build_execution_path(_trace(), status=ss.EXEC_NOT_SUBMITTED)
        completo = self._payload(ss.EXEC_OPEN_PERSISTED)
        out = ss.merge_execution_path({"p05_execution_path": incompleto}, completo)
        self.assertEqual(out["p05_execution_path"]["status"], "OPEN_PERSISTED")

    def test_retry_idempotente(self):
        payload = self._payload()
        a = ss.merge_execution_path({}, payload)
        b = ss.merge_execution_path(a, payload)
        self.assertEqual(a["p05_execution_path"]["status"],
                         b["p05_execution_path"]["status"])
        self.assertEqual(a["p05_execution_path"]["completeness_rank"],
                         b["p05_execution_path"]["completeness_rank"])

    def test_ranking_de_completude(self):
        ranks = ss._EXEC_RANK
        self.assertLess(ranks["NOT_SUBMITTED"], ranks["FILL_CONFIRMED"])
        self.assertLess(ranks["FILL_CONFIRMED"], ranks["OPEN_PERSISTED"])
        self.assertEqual(ranks["UNAVAILABLE"], 0)

    def test_payload_invalido_nao_altera_features(self):
        feats = {"p05_path": {"mae_r": 1.0}}
        self.assertEqual(ss.merge_execution_path(feats, None), feats)
        self.assertEqual(ss.merge_execution_path(feats, "x"), feats)


# ════════════════════════════════════════════════════════════════════════════
#  Persistência atômica
# ════════════════════════════════════════════════════════════════════════════
class PersistenciaAtomica(unittest.TestCase):

    def test_sem_db_e_no_op(self):
        with patch.object(ss, "DB_ENABLED", False):
            out = asyncio.run(ss.persist_execution_path(1, {"a": 1}))
        self.assertEqual(out, "SKIPPED")

    def test_sem_snapshot_id_e_no_op(self):
        with patch.object(ss, "DB_ENABLED", True):
            self.assertEqual(asyncio.run(ss.persist_execution_path(None, {})), "SKIPPED")

    def test_update_e_atomico_por_chave(self):
        capturado = {}

        class _Sess:
            async def execute(self, stmt):
                capturado["sql"] = str(stmt.compile(
                    compile_kwargs={"literal_binds": True}))
                return SimpleNamespace(rowcount=1)

            async def commit(self):
                capturado["commit"] = True

        class _Ctx:
            async def __aenter__(self):
                return _Sess()

            async def __aexit__(self, *a):
                return False

        payload = ss.build_execution_path(_full_trace(),
                                          status=ss.EXEC_OPEN_PERSISTED,
                                          fill_confirmed=True)
        with patch.object(ss, "DB_ENABLED", True), \
                patch.object(ss, "get_session", lambda: _Ctx()):
            out = asyncio.run(ss.persist_execution_path(42, payload))
        self.assertEqual(out, "WRITTEN")
        sql = capturado["sql"]
        self.assertIn("UPDATE", sql.upper())
        self.assertIn("||", sql)                       # merge JSONB no servidor
        self.assertIn("p05_execution_path", sql)
        self.assertNotIn("p05_path", sql.replace("p05_execution_path", ""))
        self.assertIn("completeness_rank", sql)

    def test_rank_menor_nao_sobrescreve(self):
        class _Sess:
            async def execute(self, stmt):
                return SimpleNamespace(rowcount=0)     # WHERE do rank barrou

            async def commit(self):
                pass

        class _Ctx:
            async def __aenter__(self):
                return _Sess()

            async def __aexit__(self, *a):
                return False

        payload = ss.build_execution_path(_trace(), status=ss.EXEC_NOT_SUBMITTED)
        with patch.object(ss, "DB_ENABLED", True), \
                patch.object(ss, "get_session", lambda: _Ctx()):
            out = asyncio.run(ss.persist_execution_path(42, payload))
        self.assertEqual(out, "SKIPPED_NOT_NEWER")

    def test_falha_de_persistencia_e_fail_soft(self):
        class _Ctx:
            async def __aenter__(self):
                raise RuntimeError("banco fora")

            async def __aexit__(self, *a):
                return False

        with patch.object(ss, "DB_ENABLED", True), \
                patch.object(ss, "get_session", lambda: _Ctx()):
            out = asyncio.run(ss.persist_execution_path(1, {"completeness_rank": 1}))
        self.assertEqual(out, "FAILED")

    def test_stage_merge_converte_em_expressao(self):
        snap = SimpleNamespace(id=9, features={"p05_path": {"mae_r": 0.2},
                                               "p05_context": {"regime": "X"}})
        with patch.object(ss, "DB_ENABLED", True):
            ok = ss.stage_feature_namespace_merge(snap)
        self.assertTrue(ok)
        self.assertNotIsInstance(snap.features, dict)
        sql = str(snap.features.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("||", sql)
        self.assertIn("p05_path", sql)
        self.assertNotIn("p05_context", sql)   # só a chave alvo é gravada

    def test_stage_merge_e_no_op_sem_db_ou_sem_chave(self):
        snap = SimpleNamespace(id=9, features={"p05_path": {}})
        with patch.object(ss, "DB_ENABLED", False):
            self.assertFalse(ss.stage_feature_namespace_merge(snap))
        self.assertIsInstance(snap.features, dict)
        snap2 = SimpleNamespace(id=None, features={"p05_path": {}})
        with patch.object(ss, "DB_ENABLED", True):
            self.assertFalse(ss.stage_feature_namespace_merge(snap2))
        snap3 = SimpleNamespace(id=1, features={})
        with patch.object(ss, "DB_ENABLED", True):
            self.assertFalse(ss.stage_feature_namespace_merge(snap3))


# ════════════════════════════════════════════════════════════════════════════
#  Resumo analítico
# ════════════════════════════════════════════════════════════════════════════
class ResumoAnalitico(unittest.TestCase):

    def _row(self, i, payload=None, path=None):
        feats = {}
        if payload is not None:
            feats["p05_execution_path"] = payload
        if path is not None:
            feats["p05_path"] = path
        return {"id": i, "features": feats}

    def _payload(self, status=ss.EXEC_OPEN_PERSISTED, **kw):
        return ss.build_execution_path(_full_trace(), status=status,
                                       route=kw.pop("route", "maker"),
                                       fill_confirmed=kw.pop("fill", True),
                                       fill_price_source="avgPrice", **kw)

    def test_unavailable_sem_observacao(self):
        out = p05.summarize_latency([self._row(1)])
        self.assertEqual(out["status"], "UNAVAILABLE")
        self.assertEqual(out["attempts_observed"], 0)
        self.assertIsNone(out["coverage_pct"])
        self.assertIsNone(out["end_to_end_ms"]["median"])

    def test_collecting_com_poucas_observacoes(self):
        rows = [self._row(i, self._payload()) for i in range(5)]
        out = p05.summarize_latency(rows)
        self.assertEqual(out["status"], "COLLECTING")
        self.assertEqual(out["attempts_observed"], 5)
        self.assertEqual(out["coverage_pct"], 100.0)

    def test_usable_com_amostra_e_cobertura(self):
        rows = [self._row(i, self._payload()) for i in range(30)]
        out = p05.summarize_latency(rows)
        self.assertEqual(out["status"], "USABLE")
        self.assertEqual(out["by_status"]["OPEN_PERSISTED"], 30)
        self.assertEqual(out["by_route"]["maker"], 30)
        self.assertEqual(out["fill_auditable"], 30)

    def test_cobertura_baixa_mantem_collecting(self):
        rows = [self._row(i, self._payload()) for i in range(20)]
        parcial = ss.build_execution_path(_trace(started=10),
                                          status=ss.EXEC_SUBMISSION_UNKNOWN)
        rows += [self._row(100 + i, parcial) for i in range(15)]
        out = p05.summarize_latency(rows)
        self.assertEqual(out["status"], "COLLECTING")
        self.assertLess(out["coverage_pct"], 80.0)
        self.assertEqual(out["by_quality"]["PARTIAL"], 15)

    def test_missing_nao_vira_zero(self):
        parcial = ss.build_execution_path(_trace(), status=ss.EXEC_SUBMISSION_UNKNOWN)
        out = p05.summarize_latency([self._row(1, parcial)])
        self.assertIsNone(out["attempt_roundtrip_ms"]["median"])
        self.assertEqual(out["attempt_roundtrip_ms"]["count"], 0)
        self.assertTrue(out["missing_by_reason"])

    def test_distribuicoes_com_mediana_p75_p90(self):
        rows = [self._row(i, self._payload()) for i in range(30)]
        out = p05.summarize_latency(rows)
        for key in ("decision_to_attempt_ms", "attempt_roundtrip_ms",
                    "attempt_to_persist_ms", "end_to_end_ms"):
            self.assertIn("median", out[key])
            self.assertIn("p75", out[key])
            self.assertIn("p90", out[key])
        self.assertAlmostEqual(out["attempt_roundtrip_ms"]["median"], 800.0, places=1)

    def test_sem_timestamp_da_exchange_e_contado(self):
        out = p05.summarize_latency([self._row(1, self._payload())])
        self.assertEqual(out["without_exchange_timestamp"], 1)

    def test_trace_invalido_vira_missing(self):
        out = p05.summarize_latency([self._row(1, {"schema_version": 99})])
        self.assertEqual(out["attempts_observed"], 0)
        self.assertTrue(out["missing_by_reason"])

    def test_slippage_reutiliza_real_trade(self):
        out = p05.summarize_latency([self._row(1, self._payload())])
        self.assertIn("RealTrade.entry_slippage_pct", out["slippage_source"])
        self.assertNotIn("slippage_pct", out)

    def test_ligacao_com_real_trade(self):
        rows = [self._row(7, self._payload())]
        reais = [{"recommendation_id": 7}, {"recommendation_id": 999},
                 {"recommendation_id": None}]
        out = p05.summarize_latency(rows, reais)
        self.assertEqual(out["linked_real_trades"], 1)

    def test_nao_correlaciona_com_lucro(self):
        out = p05.summarize_latency([self._row(1, self._payload())])
        blob = repr(out)
        for proibido in ("expectancy", "realized_r", "win_rate", "profit"):
            self.assertNotIn(proibido, blob)


# ════════════════════════════════════════════════════════════════════════════
#  Hot path
# ════════════════════════════════════════════════════════════════════════════
class HotPath(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        cls.snap = (BACKEND / "services" / "snapshot_service.py").read_text()

    def test_marcas_envolvem_a_chamada_de_entrada(self):
        self.assertIn('_exec_mark(_exec_trace, "attempt_started_at")\n'
                      '                    order_res = await _maker_fn(', self.src)
        self.assertIn('_exec_mark(_exec_trace, "attempt_started_at")\n'
                      '                        order_res = await exchange_service.place_order(',
                      self.src)
        self.assertIn('_exec_mark(_exec_trace, "attempt_returned_at")', self.src)

    def test_trace_nasce_depois_dos_gates_e_antes_da_ordem(self):
        pos_trace = self.src.index("new_execution_trace(")
        pos_order = self.src.index("# 2. Exchange order")
        self.assertLess(pos_trace, pos_order)

    def test_telemetria_nao_altera_order_res(self):
        block = self.src.split("async def _record_entry_latency")[1].split("\n\n\n")[0]
        for proibido in ("order_res =", "order_res[", "qty =", "return order_res",
                         "raise ", "continue", "place_order", "cancel"):
            self.assertNotIn(proibido, block, f"hot path alterado: {proibido}")

    def test_recorder_e_fail_soft(self):
        block = self.src.split("async def _record_entry_latency")[1].split("\n\n\n")[0]
        self.assertIn("except Exception", block)
        self.assertIn("log.debug", block)

    def test_nenhuma_chamada_adicional_a_exchange(self):
        block = self.src.split("async def _record_entry_latency")[1].split("\n\n\n")[0]
        for proibido in ("exchange_service", "ccxt", "requests", "aiohttp",
                         "httpx", "POST", "get_execution_quote",
                         "get_execution_depth"):
            self.assertNotIn(proibido, block)

    def test_instrumenta_somente_a_entrada_live(self):
        for fn in ("async def _execute_flip", "async def _execute_tf_upgrade",
                   "async def _maybe_pyramid", "async def _maybe_regime_hedge"):
            block = self.src.split(fn)[1].split("\nasync def ")[0]
            self.assertNotIn("_record_entry_latency", block, fn)
            self.assertNotIn("new_execution_trace", block, fn)

    def test_persistencia_marcada_apos_open_trade(self):
        block = self.src.split("trade = await _open_trade_fail_closed(")[-1][:2600]
        self.assertIn('_exec_mark(_exec_trace, "real_trade_persisted_at")', block)
        self.assertIn("trade_persisted=trade is not None", block)
        self.assertIn('source == "auto"', block)

    def test_nenhuma_env_ou_flag_nova(self):
        bloco = self.snap.split("P05.2L — EXECUTION LATENCY TELEMETRY")[1].split(
            "async def _current_regime_label")[0]
        self.assertNotIn("os.getenv", bloco)
        self.assertNotIn("P052L_ENABLED", self.src)

    def test_nenhuma_tabela_ou_coluna_nova(self):
        bloco = self.snap.split("P05.2L — EXECUTION LATENCY TELEMETRY")[1].split(
            "async def _current_regime_label")[0]
        for proibido in ("class ", "Column(", "mapped_column", "CREATE TABLE",
                         "ALTER TABLE", "alembic"):
            self.assertNotIn(proibido, bloco)
        self.assertIn("p05_execution_path", bloco)

    def test_sem_endpoint_novo(self):
        main = (BACKEND / "main.py").read_text()
        self.assertNotIn("p05.2l", main.lower())
        self.assertNotIn("execution-latency", main)

    def test_resolver_usa_merge_atomico(self):
        self.assertGreaterEqual(self.snap.count("stage_feature_namespace_merge(snap)"), 4)


class FrontendLatencia(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        panel = (BACKEND.parent / "frontend" / "src" / "components"
                 / "AssertivenessPanel.tsx")
        cls.src = panel.read_text()
        cls.block = cls.src.split("P05.2L Latência da entrada real")[-1].split(
            "Medido em ")[0]

    def test_secao_existe(self):
        self.assertIn("Latência da entrada real", self.src)

    def test_texto_obrigatorio(self):
        self.assertIn("Essa medição descreve o caminho técnico da entrada",
                      self.block)
        self.assertIn("ainda não prova que a", self.block)

    def test_sem_botao(self):
        for proibido in ("<button", "onClick", "Aplicar", "Executar", "Testar agora"):
            self.assertNotIn(proibido, self.block)

    def test_mostra_campos_exigidos(self):
        for campo in ("Entradas medidas", "Duração da chamada",
                      "Recomendação → registro", "Fills auditáveis",
                      "Registros incompletos", "cobertura"):
            self.assertIn(campo, self.block)

    def test_dist_nao_foi_editado(self):
        dist = BACKEND.parent / "frontend" / "dist" / "assets"
        if dist.exists():
            for f in dist.glob("*.js"):
                self.assertNotIn("Latência da entrada real",
                                 f.read_text(errors="ignore"))


if __name__ == "__main__":
    unittest.main()
