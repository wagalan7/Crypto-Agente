"""
P03 — Reconciliação persistente de execução.

Testes herméticos: nenhuma rede real, nenhuma Binance real, nenhum banco real,
nenhuma entry criada. O repositório de incidentes usa a implementação em memória
(mesma semântica de claim/lease do SQL) e as chamadas de exchange são mockadas
via patch.object no módulo real binance_signed_service (mesmo padrão do P02).

Cobre: persistência/idempotência, restart lógico, claim/lease, MARKET/MAKER
UNKNOWN, freshness de posição, cleanup de condicionais, boot, pausa/quarentena,
concorrência, falha de persistência e a API read-only.
"""
from __future__ import annotations

import sys
import types
import asyncio
import unittest
from pathlib import Path
from datetime import timedelta
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import services.execution_reconciliation_service as ers  # noqa: E402
import services.binance_signed_service as bss  # noqa: E402  (httpx-only; importa limpo)
from services.execution_reconciliation_service import (  # noqa: E402
    Kind, State, InMemoryIncidentRepo, terminal_qty, classify_entry, position_verdict,
)


def _patch_bss(**fns) -> ExitStack:
    """Faz patch.object das funções necessárias no binance_signed_service real."""
    stack = ExitStack()
    for name, val in fns.items():
        stack.enter_context(patch.object(bss, name, val))
    return stack


def _install_fake_services():
    """Fakes leves de shadow_trade_service e risk_service (evita pandas/sqlalchemy)
    para exercitar a quarentena REAL do P03 (latch + pausa)."""
    shadow = types.ModuleType("services.shadow_trade_service")
    st = {"reason": None}
    shadow._arm_execution_quarantine = lambda r: st.__setitem__("reason", st["reason"] or r)
    shadow.clear_execution_quarantine = lambda: st.__setitem__("reason", None)
    shadow.execution_quarantine_reason = lambda: st["reason"]

    risk = types.ModuleType("services.risk_service")
    rs = {"paused": False, "reason": None}

    async def set_manual_pause(paused, reason=None):
        rs["paused"] = paused
        rs["reason"] = reason if paused else None
        return {"trading_paused": paused, "pause_reason": rs["reason"]}

    async def get_status():
        return {"enabled": True, "trading_paused": rs["paused"], "pause_reason": rs["reason"]}

    async def is_paused():
        return rs["paused"]

    risk.set_manual_pause = set_manual_pause
    risk.get_status = get_status
    risk.is_paused = is_paused

    import services as pkg
    sys.modules["services.shadow_trade_service"] = shadow
    sys.modules["services.risk_service"] = risk
    pkg.shadow_trade_service = shadow
    pkg.risk_service = risk
    return shadow, risk, st, rs


def _uninstall_fake_services():
    import services as pkg
    for name in ("shadow_trade_service", "risk_service"):
        sys.modules.pop(f"services.{name}", None)
        if hasattr(pkg, name):
            try:
                delattr(pkg, name)
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
#  1) Núcleo de decisão puro (qty terminal, classify, freshness)
# ════════════════════════════════════════════════════════════════════════════
class DecisionCoreTests(unittest.TestCase):
    def test_filled_uses_confirmed_qty(self):
        qty, why = terminal_qty({"ok": True, "status": "FILLED", "orig_qty": 2.0, "executed_qty": 2.0})
        self.assertEqual(qty, 2.0)
        self.assertEqual(why, "filled")

    def test_rejected_is_zero_qty(self):
        qty, _ = terminal_qty({"ok": True, "status": "REJECTED", "raw": {}})
        self.assertEqual(qty, 0.0)

    def test_canceled_requires_explicit_executed_qty(self):
        qty, _ = terminal_qty({"ok": True, "status": "CANCELED", "raw": {}, "executed_qty": None})
        self.assertIsNone(qty)                       # sem executedQty → UNKNOWN, não zero
        qty2, _ = terminal_qty({"ok": True, "status": "CANCELED", "raw": {"executedQty": "0.5"},
                                "executed_qty": 0.5})
        self.assertEqual(qty2, 0.5)

    def test_partial_before_terminal_is_lower_bound_not_final(self):
        qty, _ = terminal_qty({"ok": True, "status": "PARTIALLY_FILLED", "executed_qty": 0.3, "raw": {}})
        self.assertIsNone(qty)
        verdict, q, _ = classify_entry({"ok": True, "status": "PARTIALLY_FILLED", "executed_qty": 0.3})
        self.assertEqual(verdict, "RETRY")
        self.assertIsNone(q)

    def test_classify_filled_to_protected_and_rejected_to_flat(self):
        v1, q1, _ = classify_entry({"ok": True, "status": "FILLED", "orig_qty": 1.0})
        self.assertEqual((v1, q1), ("PROTECTED", 1.0))
        v2, q2, _ = classify_entry({"ok": True, "status": "REJECTED", "raw": {}})
        self.assertEqual((v2, q2), ("FLAT", 0.0))

    def test_classify_inconclusive_keeps_retry(self):
        v, _, _ = classify_entry({"ok": False, "error": "down"})
        self.assertEqual(v, "RETRY")

    def test_terminal_without_qty_is_final_fill_unknown(self):
        v, _, _ = classify_entry({"ok": True, "status": "EXPIRED", "raw": {}, "executed_qty": None})
        self.assertEqual(v, "FILL_UNKNOWN")

    def test_position_verdict_none_is_unknown_not_flat(self):
        self.assertEqual(position_verdict(None, "stale"), "UNKNOWN")
        self.assertEqual(position_verdict(0.0, "ok"), "FLAT")
        self.assertEqual(position_verdict(1.5, "ok"), "OPEN")


# ════════════════════════════════════════════════════════════════════════════
#  Base assíncrona: repo em memória + quarentena mockada
# ════════════════════════════════════════════════════════════════════════════
class _AsyncBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        self._arm = patch.object(ers, "_arm_quarantine", AsyncMock())
        self._rel = patch.object(ers, "_maybe_release_quarantine", AsyncMock(return_value=False))
        self._arm.start()
        self._rel.start()

    def tearDown(self):
        self._arm.stop()
        self._rel.stop()
        ers.set_repo(None)


# ════════════════════════════════════════════════════════════════════════════
#  2) Persistência / idempotência / restart / claim / concorrência
# ════════════════════════════════════════════════════════════════════════════
class PersistenceTests(_AsyncBase):
    async def test_incident_created_once_and_idempotent(self):
        r1 = await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                       client_order_id="c1")
        r2 = await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                       client_order_id="c1", min_known_fill=0.5)
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        rows = await ers._get_repo().list_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["min_known_fill"], 0.5)
        self.assertGreaterEqual(ers._arm_quarantine.await_count, 1)

    async def test_two_persistences_same_event_one_record(self):
        await asyncio.gather(
            ers.record_incident(kind=Kind.CLEANUP_PENDING, symbol="ETH/USDT:USDT", entry_order_id="e9"),
            ers.record_incident(kind=Kind.CLEANUP_PENDING, symbol="ETH/USDT:USDT", entry_order_id="e9"),
        )
        self.assertEqual(len(await ers._get_repo().list_all()), 1)

    async def test_restart_recovers_incident(self):
        repo = ers._get_repo()
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="SOL/USDT:USDT",
                                  client_order_id="c2")
        self.assertEqual(len(await repo.list_open()), 1)   # boot lê a mesma store

    async def test_expired_claim_recovered(self):
        repo = ers._get_repo()
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="X/USDT:USDT", client_order_id="k")
        key = (await repo.list_open())[0]["incident_key"]
        await repo.claim(key, "other-proc", ers._now() - timedelta(seconds=1))
        self.assertEqual(await repo.recover_expired_claims(), 1)
        self.assertIsNone((await repo.get(key))["claimed_by"])

    async def test_two_reconcilers_do_not_process_same_incident(self):
        repo = ers._get_repo()
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="Y/USDT:USDT", client_order_id="z")
        key = (await repo.list_open())[0]["incident_key"]
        lease = ers._now() + timedelta(seconds=60)
        self.assertTrue(await repo.claim(key, "proc-A", lease))
        self.assertFalse(await repo.claim(key, "proc-B", lease))


# ════════════════════════════════════════════════════════════════════════════
#  3) MARKET/ENTRY UNKNOWN — nunca reenvia entry
# ════════════════════════════════════════════════════════════════════════════
class EntryReconcileTests(_AsyncBase):
    async def _seed(self, **kw):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  client_order_id="c1", side="buy", planned_stop=100.0, **kw)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    async def test_timeout_then_filled_protected_no_entry_resend(self):
        key = await self._seed()
        with _patch_bss(
            get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
            _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
            place_order=AsyncMock(), place_maker_entry_then_protect=AsyncMock(),
        ), patch.object(ers, "_ensure_stop", AsyncMock(return_value=(True, "sl vivo"))):
            await ers._reconcile_one(key)
            row = await ers._get_repo().get(key)
            self.assertEqual(row["state"], State.PROTECTED)
            self.assertIsNotNone(row["resolved_at"])
            bss.place_order.assert_not_called()
            bss.place_maker_entry_then_protect.assert_not_called()

    async def test_get_inconclusive_keeps_pause(self):
        key = await self._seed()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": False, "error": "down"})):
            await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        self.assertEqual(row["state"], State.RETRY_PENDING)
        self.assertIsNone(row["resolved_at"])

    async def test_rejected_resolves_flat(self):
        key = await self._seed()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "REJECTED", "raw": {}})):
            await ers._reconcile_one(key)
        self.assertEqual((await ers._get_repo().get(key))["state"], State.FLAT)

    async def test_terminal_without_qty_keeps_final_fill_unknown(self):
        key = await self._seed()
        with _patch_bss(get_order=AsyncMock(return_value={
                "ok": True, "status": "CANCELED", "raw": {}, "executed_qty": None})):
            await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        self.assertEqual(row["kind"], Kind.FINAL_FILL_QTY_UNKNOWN)  # kind muda
        self.assertEqual(row["state"], State.RETRY_PENDING)          # mantém pausa
        self.assertIsNone(row["resolved_at"])

    async def test_filled_but_position_unknown_keeps_pause(self):
        key = await self._seed()
        with _patch_bss(
            get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
            _fresh_position_size=AsyncMock(return_value=(None, "stale")),
        ):
            await ers._reconcile_one(key)
        self.assertEqual((await ers._get_repo().get(key))["state"], State.RETRY_PENDING)


# ════════════════════════════════════════════════════════════════════════════
#  4) MAKER UNKNOWN + condicionais + freshness
# ════════════════════════════════════════════════════════════════════════════
class CleanupTests(_AsyncBase):
    async def _seed(self, cond_ids):
        await ers.record_incident(kind=Kind.CONDITIONAL_SUBMISSION_UNKNOWN, symbol="ETH/USDT:USDT",
                                  conditional_ids=cond_ids)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    async def _elig(self, key):
        await ers._get_repo().update(key, next_retry_at=ers._now() - timedelta(seconds=1))

    async def test_first_empty_scan_does_not_resolve_grace(self):
        key = await self._seed({"sl": "SL1"})
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        cancel_algo_order=AsyncMock()):
            await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["clean_observations"], 1)

    async def test_grace_across_separate_cycles_resolves_flat(self):
        key = await self._seed({"sl": "SL1"})
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        cancel_algo_order=AsyncMock()):
            await ers._reconcile_one(key)
            await self._elig(key)
            await ers._reconcile_one(key)
        self.assertEqual((await ers._get_repo().get(key))["state"], State.FLAT)

    async def test_conditional_reappears_is_canceled_and_resets_clean(self):
        key = await self._seed({"sl": "SL1"})
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        cancel_algo_order=AsyncMock()):
            await ers._reconcile_one(key)
        await self._elig(key)
        cancel = AsyncMock()
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [{"algoId": "SL1"}]}),
                        cancel_algo_order=cancel):
            await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        cancel.assert_awaited_once_with("SL1")
        self.assertEqual(row["clean_observations"], 0)
        self.assertIsNone(row["resolved_at"])

    async def test_only_exact_ids_touched_not_other_trades(self):
        key = await self._seed({"sl": "SL1"})
        cancel = AsyncMock()
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [{"algoId": "OTHER-999"}]}),
                        cancel_algo_order=cancel):
            await ers._reconcile_one(key)
        cancel.assert_not_called()

    async def test_stale_listing_does_not_count_as_clean(self):
        key = await self._seed({"sl": "SL1"})
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": False, "error": "stale"}),
                        cancel_algo_order=AsyncMock()):
            await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        self.assertEqual(row["clean_observations"], 0)
        self.assertIsNone(row["resolved_at"])

    async def test_position_unknown_does_not_resolve_cleanup(self):
        key = await self._seed({"sl": "SL1"})
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(None, "rate_limited")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        cancel_algo_order=AsyncMock()):
            await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["clean_observations"], 0)


# ════════════════════════════════════════════════════════════════════════════
#  5) MANUAL_REQUIRED — untracked/persistence nunca fecham sozinhos
# ════════════════════════════════════════════════════════════════════════════
class ManualKindTests(_AsyncBase):
    async def test_untracked_position_goes_manual_required(self):
        await ers.record_incident(kind=Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT", side="buy",
                                  min_known_fill=0.7)
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        await ers._reconcile_one(key)
        row = await ers._get_repo().get(key)
        self.assertEqual(row["state"], State.MANUAL_REQUIRED)
        self.assertIsNone(row["resolved_at"])

    async def test_max_attempts_escalates_to_manual(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="Z/USDT:USDT", client_order_id="q")
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        await ers._get_repo().update(key, attempts=ers.RECONCILE_MAX_ATTEMPTS - 1)
        with _patch_bss(get_order=AsyncMock(return_value={"ok": False, "error": "down"})):
            await ers._reconcile_one(key)
        self.assertEqual((await ers._get_repo().get(key))["state"], State.MANUAL_REQUIRED)


# ════════════════════════════════════════════════════════════════════════════
#  6) Exchange mismatch — bloqueado, sem mutação
# ════════════════════════════════════════════════════════════════════════════
class ExchangeMismatchTests(_AsyncBase):
    async def test_non_binance_is_blocked_no_mutation(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  exchange="bybit", client_order_id="c")
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        with _patch_bss(get_order=AsyncMock()):
            await ers._reconcile_one(key)
            bss.get_order.assert_not_called()
        row = await ers._get_repo().get(key)
        self.assertIn("mismatch", (row["last_error"] or ""))


# ════════════════════════════════════════════════════════════════════════════
#  7) Boot reconcile + untracked + fail-closed
# ════════════════════════════════════════════════════════════════════════════
class BootTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        self.shadow, self.risk, self.st, self.rs = _install_fake_services()

    def tearDown(self):
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_boot_arms_quarantine_when_open_incident(self):
        with patch.object(ers, "_detect_untracked_positions", AsyncMock(return_value=0)), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                      client_order_id="c1")
            out = await ers.boot_reconcile()
        self.assertEqual(out["open_incidents"], 1)
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())

    async def test_boot_untracked_position_creates_incident_and_pause(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": [
                            {"symbol": "BTCUSDT", "size": 0.5}]})), \
                patch.object(ers, "_open_real_trade_symbols", AsyncMock(return_value=set())), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            out = await ers.boot_reconcile()
        self.assertEqual(out["untracked"], 1)
        rows = await ers._get_repo().list_open()
        self.assertEqual(rows[0]["kind"], Kind.UNTRACKED_POSITION)
        self.assertTrue((await self.risk.get_status())["trading_paused"])

    async def test_boot_exchange_unavailable_fails_closed_no_flat_assumption(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "stale": True, "positions": []})), \
                patch.object(ers, "_open_real_trade_symbols", AsyncMock(return_value=set())), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            out = await ers.boot_reconcile()
        self.assertEqual(out["untracked"], 0)
        self.assertEqual(len(await ers._get_repo().list_all()), 0)


# ════════════════════════════════════════════════════════════════════════════
#  8) Pausa/quarentena — libera só a própria, respeita pausa manual
# ════════════════════════════════════════════════════════════════════════════
class QuarantineReleaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        self.shadow, self.risk, self.st, self.rs = _install_fake_services()

    def tearDown(self):
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_release_own_quarantine_when_all_resolved(self):
        await ers._arm_quarantine("teste")
        self.assertTrue(self.rs["paused"])
        self.assertTrue(await ers._maybe_release_quarantine())
        self.assertFalse(self.rs["paused"])
        self.assertIsNone(self.shadow.execution_quarantine_reason())

    async def test_manual_operator_pause_is_preserved(self):
        await self.risk.set_manual_pause(True, "Pausa manual via kill switch")
        self.assertFalse(await ers._maybe_release_quarantine())
        self.assertTrue(self.rs["paused"])

    async def test_open_incident_blocks_release(self):
        await ers._arm_quarantine("x")
        await ers._get_repo().upsert("k", {"kind": Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B", "state": State.OPEN})
        self.assertFalse(await ers._maybe_release_quarantine())


# ════════════════════════════════════════════════════════════════════════════
#  9) API read-only + arquitetura
# ════════════════════════════════════════════════════════════════════════════
class ApiAndArchitectureTests(_AsyncBase):
    async def test_status_shape_readonly(self):
        await ers.record_incident(kind=Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT")
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        await ers._get_repo().update(key, state=State.MANUAL_REQUIRED, manual_reason="teste")
        st = await ers.get_status()
        for field in ("open_total", "retry_pending", "manual_required", "protected", "flat",
                      "quarantine_active", "items", "manual_items", "reconciler_running",
                      "last_reconciliation_at"):
            self.assertIn(field, st)
        self.assertEqual(st["manual_required"], 1)
        self.assertEqual(st["manual_items"][0]["manual_reason"], "teste")

    def test_no_mutation_endpoints_registered(self):
        main_src = (BACKEND / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/execution-incidents/status")', main_src)
        for forbidden in ("/api/execution-incidents/execute", "/api/execution-incidents/retry",
                          "/api/execution-incidents/resolve", "/api/execution-incidents/close",
                          "/api/execution-incidents/enable-live"):
            self.assertNotIn(forbidden, main_src)

    async def test_reconcile_never_sends_entry_or_market(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  client_order_id="c1", side="buy", planned_stop=100.0)
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        with _patch_bss(
            get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
            _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
            get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                {"type": "STOP_MARKET", "reduceOnly": True, "algoId": "SLX", "closePosition": True}]}),
            place_protection_orders=AsyncMock(),
            place_order=AsyncMock(), place_maker_entry_then_protect=AsyncMock(),
        ):
            await ers._reconcile_one(key)
            # P03 NUNCA coloca ordem: nem entry, nem maker, nem proteção nova.
            bss.place_order.assert_not_called()
            bss.place_maker_entry_then_protect.assert_not_called()
            bss.place_protection_orders.assert_not_called()
        # SL vivo válido foi ADOTADO (read-only) → PROTECTED.
        self.assertEqual((await ers._get_repo().get(key))["state"], State.PROTECTED)

    def test_feature_flags_default_off(self):
        import os
        self.assertNotEqual(os.getenv("MAKER_ENTRY_ENABLED", "false").lower(), "true")
        self.assertNotEqual(os.getenv("TF_UPGRADE_ENABLED", "false").lower(), "true")
        self.assertNotEqual(os.getenv("PYRAMIDING_ENABLED", "false").lower(), "true")


if __name__ == "__main__":
    unittest.main()
