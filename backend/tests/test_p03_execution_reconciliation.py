"""
P03 / P03.1 — Reconciliação persistente de execução (testes herméticos).

Nenhuma rede/Binance/DB real; nenhuma entry criada. Repo em memória (mesma
semântica de claim/lease/upsert do SQL) + exchange mockada via patch.object no
módulo real binance_signed_service, usando o FORMATO NORMALIZADO snake_case real
de get_open_algo_orders (algo_id/client_algo_id/reduce_only/close_position/
quantity/side/trigger_price/type).

Limitação declarada: sem Postgres descartável, a atomicidade do upsert SQL
(ON CONFLICT) e o fencing por UPDATE…WHERE são exercitados na implementação em
memória equivalente — NÃO é um teste Postgres real.
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
    _adopt_live_stop, _backoff_seconds, kind_from_safety_state, _exact_conditional_ids,
    _matching_orders,
)


# ── Hermeticidade: bloqueia rede/DNS durante TODA a suíte P03 ────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection


def _blocked_net(*a, **k):
    raise RuntimeError("REDE BLOQUEADA no teste P03 (hermético): tentativa de "
                       f"resolver/conectar {a[:1]} — mocke a exchange.")


def setUpModule():
    _socket.getaddrinfo = _blocked_net       # DNS bloqueado (httpx/aiohttp resolvem por aqui)
    _socket.create_connection = _blocked_net  # conexão TCP externa bloqueada


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION


def _algo(**kw):
    """Ordem no FORMATO REAL de get_open_algo_orders (snake_case)."""
    o = {"algo_id": "", "client_algo_id": None, "symbol": "BTCUSDT", "side": "SELL",
         "type": "STOP_MARKET", "trigger_price": 0.0, "quantity": 0.0,
         "close_position": False, "reduce_only": False, "status": "NEW", "working_type": "MARK_PRICE"}
    o.update(kw)
    return o


def _patch_bss(**fns) -> ExitStack:
    stack = ExitStack()
    for name, val in fns.items():
        stack.enter_context(patch.object(bss, name, val))
    return stack


def _install_fake_services():
    """Fakes leves owner-aware de shadow_trade_service + risk_service."""
    shadow = types.ModuleType("services.shadow_trade_service")
    owners = set()
    st = {"reason": None}

    def arm(reason, owner="legacy"):
        owners.add(owner)
        if not st["reason"]:
            st["reason"] = reason

    def clear(owner=None):
        if owner is None:
            owners.clear()
        else:
            owners.discard(owner)
        if not owners:
            st["reason"] = None

    shadow._arm_execution_quarantine = arm
    shadow.clear_execution_quarantine = clear
    shadow.execution_quarantine_reason = lambda: st["reason"]
    shadow.execution_quarantine_owners = lambda: set(owners)

    risk = types.ModuleType("services.risk_service")
    rs = {"paused": False, "reason": None}

    async def set_manual_pause(paused, reason=None):
        rs["paused"] = paused
        rs["reason"] = reason if paused else None
        if not paused:
            clear(None)            # side effect REAL: resume manual limpa TODOS os owners
        return {"trading_paused": paused, "pause_reason": rs["reason"]}

    async def release_p03_pause(marker):
        # CAS owner-aware: só remove se a pausa atual ainda é P03. NÃO chama o
        # clear genérico (não apaga owners P02/manual).
        if not rs["paused"] or not (rs["reason"] or "").startswith(marker):
            return False
        rs["paused"] = False
        rs["reason"] = None
        return True

    async def get_status():
        return {"enabled": True, "trading_paused": rs["paused"], "pause_reason": rs["reason"]}

    risk.set_manual_pause = set_manual_pause
    risk.release_p03_pause = release_p03_pause
    risk.get_status = get_status
    risk.is_paused = lambda: rs["paused"]

    import services as pkg
    sys.modules["services.shadow_trade_service"] = shadow
    sys.modules["services.risk_service"] = risk
    pkg.shadow_trade_service = shadow
    pkg.risk_service = risk
    return shadow, risk, st, rs, owners


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
#  1) Núcleo puro
# ════════════════════════════════════════════════════════════════════════════
class DecisionCoreTests(unittest.TestCase):
    def test_terminal_qty_rules(self):
        self.assertEqual(terminal_qty({"ok": True, "status": "FILLED", "orig_qty": 2.0})[0], 2.0)
        self.assertEqual(terminal_qty({"ok": True, "status": "REJECTED", "raw": {}})[0], 0.0)
        self.assertIsNone(terminal_qty({"ok": True, "status": "CANCELED", "raw": {}, "executed_qty": None})[0])
        self.assertEqual(terminal_qty({"ok": True, "status": "CANCELED", "raw": {"executedQty": "0.5"},
                                       "executed_qty": 0.5})[0], 0.5)
        self.assertIsNone(terminal_qty({"ok": True, "status": "PARTIALLY_FILLED", "executed_qty": 0.3})[0])

    def test_nan_inf_qty_invalid(self):
        self.assertIsNone(terminal_qty({"ok": True, "status": "FILLED", "orig_qty": float("nan")})[0])
        self.assertIsNone(terminal_qty({"ok": True, "status": "FILLED", "orig_qty": float("inf")})[0])

    def test_classify_and_position_verdict(self):
        self.assertEqual(classify_entry({"ok": True, "status": "FILLED", "orig_qty": 1.0})[:2], ("PROTECTED", 1.0))
        self.assertEqual(classify_entry({"ok": True, "status": "REJECTED", "raw": {}})[:2], ("FLAT", 0.0))
        self.assertEqual(classify_entry({"ok": False})[0], "RETRY")
        self.assertEqual(classify_entry({"ok": True, "status": "EXPIRED", "raw": {}, "executed_qty": None})[0], "FILL_UNKNOWN")
        self.assertEqual(position_verdict(None, "stale"), "UNKNOWN")
        self.assertEqual(position_verdict(0.0, "ok"), "FLAT")
        self.assertEqual(position_verdict(1.5, "ok"), "OPEN")

    def test_backoff_initial_matches_docs(self):
        self.assertEqual(_backoff_seconds(1), ers.RECONCILE_BACKOFF_BASE_S)   # 15s no 1º retry
        self.assertEqual(_backoff_seconds(2), ers.RECONCILE_BACKOFF_BASE_S * 2)

    def test_kind_from_safety_state(self):
        self.assertEqual(kind_from_safety_state("ENTRY_SUBMISSION_UNKNOWN"), Kind.ENTRY_SUBMISSION_UNKNOWN)
        self.assertEqual(kind_from_safety_state("LEGACY_STOP_ROLLBACK_UNKNOWN"), Kind.CLEANUP_PENDING)
        self.assertEqual(kind_from_safety_state("x", closed=True), Kind.CLEANUP_PENDING)
        self.assertEqual(kind_from_safety_state("ENTRY_ORDER_UNKNOWN"), Kind.ENTRY_ORDER_UNKNOWN)

    # ── Adoção de SL: consome snake_case real ──
    def test_adopt_valid_snake_case_stop(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}   # long → SL SELL
        listing = {"orders": [_algo(algo_id="A1", side="SELL", reduce_only=True,
                                    quantity=1.0, trigger_price=100.15, type="STOP_MARKET")]}
        ok, aid, _ = _adopt_live_stop(inc, 1.0, listing)
        self.assertTrue(ok)
        self.assertEqual(aid, "A1")
        # trailing rejeitado mesmo com tudo certo
        trail = {"orders": [_algo(algo_id="A1", side="SELL", reduce_only=True, quantity=1.0,
                                  trigger_price=100.1, type="TRAILING_STOP_MARKET")]}
        self.assertFalse(_adopt_live_stop(inc, 1.0, trail)[0])

    def test_adopt_rejects_wrong_side(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        listing = {"orders": [_algo(algo_id="A1", side="BUY", reduce_only=True, quantity=1.0, trigger_price=100.0)]}
        self.assertFalse(_adopt_live_stop(inc, 1.0, listing)[0])

    def test_adopt_rejects_no_reduce_no_close(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        listing = {"orders": [_algo(algo_id="A1", side="SELL", reduce_only=False,
                                    close_position=False, quantity=1.0, trigger_price=100.0)]}
        self.assertFalse(_adopt_live_stop(inc, 1.0, listing)[0])

    def test_adopt_rejects_insufficient_qty(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        listing = {"orders": [_algo(algo_id="A1", side="SELL", reduce_only=True, quantity=0.4, trigger_price=100.0)]}
        self.assertFalse(_adopt_live_stop(inc, 1.0, listing)[0])

    def test_adopt_rejects_empty_algo_id_and_trigger_out_of_tol(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        self.assertFalse(_adopt_live_stop(inc, 1.0, {"orders": [_algo(algo_id="", side="SELL",
                          close_position=True, trigger_price=100.0)]})[0])
        # trigger fora da tolerância (0,5%)
        self.assertFalse(_adopt_live_stop(inc, 1.0, {"orders": [_algo(algo_id="A1", side="SELL",
                          close_position=True, trigger_price=140.0)]})[0])

    def test_adopt_close_position_covers(self):
        inc = {"side": "sell", "planned_stop": 200.0, "symbol": "BTC/USDT:USDT"}  # short → SL BUY
        listing = {"orders": [_algo(algo_id="A2", side="BUY", close_position=True, trigger_price=200.3)]}
        self.assertTrue(_adopt_live_stop(inc, None, listing)[0])   # 0,15% dentro de 0,2%
        # trigger fora de 0,2% (0,25%) → rejeitado
        listing2 = {"orders": [_algo(algo_id="A2", side="BUY", close_position=True, trigger_price=200.5)]}
        self.assertFalse(_adopt_live_stop(inc, None, listing2)[0])

    def test_exact_conditional_ids_from_prefix(self):
        ids = _exact_conditional_ids({"conditional_prefix": "cw-x"})
        self.assertIn("cw-x-sl", ids)
        self.assertIn("cw-x-tp1", ids)

    def test_matching_orders_snake_client_algo_id(self):
        listing = {"orders": [_algo(algo_id="A9", client_algo_id="cw-x-sl")]}
        m = _matching_orders(listing, ["cw-x-sl"])
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["algo_id"], "A9")


# ════════════════════════════════════════════════════════════════════════════
#  Base assíncrona
# ════════════════════════════════════════════════════════════════════════════
class _AsyncBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._p03_latch_armed = False
        ers._boot_scan_safe = True   # testes de reconcile não exercitam o re-scan de boot
        self._arm = patch.object(ers, "_arm_quarantine", AsyncMock())
        self._rel = patch.object(ers, "_maybe_release_quarantine", AsyncMock(return_value=False))
        self._mrt = patch.object(ers, "_match_real_trade", AsyncMock(return_value={"skip": True}))
        self._arm.start()
        self._rel.start()
        self._mrt.start()

    def tearDown(self):
        self._arm.stop()
        self._rel.stop()
        self._mrt.stop()
        ers.set_repo(None)

    async def _seed(self, **kw):
        await ers.record_incident(**kw)
        return (await ers._get_repo().list_open())[0]["incident_key"]


# ════════════════════════════════════════════════════════════════════════════
#  2) Persistência / reabertura
# ════════════════════════════════════════════════════════════════════════════
class PersistenceTests(_AsyncBase):
    async def test_created_once_and_two_upserts_one_row(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="c1")
        r2 = await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                       client_order_id="c1", min_known_fill=0.5)
        self.assertFalse(r2["created"])
        rows = await ers._get_repo().list_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["min_known_fill"], 0.5)

    async def test_resolved_incident_reopens_on_recurrence(self):
        key = await self._seed(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="ETH/USDT:USDT", client_order_id="c9")
        await ers._get_repo().update(key, state=State.FLAT, resolved_at=ers._now())
        self.assertEqual(len(await ers._get_repo().list_open()), 0)
        # mesma chave reincide → REABRE (não fica invisível pela unique key)
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="ETH/USDT:USDT", client_order_id="c9")
        opened = await ers._get_repo().list_open()
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["state"], State.OPEN)

    async def test_key_has_no_generic_dash(self):
        await ers.record_incident(kind=Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT")
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        self.assertFalse(key.endswith(":-"))


# ════════════════════════════════════════════════════════════════════════════
#  3) Claim / lease / fencing (semântica SQL exercitada em memória)
# ════════════════════════════════════════════════════════════════════════════
class ClaimFencingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = InMemoryIncidentRepo()

    async def _mk(self, key="k"):
        await self.repo.upsert(key, {"kind": Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B"})
        return key

    async def test_two_claims_one_owner(self):
        k = await self._mk()
        lease = ers._now() + timedelta(seconds=60)
        self.assertTrue(await self.repo.claim(k, "A", lease))
        self.assertFalse(await self.repo.claim(k, "B", lease))

    async def test_expired_lease_recovered_and_reclaimable(self):
        k = await self._mk()
        await self.repo.claim(k, "A", ers._now() - timedelta(seconds=1))
        self.assertEqual(await self.repo.recover_expired_claims(), 1)
        self.assertTrue(await self.repo.claim(k, "B", ers._now() + timedelta(seconds=60)))

    async def test_update_claimed_requires_owner_and_lease(self):
        k = await self._mk()
        await self.repo.claim(k, "A", ers._now() + timedelta(seconds=60))
        self.assertIsNone(await self.repo.update_claimed(k, "B", state=State.FLAT))  # dono errado
        self.assertIsNotNone(await self.repo.update_claimed(k, "A", state=State.RECONCILING))

    async def test_stale_owner_cannot_update_after_lease_expiry(self):
        k = await self._mk()
        await self.repo.claim(k, "A", ers._now() - timedelta(seconds=1))  # lease vencido
        self.assertIsNone(await self.repo.update_claimed(k, "A", state=State.FLAT))

    async def test_release_claim_owner_checked(self):
        k = await self._mk()
        await self.repo.claim(k, "A", ers._now() + timedelta(seconds=60))
        await self.repo.release_claim(k, owner="B")             # não libera claim alheio
        self.assertEqual((await self.repo.get(k))["claimed_by"], "A")
        await self.repo.release_claim(k, owner="A")
        self.assertIsNone((await self.repo.get(k))["claimed_by"])


# ════════════════════════════════════════════════════════════════════════════
#  4) Entry reconcile (adota/cria SL; nunca entry/MARKET)
# ════════════════════════════════════════════════════════════════════════════
class EntryReconcileTests(_AsyncBase):
    async def _seed_entry(self, **kw):
        base = dict(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                    client_order_id="c1", side="buy", planned_stop=100.0)
        base.update(kw)
        await ers.record_incident(**base)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    async def test_filled_adopts_snake_case_sl_protected(self):
        await self._seed_entry()
        listing = {"ok": True, "orders": [_algo(algo_id="A1", side="SELL", reduce_only=True,
                                                quantity=1.0, trigger_price=100.1)]}
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                        _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value=listing),
                        place_protection_orders=AsyncMock(), place_order=AsyncMock(),
                        place_maker_entry_then_protect=AsyncMock()):
            await ers.reconcile_due()
            bss.place_order.assert_not_called()
            bss.place_maker_entry_then_protect.assert_not_called()
            bss.place_protection_orders.assert_not_called()     # adotou, não criou
        self.assertEqual((await ers._get_repo().list_open() or [{}])[0].get("state", State.FLAT), State.FLAT)
        rows = await ers._get_repo().list_all()
        self.assertEqual(rows[0]["state"], State.PROTECTED)

    async def test_filled_no_sl_creates_sl_real_signature(self):
        await self._seed_entry(planned_qty=1.0)
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                        _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "S1"}),
                        place_order=AsyncMock()):
            await ers.reconcile_due()
            self.assertTrue(bss.place_protection_orders.await_count == 1)
            args, kwargs = bss.place_protection_orders.call_args
            self.assertEqual(args[0], "BTC/USDT:USDT")
            self.assertEqual(args[1], "BUY")            # entry_side mapeado
            self.assertEqual(kwargs.get("stop_loss"), 100.0)
            self.assertIsNone(kwargs.get("tp1"))
            bss.place_order.assert_not_called()
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.PROTECTED)

    async def test_sl_creation_unconfirmed_keeps_retry(self):
        await self._seed_entry(planned_qty=1.0)
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                        _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        place_protection_orders=AsyncMock(return_value={"sl_ok": False, "sl_order_id": None})):
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.RETRY_PENDING)

    async def test_rejected_flat_and_terminal_without_qty_unknown(self):
        k1 = await self._seed_entry()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "REJECTED", "raw": {}})):
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().get(k1))["state"], State.FLAT)
        # terminal sem qty
        ers.set_repo(InMemoryIncidentRepo())
        k2 = await self._seed_entry()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "CANCELED", "raw": {}, "executed_qty": None})):
            await ers.reconcile_due()
        row = await ers._get_repo().get(k2)
        self.assertEqual(row["kind"], Kind.FINAL_FILL_QTY_UNKNOWN)
        self.assertIsNone(row["resolved_at"])

    async def test_position_unknown_after_fill_keeps(self):
        await self._seed_entry()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                        _fresh_position_size=AsyncMock(return_value=(None, "stale"))):
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.RETRY_PENDING)


# ════════════════════════════════════════════════════════════════════════════
#  5) Maker viva
# ════════════════════════════════════════════════════════════════════════════
class MakerTests(_AsyncBase):
    async def _seed_maker(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="ETH/USDT:USDT",
                                  client_order_id="m1", side="buy", planned_stop=50.0,
                                  planned_qty=1.0, pending_maker=True)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    async def test_new_maker_cancelled_then_terminal_protected(self):
        await self._seed_maker()
        get_order = AsyncMock(side_effect=[
            {"ok": True, "status": "NEW"},                       # 1ª consulta: viva
            {"ok": True, "status": "FILLED", "orig_qty": 1.0},   # pós-cancel: terminal
        ])
        with _patch_bss(get_order=get_order,
                        cancel_order=AsyncMock(return_value={"ok": True}),
                        _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                            _algo(algo_id="A1", side="SELL", close_position=True, trigger_price=50.0)]}),
                        place_order=AsyncMock()):
            await ers.reconcile_due()
            bss.cancel_order.assert_awaited_once()               # cancelou a maker pelo ID
            bss.place_order.assert_not_called()                  # nunca MARKET
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.PROTECTED)

    async def test_cancel_uncertain_keeps_retry(self):
        await self._seed_maker()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "NEW"}),
                        cancel_order=AsyncMock(return_value={"ok": False, "error": "timeout"}),
                        place_order=AsyncMock()):
            await ers.reconcile_due()
            bss.place_order.assert_not_called()
        row = await ers._get_repo().list_all()[0] if False else (await ers._get_repo().list_all())[0]
        self.assertEqual(row["state"], State.RETRY_PENDING)
        self.assertIsNone(row["resolved_at"])

    async def test_terminal_without_qty_after_cancel_unknown(self):
        await self._seed_maker()
        get_order = AsyncMock(side_effect=[
            {"ok": True, "status": "NEW"},
            {"ok": True, "status": "CANCELED", "raw": {}, "executed_qty": None},
        ])
        with _patch_bss(get_order=get_order, cancel_order=AsyncMock(return_value={"ok": True})):
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().list_all())[0]["kind"], Kind.FINAL_FILL_QTY_UNKNOWN)


# ════════════════════════════════════════════════════════════════════════════
#  6) Cleanup por identidade exata
# ════════════════════════════════════════════════════════════════════════════
class CleanupTests(_AsyncBase):
    async def _seed_cleanup(self, **kw):
        base = dict(kind=Kind.CONDITIONAL_SUBMISSION_UNKNOWN, symbol="ETH/USDT:USDT",
                    conditional_ids={"sl": "SL1"})
        base.update(kw)
        await ers.record_incident(**base)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    async def _elig(self, key):
        await ers._get_repo().update(key, next_retry_at=ers._now() - timedelta(seconds=1))

    async def test_no_identity_does_not_resolve(self):
        # incidente de cleanup SEM ids/prefixo → não pode resolver por "nenhum match"
        key = await self._seed_cleanup(conditional_ids=None, conditional_prefix=None,
                                       kind=Kind.CLEANUP_PENDING)
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []})):
            await ers.reconcile_due()
        row = await ers._get_repo().get(key)
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["state"], State.RETRY_PENDING)

    async def test_grace_across_cycles_resolves_flat(self):
        key = await self._seed_cleanup()
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []})):
            await ers.reconcile_due()
            await self._elig(key)
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().get(key))["state"], State.FLAT)

    async def test_reappearance_cancels_snake_case_and_resets_grace(self):
        key = await self._seed_cleanup()
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []})):
            await ers.reconcile_due()      # clean=1
        await self._elig(key)
        cancel = AsyncMock(return_value={"ok": True})
        listings = [{"ok": True, "orders": [_algo(algo_id="A1", client_algo_id="SL1")]},  # reapareceu
                    {"ok": True, "orders": []}]                                            # após cancel
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(side_effect=listings),
                        cancel_algo_order=cancel):
            await ers.reconcile_due()
        cancel.assert_awaited_once_with("A1")           # cancela pelo algo_id real
        row = await ers._get_repo().get(key)
        self.assertEqual(row["clean_observations"], 0)  # grace reiniciado
        self.assertIsNone(row["resolved_at"])

    async def test_other_trades_order_untouched(self):
        key = await self._seed_cleanup()
        cancel = AsyncMock(return_value={"ok": True})
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                            _algo(algo_id="OTHER", client_algo_id="other-sl")]}),
                        cancel_algo_order=cancel):
            await ers.reconcile_due()
        cancel.assert_not_called()

    async def test_cancel_ok_false_keeps_incident(self):
        key = await self._seed_cleanup()
        await self._elig(key)
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                            _algo(algo_id="A1", client_algo_id="SL1")]}),
                        cancel_algo_order=AsyncMock(return_value={"ok": False, "error": "x"})):
            await ers.reconcile_due()
        row = await ers._get_repo().get(key)
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["state"], State.RETRY_PENDING)

    async def test_stale_listing_not_clean(self):
        key = await self._seed_cleanup()
        with _patch_bss(_fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": False, "error": "stale"})):
            await ers.reconcile_due()
        row = await ers._get_repo().get(key)
        self.assertEqual(row["clean_observations"], 0)
        self.assertIsNone(row["resolved_at"])


# ════════════════════════════════════════════════════════════════════════════
#  7) Quarentena com ownership + boot fail-closed (fakes reais)
# ════════════════════════════════════════════════════════════════════════════
class QuarantineBootTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._p03_latch_armed = False
        ers._boot_scan_safe = False
        self.shadow, self.risk, self.st, self.rs, self.owners = _install_fake_services()

    def tearDown(self):
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_p03_arms_own_owner_and_does_not_clear_p02(self):
        # P02/legacy arma o latch primeiro
        self.shadow._arm_execution_quarantine("p02 stop failure", owner="legacy")
        await ers._arm_quarantine("p03 incidente")
        self.assertIn("p03", self.owners)
        self.assertIn("legacy", self.owners)
        # P03 tenta liberar (sem incidentes) — NÃO pode apagar o latch legacy
        ers._prev_open_count = 1  # simula transição
        ers._boot_scan_safe = True
        await ers._maybe_release_quarantine()
        self.assertIn("legacy", self.owners)                 # latch P02 preservado
        self.assertNotIn("p03", self.owners)                 # só o próprio owner saiu
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())

    async def test_arm_does_not_overwrite_manual_pause(self):
        await self.risk.set_manual_pause(True, "Pausa manual via kill switch")
        await ers._arm_quarantine("p03 incidente")
        self.assertEqual(self.rs["reason"], "Pausa manual via kill switch")  # preservada

    async def test_release_only_on_transition_and_own(self):
        await ers._arm_quarantine("p03 incidente")           # pausa P03-owned
        self.assertTrue(self.rs["paused"])
        ers._prev_open_count = 1
        # sem boot_scan_safe → NÃO libera (mantém quarentena)
        self.assertFalse(await ers._maybe_release_quarantine())
        self.assertTrue(self.rs["paused"])
        # com boot_scan_safe → libera SÓ o owner p03, via release_p03_pause (sem clear genérico)
        ers._boot_scan_safe = True
        self.assertTrue(await ers._maybe_release_quarantine())
        self.assertFalse(self.rs["paused"])
        self.assertNotIn("p03", self.owners)

    async def test_zero_incidents_no_transition_no_clear(self):
        # sem P03 ter armado e sem transição, reconcile_due não deve limpar/logar
        out = await ers.reconcile_due()
        self.assertFalse(out["quarantine_released"])
        self.assertIsNone(self.shadow.execution_quarantine_reason())

    async def test_boot_arms_before_loops_when_open_incident(self):
        with patch.object(ers, "_detect_untracked_positions",
                          AsyncMock(return_value={"status": "FLAT", "count": 0})), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="c1")
            out = await ers.boot_reconcile()
        self.assertEqual(out["open_incidents"], 1)
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())

    async def test_boot_stale_positions_arms_latch_failclosed(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "stale": True, "positions": []})), \
                patch.object(ers, "_open_real_trade_symbols", AsyncMock(return_value=set())), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.boot_reconcile()
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())    # armou por stale
        self.assertTrue(self.rs["paused"])

    async def test_boot_untracked_uses_real_side(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": [
                            {"symbol": "BTCUSDT", "size": 0.5, "side": "Sell"}]})), \
                patch.object(ers, "_open_real_trade_symbols", AsyncMock(return_value=set())), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.boot_reconcile()
        rows = await ers._get_repo().list_open()
        self.assertEqual(rows[0]["kind"], Kind.UNTRACKED_POSITION)
        self.assertEqual(rows[0]["side"], "sell")             # side REAL, não abs→buy

    async def test_boot_list_open_failure_arms_latch(self):
        class _BadRepo(InMemoryIncidentRepo):
            async def recover_expired_claims(self):
                raise RuntimeError("db down")
        ers.set_repo(_BadRepo())
        out = await ers.boot_reconcile()
        self.assertIn("boot_error", out)
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())    # fail-closed


# ════════════════════════════════════════════════════════════════════════════
#  8) API + arquitetura
# ════════════════════════════════════════════════════════════════════════════
class ApiArchTests(_AsyncBase):
    async def test_status_shape(self):
        await ers.record_incident(kind=Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT")
        st = await ers.get_status()
        for f in ("open_total", "retry_pending", "manual_required", "protected", "flat",
                  "quarantine_active", "items", "manual_items", "reconciler_running"):
            self.assertIn(f, st)

    def test_no_mutation_endpoints(self):
        src = (BACKEND / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/execution-incidents/status")', src)
        for bad in ("/api/execution-incidents/execute", "/api/execution-incidents/retry",
                    "/api/execution-incidents/resolve", "/api/execution-incidents/close"):
            self.assertNotIn(bad, src)

    def test_flags_off(self):
        import os
        self.assertNotEqual(os.getenv("MAKER_ENTRY_ENABLED", "false").lower(), "true")
        self.assertNotEqual(os.getenv("TF_UPGRADE_ENABLED", "false").lower(), "true")
        self.assertNotEqual(os.getenv("PYRAMIDING_ENABLED", "false").lower(), "true")


# ════════════════════════════════════════════════════════════════════════════
#  P03.1B — QTY/SL/tracking/lease/integração/ownership/boot
# ════════════════════════════════════════════════════════════════════════════
class QtyRawTests(unittest.TestCase):
    def test_terminal_status_without_raw_executedqty_stays_unknown(self):
        # executed_qty normalizado = 0.0 NÃO pode virar FLAT por ausência.
        q, _ = terminal_qty({"ok": True, "status": "CANCELED", "raw": {}, "executed_qty": 0.0})
        self.assertIsNone(q)
        v, _, _ = classify_entry({"ok": True, "status": "EXPIRED_IN_MATCH", "raw": {}, "executed_qty": 0.0})
        self.assertEqual(v, "FILL_UNKNOWN")

    def test_terminal_with_raw_executedqty_used(self):
        q, _ = terminal_qty({"ok": True, "status": "CANCELED", "raw": {"executedQty": "0.7"}, "executed_qty": 0.0})
        self.assertEqual(q, 0.7)

    def test_string_false_not_true(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        listing = {"orders": [_algo(algo_id="A1", side="SELL", reduce_only="false",
                                    close_position="false", quantity=1.0, trigger_price=100.0)]}
        self.assertFalse(_adopt_live_stop(inc, 1.0, listing)[0])   # "false" não cobre

    def test_fresh_larger_needs_bigger_coverage(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        listing = {"orders": [_algo(algo_id="A1", side="SELL", reduce_only=True, quantity=1.0, trigger_price=100.0)]}
        self.assertTrue(_adopt_live_stop(inc, 1.0, listing)[0])    # cobre 1.0
        self.assertFalse(_adopt_live_stop(inc, 2.0, listing)[0])   # posição fresh 2.0 > 1.0

    def test_reject_missing_side_and_dead_status(self):
        inc = {"side": "buy", "planned_stop": 100.0, "symbol": "BTC/USDT:USDT"}
        self.assertFalse(_adopt_live_stop(inc, 1.0, {"orders": [_algo(algo_id="A1", side="",
                          close_position=True, trigger_price=100.0)]})[0])          # sem side
        self.assertFalse(_adopt_live_stop(inc, 1.0, {"orders": [_algo(algo_id="A1", side="SELL",
                          close_position=True, trigger_price=100.0, status="CANCELED")]})[0])  # morta

    def test_safe_prefix_no_unicode(self):
        p = ers._safe_prefix("binance:ENTRY:BTC/USDT:USDT:cw-abc")
        self.assertNotIn("…", p)
        self.assertTrue(all(c.isalnum() or c == "-" for c in p))
        self.assertLessEqual(len(p), 20)


class IntegrationAssemblyTests(unittest.TestCase):
    def test_assemble_preserves_caller_client_id_and_maker(self):
        rec = {"symbol": "BTC/USDT:USDT", "direction": "long", "qty": 1.0, "stop_loss": 100.0}
        order_res = {"ok": True, "status": "PARTIALLY_FILLED", "was_maker": True,
                     "executed_qty": 0.3, "safety_state": "ENTRY_SUBMISSION_UNKNOWN",
                     "sl_order_id": "SL9", "tp1_order_id": "TP1", "result": {"orderId": "E1"}}
        kw = ers.assemble_entry_incident(order_res, rec, snapshot_id=42,
                                         local_client_order_id="cw-local")
        self.assertEqual(kw["client_order_id"], "cw-local")   # fallback do caller
        self.assertEqual(kw["conditional_prefix"], "cw-local")
        self.assertTrue(kw["pending_maker"])                  # was_maker + pending
        self.assertEqual(kw["conditional_ids"], {"sl": "SL9", "tp1": "TP1"})
        self.assertEqual(kw["entry_order_id"], "E1")
        self.assertEqual(kw["min_known_fill"], 0.3)
        self.assertEqual(kw["snapshot_id"], "42")

    def test_assemble_final_fill_qty_unknown_kind(self):
        kw = ers.assemble_entry_incident({"ok": True, "final_fill_qty_unknown": True},
                                         {"symbol": "X/USDT:USDT"}, local_client_order_id="c")
        self.assertEqual(kw["kind"], Kind.FINAL_FILL_QTY_UNKNOWN)


class LeaseUpsertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = InMemoryIncidentRepo()

    async def test_expired_lease_does_not_renew(self):
        await self.repo.upsert("k", {"kind": Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B"})
        await self.repo.claim("k", "A", ers._now() - timedelta(seconds=1))   # já vencido
        self.assertFalse(await self.repo.renew_claim("k", "A", ers._now() + timedelta(seconds=60)))

    async def test_old_owner_cannot_mutate_after_expiry(self):
        await self.repo.upsert("k", {"kind": Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B"})
        await self.repo.claim("k", "A", ers._now() - timedelta(seconds=1))
        self.assertIsNone(await self.repo.update_claimed("k", "A", state=State.FLAT))

    async def test_upsert_merge_keeps_greatest_lb_and_all_ids(self):
        await self.repo.upsert("k", {"kind": Kind.CLEANUP_PENDING, "symbol": "B",
                                     "min_known_fill": 0.5, "conditional_ids": {"sl": "S1"}})
        row, created = await self.repo.upsert("k", {"min_known_fill": 0.2,
                                                    "conditional_ids": {"tp1": "T1"}, "planned_stop": 9.0})
        self.assertFalse(created)
        self.assertEqual(row["min_known_fill"], 0.5)               # GREATEST
        self.assertEqual(row["conditional_ids"]["sl"], "S1")
        self.assertEqual(row["conditional_ids"]["tp1"], "T1")
        self.assertIn("S1", row["conditional_ids"]["all"])         # união histórica
        self.assertIn("T1", row["conditional_ids"]["all"])
        self.assertEqual(row["planned_stop"], 9.0)                 # preencheu ausente

    async def test_upsert_reopens_resolved(self):
        await self.repo.upsert("k", {"kind": Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B"})
        await self.repo.update("k", state=State.FLAT, resolved_at=ers._now())
        row, _ = await self.repo.upsert("k", {"kind": Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B"})
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["state"], State.OPEN)


class TrackingTests(_AsyncBase):
    async def test_protected_without_real_trade_goes_manual_untracked(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  client_order_id="c1", side="buy", planned_stop=100.0)
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        listing = {"ok": True, "orders": [_algo(algo_id="A1", side="SELL", close_position=True, trigger_price=100.0)]}
        with patch.object(ers, "_match_real_trade", AsyncMock(return_value=None)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                           get_open_algo_orders=AsyncMock(return_value=listing)):
            await ers.reconcile_due()
        row = await ers._get_repo().get(key)
        self.assertEqual(row["state"], State.MANUAL_REQUIRED)     # protegido sem RealTrade
        # criou também um incidente UNTRACKED_POSITION
        kinds = {r["kind"] for r in await ers._get_repo().list_all()}
        self.assertIn(Kind.UNTRACKED_POSITION, kinds)

    async def test_created_sl_persisted_to_real_trade(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  client_order_id="c1", side="buy", planned_stop=100.0, planned_qty=1.0)
        persist = AsyncMock()
        with patch.object(ers, "_match_real_trade", AsyncMock(return_value={"id": 7})), \
                patch.object(ers, "_persist_sl_order_id", persist), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                           place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "S1"})):
            await ers.reconcile_due()
        persist.assert_awaited_once_with(7, "S1")               # idempotente no RealTrade
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.PROTECTED)

    async def test_entry_flat_with_conditional_identity_routes_cleanup(self):
        # fresh-flat mas com prefixo de condicional → NÃO vai direto a FLAT
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  client_order_id="cw-x", conditional_prefix="cw-x", side="buy")
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                        _fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                            _algo(algo_id="A1", client_algo_id="cw-x-sl")]}),
                        cancel_algo_order=AsyncMock(return_value={"ok": True})):
            await ers.reconcile_due()
        row = await ers._get_repo().get(key)
        self.assertIsNone(row["resolved_at"])                   # não resolveu FLAT — cleanup em curso


class OwnershipRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._p03_latch_armed = False
        ers._boot_scan_safe = True
        self.shadow, self.risk, self.st, self.rs, self.owners = _install_fake_services()

    def tearDown(self):
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_release_does_not_use_generic_clear_preserving_p02(self):
        self.shadow._arm_execution_quarantine("p02", owner="legacy")
        await ers._arm_quarantine("p03")
        ers._prev_open_count = 1
        await ers._maybe_release_quarantine()
        # legacy preservado E pausa não foi removida (ainda há owner P02)
        self.assertIn("legacy", self.owners)
        self.assertTrue(self.rs["paused"])          # não resumiu (outros owners restam)

    async def test_manual_non_p03_pause_not_removed(self):
        await self.risk.set_manual_pause(True, "Pausa manual via kill switch")
        await ers._arm_quarantine("p03")            # não sobrescreve reason manual
        ers._prev_open_count = 1
        await ers._maybe_release_quarantine()
        self.assertTrue(self.rs["paused"])          # pausa manual permanece

    async def test_open_incident_rearms_p03_after_improper_resume(self):
        await ers.record_incident(kind=Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT")
        # resume indevido limpou o latch
        self.shadow.clear_execution_quarantine(None)
        self.assertIsNone(self.shadow.execution_quarantine_reason())
        with patch.object(ers, "_detect_untracked_positions", AsyncMock(return_value={"status": "FLAT", "count": 0})):
            await ers.reconcile_due()               # ciclo re-arma o owner P03
        self.assertIn("p03", self.owners)


class BootSafeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._p03_latch_armed = False
        ers._boot_scan_safe = False
        self.shadow, self.risk, self.st, self.rs, self.owners = _install_fake_services()

    def tearDown(self):
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_stale_scan_keeps_boot_unsafe_and_blocks_release(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "stale": True, "positions": []})), \
                patch.object(ers, "_open_real_trade_symbols", AsyncMock(return_value=set())):
            await ers._detect_untracked_positions()
        self.assertFalse(ers._boot_scan_safe)
        # com pausa P03 e zero incidentes, ainda NÃO libera (boot inseguro)
        await ers._arm_quarantine("p03")
        ers._prev_open_count = 1
        self.assertFalse(await ers._maybe_release_quarantine())

    async def test_later_fresh_scan_allows_release(self):
        # 1º scan stale → inseguro; 2º scan fresh-flat → seguro
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": []})), \
                patch.object(ers, "_open_real_trade_symbols", AsyncMock(return_value=set())):
            out = await ers._detect_untracked_positions()
        self.assertEqual(out["status"], "FLAT")
        self.assertTrue(ers._boot_scan_safe)

    async def test_fresh_flat_distinct_from_unknown(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": False, "error": "down"})):
            out = await ers._detect_untracked_positions()
        self.assertEqual(out["status"], "UNKNOWN")   # erro ≠ flat
        self.assertFalse(ers._boot_scan_safe)


# ════════════════════════════════════════════════════════════════════════════
#  P03.1C — cross-exchange / maker GTX / SL / persist / union / hermeticidade
# ════════════════════════════════════════════════════════════════════════════
class CrossExchangeMatchTests(unittest.TestCase):
    def _rt(self, **kw):
        base = {"status": "open", "exchange": "binance", "source": "auto",
                "symbol": "BTC/USDT:USDT", "side": "long", "qty": 1.0, "id": 1}
        base.update(kw)
        return base

    def test_binance_auto_correct_matches(self):
        self.assertTrue(ers._real_trade_match(self._rt(), "BTC/USDT:USDT", "buy", 1.0))

    def test_bybit_and_shadow_and_manual_dont_match(self):
        self.assertFalse(ers._real_trade_match(self._rt(exchange="bybit"), "BTC/USDT:USDT", "buy", 1.0))
        self.assertFalse(ers._real_trade_match(self._rt(exchange=None), "BTC/USDT:USDT", "buy", 1.0))
        self.assertFalse(ers._real_trade_match(self._rt(source="manual"), "BTC/USDT:USDT", "buy", 1.0))
        self.assertFalse(ers._real_trade_match(self._rt(source="shadow"), "BTC/USDT:USDT", "buy", 1.0))

    def test_opposite_side_and_quote_and_qty(self):
        self.assertFalse(ers._real_trade_match(self._rt(side="short"), "BTC/USDT:USDT", "buy", 1.0))
        # BTCUSDC não mascara BTCUSDT
        self.assertFalse(ers._real_trade_match(self._rt(symbol="BTC/USDC:USDC"), "BTC/USDT:USDT", "buy", 1.0))
        # qty DB muito menor que a posição fresh
        self.assertFalse(ers._real_trade_match(self._rt(qty=0.1), "BTC/USDT:USDT", "buy", 1.0))
        self.assertFalse(ers._real_trade_match(self._rt(status="closed_tp2"), "BTC/USDT:USDT", "buy", 1.0))


class MakerGtxTests(unittest.TestCase):
    def test_gtx_ambiguous_without_status_is_pending(self):
        kw = ers.assemble_entry_incident({"ok": True, "was_maker": True}, {"symbol": "X/USDT:USDT"},
                                         local_client_order_id="c")
        self.assertTrue(kw["pending_maker"])

    def test_entry_order_terminal_prevents_false_pending(self):
        kw = ers.assemble_entry_incident({"ok": True, "was_maker": True, "entry_order_terminal": True,
                                          "status": "NEW"}, {"symbol": "X/USDT:USDT"}, local_client_order_id="c")
        self.assertFalse(kw["pending_maker"])

    def test_still_active_or_unknown_state_is_pending(self):
        kw = ers.assemble_entry_incident({"ok": True, "was_maker": True,
                                          "safety_state": "ENTRY_ORDER_STILL_ACTIVE_OR_UNKNOWN"},
                                         {"symbol": "X/USDT:USDT"}, local_client_order_id="c")
        self.assertTrue(kw["pending_maker"])

    def test_local_qty_used(self):
        kw = ers.assemble_entry_incident({"ok": True}, {"symbol": "X/USDT:USDT", "qty": 9},
                                         local_client_order_id="c", local_planned_qty=3.0)
        self.assertEqual(kw["planned_qty"], 3.0)         # local vence o rec


class ConditionalUnionTests(unittest.TestCase):
    def test_union_preserves_history(self):
        merged = ers._merge_conditional_ids({"sl": "S1"}, {"sl": "S2"})
        self.assertEqual(merged["sl"], "S2")
        self.assertIn("S1", merged["all"])
        self.assertIn("S2", merged["all"])

    def test_exact_ids_include_all_and_prefix(self):
        ids = ers._exact_conditional_ids({"conditional_ids": {"sl": "S2", "all": ["S1", "S2"]},
                                          "conditional_prefix": "cw-x"})
        for want in ("S1", "S2", "cw-x-sl", "cw-x-tp1"):
            self.assertIn(want, ids)


class PersistedFlagTests(_AsyncBase):
    async def test_record_incident_returns_persisted(self):
        r = await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="c")
        self.assertTrue(r["persisted"])


class Sl1cReconcileTests(_AsyncBase):
    async def _seed(self, **kw):
        base = dict(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="c1",
                    side="buy", planned_stop=100.0)
        base.update(kw)
        await ers.record_incident(**base)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    async def test_rejected_with_conditional_identity_not_flat(self):
        key = await self._seed(conditional_prefix="cw-x")
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "REJECTED", "raw": {}}),
                        _fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                            _algo(algo_id="A1", client_algo_id="cw-x-sl")]}),
                        cancel_algo_order=AsyncMock(return_value={"ok": True})):
            await ers.reconcile_due()
        self.assertIsNone((await ers._get_repo().get(key))["resolved_at"])   # cleanup, não FLAT

    async def test_terminal_cancel_without_raw_qty_stays_unknown(self):
        key = await self._seed()
        with _patch_bss(get_order=AsyncMock(return_value={
                "ok": True, "status": "CANCELED", "raw": {}, "executed_qty": 0.0})):
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().get(key))["kind"], Kind.FINAL_FILL_QTY_UNKNOWN)

    async def test_protected_needs_bigger_coverage_when_fresh_larger(self):
        # posição fresh 2.0 mas SL cobre só 1.0 → não adota → cria (place_protection)
        await self._seed(planned_qty=2.0)
        with patch.object(ers, "_match_real_trade", AsyncMock(return_value={"id": 5})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=True)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 2.0}),
                           _fresh_position_size=AsyncMock(return_value=(2.0, "ok")),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                               _algo(algo_id="A1", side="SELL", reduce_only=True, quantity=1.0, trigger_price=100.0)]}),
                           place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "S9"})):
            await ers.reconcile_due()
            args, kwargs = bss.place_protection_orders.call_args
            self.assertEqual(args[2], 2.0)               # cobre a posição fresh total

    async def test_sl_persist_conflict_blocks_protected(self):
        await self._seed(planned_qty=1.0)
        with patch.object(ers, "_match_real_trade", AsyncMock(return_value={"id": 5})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=False)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                               _algo(algo_id="A1", side="SELL", close_position=True, trigger_price=100.0)]})):
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.RETRY_PENDING)


class HermeticNetTests(unittest.TestCase):
    def test_network_is_blocked(self):
        with self.assertRaises(RuntimeError):
            _socket.getaddrinfo("demo-fapi.binance.com", 443)
        with self.assertRaises(RuntimeError):
            _socket.create_connection(("demo-fapi.binance.com", 443))


if __name__ == "__main__":
    unittest.main()
