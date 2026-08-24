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


# ── Hermeticidade: bloqueia rede/DNS durante TODA a suíte P03 e CONTABILIZA
# qualquer tentativa. Mesmo que o código capture a exceção, a tentativa fica
# registrada e a suíte FALHA no tearDownModule. ─────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS = []          # tentativas de rede do CÓDIGO sob teste (não das provas)


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P03 (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net       # DNS bloqueado (httpx/aiohttp resolvem por aqui)
    _socket.create_connection = _blocked_net  # conexão TCP externa bloqueada


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de "
                           "rede pelo código sob teste (mesmo se capturadas).")


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


def _bss_guard() -> ExitStack:
    """Guarda de HERMETICIDADE: mocka TODAS as funções de exchange do
    binance_signed_service com defaults benignos, para NENHUMA chamada real
    escapar (asyncio getaddrinfo em thread). Os testes sobrepõem via _patch_bss."""
    return _patch_bss(
        get_positions=AsyncMock(return_value={"ok": True, "positions": []}),
        _fresh_position_size=AsyncMock(return_value=(0.0, "ok")),
        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
        get_order=AsyncMock(return_value={"ok": False, "error": "guard"}),
        cancel_order=AsyncMock(return_value={"ok": True}),
        cancel_algo_order=AsyncMock(return_value={"ok": True}),
        place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "GUARD"}),
        place_order=AsyncMock(return_value={"ok": False}),
        place_maker_entry_then_protect=AsyncMock(return_value={"ok": False}),
        is_configured=lambda: False,
    )


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
    _MARK = "P03-QUARANTINE:"

    async def _open_p03():
        try:
            return len(await ers._get_repo().list_open())
        except Exception:
            return 0

    async def arm_p03_pause(reason):
        # atômico owner-aware: preserva pausa não-P03 do operador
        cur = rs["reason"] or ""
        if rs["paused"] and not cur.startswith(_MARK):
            return False
        rs["paused"] = True
        rs["reason"] = f"{_MARK} {reason}"
        return True

    async def set_manual_pause(paused, reason=None):
        if paused:
            rs["paused"] = True
            rs["reason"] = reason or "Pausa manual via kill switch"
            arm(rs["reason"], owner="manual")     # latch owner MANUAL (não genérico)
        else:
            if await _open_p03() > 0:              # incidente P03 aberto → mantém pausa
                rs["paused"] = True
                rs["reason"] = f"{_MARK} incidente P03 aberto"
            else:
                rs["paused"] = False
                rs["reason"] = None
            clear("manual")                        # limpa SÓ o owner manual (nunca genérico)
        return {"trading_paused": rs["paused"], "pause_reason": rs["reason"]}

    async def release_p03_pause(marker):
        # resultado ESTRUTURADO (P03.1E), na mesma "transação" lógica
        if await _open_p03() > 0:
            return "STILL_OPEN"
        if not rs["paused"]:
            return "RELEASED"
        if not (rs["reason"] or "").startswith(marker):
            return "SAFE_OTHER_OWNER"
        rs["paused"] = False
        rs["reason"] = None
        return "RELEASED"

    async def get_status():
        return {"enabled": True, "trading_paused": rs["paused"], "pause_reason": rs["reason"]}

    risk.RELEASE_RELEASED = "RELEASED"
    risk.RELEASE_SAFE_OTHER_OWNER = "SAFE_OTHER_OWNER"
    risk.RELEASE_STILL_OPEN = "STILL_OPEN"
    risk.RELEASE_ERROR = "ERROR"
    risk.arm_p03_pause = arm_p03_pause
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
        # fresh position mockada (lado "buy" = lado dos incidentes padrão) — evita
        # get_positions real (hermeticidade). Testes de mismatch usam classe própria.
        self._fp = patch.object(ers, "_fresh_position",
                                AsyncMock(return_value={"quality": "FRESH", "size": 1.0, "side": "buy"}))
        self._guard = _bss_guard()
        self._arm.start()
        self._rel.start()
        self._mrt.start()
        self._fp.start()

    def tearDown(self):
        self._arm.stop()
        self._rel.stop()
        self._mrt.stop()
        self._fp.stop()
        self._guard.close()
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
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=True)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(return_value=listing),   # adota A1 e revalida A1
                           place_protection_orders=AsyncMock(), place_order=AsyncMock(),
                           place_maker_entry_then_protect=AsyncMock()):
            await ers.reconcile_due()
            bss.place_order.assert_not_called()
            bss.place_maker_entry_then_protect.assert_not_called()
            bss.place_protection_orders.assert_not_called()     # adotou, não criou
        rows = await ers._get_repo().list_all()
        self.assertEqual(rows[0]["state"], State.PROTECTED)

    async def test_filled_no_sl_creates_sl_real_signature(self):
        await self._seed_entry(planned_qty=1.0)
        # adopção falha (listagem vazia) → cria S1; a RELISTAGEM em _resolve_protected
        # DEVE mostrar S1 vivo p/ revalidar (item: "SL criado é relistado e revalidado").
        listings = [{"ok": True, "orders": []},
                    {"ok": True, "orders": [_algo(algo_id="S1", side="SELL", close_position=True,
                                                  trigger_price=100.0)]}]
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=True)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(side_effect=listings),
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
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=True)), \
                _patch_bss(get_order=get_order,
                           cancel_order=AsyncMock(return_value={"ok": True}),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                               _algo(algo_id="A1", symbol="ETHUSDT", side="SELL",
                                     close_position=True, trigger_price=50.0)]}),
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
    def setUp(self):
        super().setUp()
        # cleanup opera sobre posição FLAT (limpeza de condicionais órfãs) — a 2ª
        # leitura fresh padrão é FLAT; testes de OPEN re-protegem via override próprio.
        self._fp.stop()
        self._fp = patch.object(ers, "_fresh_position",
                                AsyncMock(return_value={"quality": "FRESH", "size": 0.0, "side": None}))
        self._fp.start()

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
        self._guard = _bss_guard()

    def tearDown(self):
        self._guard.close()
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
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[])), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.boot_reconcile()
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())    # armou por stale
        self.assertTrue(self.rs["paused"])

    async def test_boot_untracked_uses_real_side(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": [
                            {"symbol": "BTCUSDT", "size": 0.5, "side": "Sell"}]})), \
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[])), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.boot_reconcile()
        rows = await ers._get_repo().list_open()
        self.assertEqual(rows[0]["kind"], Kind.UNTRACKED_POSITION)
        self.assertEqual(rows[0]["side"], "sell")             # side REAL, não abs→buy

    async def test_boot_partial_coverage_is_untracked(self):
        # invariante #5: posição fresh 2.0 coberta só por RealTrade 0.5 → NÃO é tracked.
        tracked = {"status": "open", "exchange": "binance", "source": "auto",
                   "symbol": "BTC/USDT:USDT", "side": "buy", "qty": 0.5, "id": 1}
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": [
                            {"symbol": "BTCUSDT", "size": 2.0, "side": "Buy"}]})), \
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[tracked])), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.boot_reconcile()
        rows = await ers._get_repo().list_open()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], Kind.UNTRACKED_POSITION)   # cobertura parcial ≠ tracked

    async def test_boot_absent_side_is_unknown_and_quarantines(self):
        # posição fresh com lado ausente/inválido → NÃO infiro BUY, NÃO FLAT:
        # boot inseguro + quarentena (item 11).
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": [
                            {"symbol": "BTCUSDT", "size": 1.0, "side": ""}]})), \
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[])), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            out = await ers.boot_reconcile()
        self.assertFalse(ers._boot_scan_safe)                       # boot inseguro
        self.assertIsNotNone(self.shadow.execution_quarantine_reason())  # quarentena armada
        self.assertEqual(await ers._get_repo().list_open(), [])     # não registrou UNTRACKED com lado inventado

    async def test_boot_full_aggregate_coverage_is_tracked(self):
        # cobertura AGREGADA integral (0.5+1.5=2.0) → tracked, sem incidente.
        t1 = {"status": "open", "exchange": "binance", "source": "auto",
              "symbol": "BTC/USDT:USDT", "side": "buy", "qty": 0.5, "id": 1}
        t2 = {"status": "open", "exchange": "binance", "source": "managed",
              "symbol": "BTC/USDT:USDT", "side": "buy", "qty": 1.5, "id": 2}
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "positions": [
                            {"symbol": "BTCUSDT", "size": 2.0, "side": "Buy"}]})), \
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[t1, t2])), \
                patch.object(ers, "reconcile_due", AsyncMock(return_value={})):
            await ers.boot_reconcile()
        self.assertEqual(await ers._get_repo().list_open(), [])       # totalmente coberta

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
        persist = AsyncMock(return_value=True)
        listings = [{"ok": True, "orders": []},                 # adopção falha → cria
                    {"ok": True, "orders": [_algo(algo_id="S1", side="SELL", close_position=True,
                                                  trigger_price=100.0)]}]   # relist revalida S1
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 7, "verdict": ers.Coverage.COVERED})), \
                patch.object(ers, "_persist_sl_order_id", persist), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(side_effect=listings),
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
        self._guard = _bss_guard()

    def tearDown(self):
        self._guard.close()
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_release_does_not_use_generic_clear_preserving_p02(self):
        self.shadow._arm_execution_quarantine("p02", owner="legacy")
        await ers._arm_quarantine("p03")
        ers._prev_open_count = 1
        await ers._maybe_release_quarantine()
        # LATCH owner-scoped: legacy (P02) preservado; só o owner p03 saiu (sem clear
        # genérico). O latch legacy segue bloqueando entradas independentemente.
        self.assertIn("legacy", self.owners)
        self.assertNotIn("p03", self.owners)

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
        self._guard = _bss_guard()

    def tearDown(self):
        self._guard.close()
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_stale_scan_keeps_boot_unsafe_and_blocks_release(self):
        with _patch_bss(is_configured=lambda: True,
                        get_positions=AsyncMock(return_value={"ok": True, "stale": True, "positions": []})), \
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[])):
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
                patch.object(ers, "_open_real_trades", AsyncMock(return_value=[])):
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
        self.assertTrue(ers._real_trade_match(self._rt(), "BTC/USDT:USDT", "buy"))
        self.assertTrue(ers._real_trade_match(self._rt(source="managed"), "BTC/USDT:USDT", "buy"))

    def test_bybit_and_shadow_and_manual_dont_match(self):
        self.assertFalse(ers._real_trade_match(self._rt(exchange="bybit"), "BTC/USDT:USDT", "buy"))
        self.assertFalse(ers._real_trade_match(self._rt(exchange=None), "BTC/USDT:USDT", "buy"))
        self.assertFalse(ers._real_trade_match(self._rt(source="manual"), "BTC/USDT:USDT", "buy"))
        self.assertFalse(ers._real_trade_match(self._rt(source="shadow"), "BTC/USDT:USDT", "buy"))

    def test_opposite_side_and_quote_and_missing_side(self):
        self.assertFalse(ers._real_trade_match(self._rt(side="short"), "BTC/USDT:USDT", "buy"))
        # BTCUSDC não mascara BTCUSDT
        self.assertFalse(ers._real_trade_match(self._rt(symbol="BTC/USDC:USDC"), "BTC/USDT:USDT", "buy"))
        # lado ausente no RealTrade não casa
        self.assertFalse(ers._real_trade_match(self._rt(side=None), "BTC/USDT:USDT", "buy"))
        self.assertFalse(ers._real_trade_match(self._rt(status="closed_tp2"), "BTC/USDT:USDT", "buy"))


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
            guard = kwargs.get("mutation_guard")         # invariante #6: guard de lease presente
            self.assertTrue(callable(guard), "place_protection_orders sem mutation_guard")
            self.assertIsInstance(await guard(), bool)   # revalida o lease (awaitable→bool, fail-closed)

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

    async def test_ambiguous_real_trade_id_none_never_protected(self):
        # invariante #4: RealTrade ambíguo (target_id=None) NUNCA vira PROTECTED.
        await self._seed(planned_qty=1.0)
        with patch.object(ers, "_match_real_trade", AsyncMock(return_value={"id": None})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=True)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                               _algo(algo_id="A1", side="SELL", close_position=True, trigger_price=100.0)]})):
            await ers.reconcile_due()
        st = (await ers._get_repo().list_all())[0]["state"]
        self.assertEqual(st, State.MANUAL_REQUIRED)          # nunca PROTECTED sem alvo determinístico
        self.assertNotEqual(st, State.PROTECTED)


class VerifiedClosureTests(_AsyncBase):
    """P03.1E-FIX: reproduções que FALHAVAM em 5904b963, agora fechadas."""
    async def _seed_entry(self, **kw):
        base = dict(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                    client_order_id="c1", side="buy", planned_stop=100.0, planned_qty=1.0)
        base.update(kw)
        await ers.record_incident(**base)
        return (await ers._get_repo().list_open())[0]["incident_key"]

    def _set_fresh(self, *reads):
        """Reprograma o _fresh_position da base (side_effect sequencial ou valor)."""
        self._fp.stop()
        val = list(reads)
        self._fp = patch.object(ers, "_fresh_position",
                                AsyncMock(side_effect=val) if len(val) > 1
                                else AsyncMock(return_value=val[0]))
        self._fp.start()

    async def test_second_read_unknown_never_protected(self):
        await self._seed_entry()
        # fp1 (portão) OPEN buy → adota SL; fp2 (_resolve_protected) UNKNOWN → RETRY.
        self._set_fresh({"quality": "FRESH", "size": 1.0, "side": "buy"},
                        {"quality": "UNKNOWN", "size": None, "side": None})
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                               _algo(algo_id="A1", side="SELL", close_position=True, trigger_price=100.0)]})):
            await ers.reconcile_due()
        st = (await ers._get_repo().list_all())[0]["state"]
        self.assertEqual(st, State.RETRY_PENDING)
        self.assertNotEqual(st, State.PROTECTED)

    async def test_long_incident_fresh_short_zero_mutations(self):
        await self._seed_entry(side="buy")                          # LONG
        self._set_fresh({"quality": "FRESH", "size": 1.0, "side": "sell"})  # fresh SHORT
        place = AsyncMock(return_value={"sl_ok": True, "sl_order_id": "X"})
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []}),
                        place_protection_orders=place):
            await ers.reconcile_due()
        place.assert_not_called()                                   # ZERO mutação de proteção
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.MANUAL_REQUIRED)

    async def test_maker_buy_fresh_short_zero_mutations(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="ETH/USDT:USDT",
                                  client_order_id="m1", side="buy", planned_stop=50.0,
                                  planned_qty=1.0, pending_maker=True)
        self._set_fresh({"quality": "FRESH", "size": 1.0, "side": "sell"})  # fresh SHORT vs maker BUY
        cancel = AsyncMock(return_value={"ok": True}); place = AsyncMock()
        with _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "NEW", "executed_qty": 0.0}),
                        cancel_order=cancel, place_protection_orders=place,
                        get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": []})):
            await ers.reconcile_due()
        cancel.assert_not_called()                                  # nem cancela a maker
        place.assert_not_called()                                   # nem cria proteção
        self.assertEqual((await ers._get_repo().list_all())[0]["state"], State.MANUAL_REQUIRED)

    async def test_db_off_skip_never_protected(self):
        await self._seed_entry()
        with patch.object(ers, "_match_real_trade", AsyncMock(return_value={"skip": True})), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(return_value={"ok": True, "orders": [
                               _algo(algo_id="A1", side="SELL", close_position=True, trigger_price=100.0)]})):
            await ers.reconcile_due()
        st = (await ers._get_repo().list_all())[0]["state"]
        self.assertEqual(st, State.RETRY_PENDING)                   # DB off → nunca PROTECTED
        self.assertNotEqual(st, State.PROTECTED)

    async def test_sl_absent_after_post_never_protected(self):
        await self._seed_entry()
        listings = [{"ok": True, "orders": []},                     # adopt falha → cria
                    {"ok": True, "orders": []}]                     # relist: SL sumiu
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(side_effect=listings),
                           place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "S1"})):
            await ers.reconcile_due()
        st = (await ers._get_repo().list_all())[0]["state"]
        self.assertEqual(st, State.RETRY_PENDING)                   # SL ausente pós-POST → nunca PROTECTED
        self.assertNotEqual(st, State.PROTECTED)

    async def test_created_sl_persisted_in_incident(self):
        key = await self._seed_entry()
        listings = [{"ok": True, "orders": []},
                    {"ok": True, "orders": [_algo(algo_id="S1", side="SELL", close_position=True,
                                                  trigger_price=100.0)]}]
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                patch.object(ers, "_persist_sl_order_id", AsyncMock(return_value=True)), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(side_effect=listings),
                           place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "S1"})):
            await ers.reconcile_due()
        cids = (await ers._get_repo().get(key)).get("conditional_ids") or {}
        self.assertEqual(cids.get("sl"), "S1")                      # sl_id gravado no incidente
        self.assertIn("S1", (cids.get("all") or []))               # união histórica

    async def test_mystery_sl_status_rejected(self):
        await self._seed_entry()
        myst = {"ok": True, "orders": [_algo(algo_id="A1", side="SELL", close_position=True,
                                             trigger_price=100.0, status="MYSTERY")]}
        with patch.object(ers, "_match_real_trade",
                          AsyncMock(return_value={"id": 1, "verdict": ers.Coverage.COVERED})), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           get_open_algo_orders=AsyncMock(return_value=myst),
                           place_protection_orders=AsyncMock(return_value={"sl_ok": True, "sl_order_id": "A1"})):
            await ers.reconcile_due()
        self.assertNotEqual((await ers._get_repo().list_all())[0]["state"], State.PROTECTED)

    async def test_fresh_position_absent_side_is_none(self):
        self._fp.stop()                                            # usa o _fresh_position REAL
        try:
            with _patch_bss(get_positions=AsyncMock(return_value={"ok": True, "positions": [
                    {"symbol": "BTCUSDT", "size": 1.0, "side": ""}]})):
                fp = await ers._fresh_position("BTC/USDT:USDT")
            self.assertEqual(fp["quality"], "FRESH")
            self.assertIsNone(fp["side"])                          # lado ausente → None (nunca "buy")
        finally:
            self._fp = patch.object(ers, "_fresh_position",
                                    AsyncMock(return_value={"quality": "FRESH", "size": 1.0, "side": "buy"}))
            self._fp.start()

    async def test_identity_a_only_trade_b_no_match(self):
        rt_b = {"status": "open", "exchange": "binance", "source": "auto", "symbol": "BTC/USDT:USDT",
                "side": "buy", "qty": 1.0, "qty_initial": 1.0, "client_order_id": "B",
                "exchange_order_id": None, "id": 99}

        class _Res:
            def scalars(self): return self
            def all(self): return [types.SimpleNamespace(**rt_b)]

        class _Sess:
            async def execute(self, *a, **k): return _Res()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        import db as _db
        self._mrt.stop()                                          # usa o _match_real_trade REAL
        try:
            with patch.object(_db, "DB_ENABLED", True), patch.object(_db, "get_session", lambda: _Sess()):
                rt = await ers._match_real_trade("BTC/USDT:USDT", "buy", 1.0, identity=["A"])
            self.assertIsNone(rt)                                 # identidade A + só trade B → NO_MATCH
        finally:
            self._mrt = patch.object(ers, "_match_real_trade", AsyncMock(return_value={"skip": True}))
            self._mrt.start()


class CoverageVerdictTests(unittest.TestCase):
    """invariante #4: cobertura RealTrade por AGREGAÇÃO Decimal + tolerância = stepSize
    (sem 0,1%/50% arbitrários). 5 estados: COVERED/INSUFFICIENT/NO_MATCH/AMBIGUOUS/UNKNOWN."""
    def _rt(self, qty, tid=1):
        return {"id": tid, "qty": qty}

    def test_no_match_when_pool_empty(self):
        self.assertEqual(ers._coverage_verdict([], [], 1.0, 0.001), (ers.Coverage.NO_MATCH, None))

    def test_ambiguous_when_multiple_without_single_identity(self):
        v, tid = ers._coverage_verdict([self._rt(0.6, 1), self._rt(0.6, 2)], [], 1.0, 0.001)
        self.assertEqual(v, ers.Coverage.AMBIGUOUS)
        self.assertIsNone(tid)                               # alvo indeterminado

    def test_covered_single_identity(self):
        v, tid = ers._coverage_verdict([self._rt(1.0, 9)], [self._rt(1.0, 9)], 1.0, 0.001)
        self.assertEqual(v, ers.Coverage.COVERED)
        self.assertEqual(tid, 9)

    def test_covered_single_pool_no_identity(self):
        v, tid = ers._coverage_verdict([self._rt(1.0, 4)], [], 1.0, 0.001)
        self.assertEqual((v, tid), (ers.Coverage.COVERED, 4))

    def test_insufficient_uses_stepsize_not_percentage(self):
        # falta 0.05 (>> step 0.001) → INSUFFICIENT (com 0,1% antigo 0.95 "passaria")
        v, tid = ers._coverage_verdict([self._rt(0.95, 3)], [], 1.0, 0.001)
        self.assertEqual(v, ers.Coverage.INSUFFICIENT)
        self.assertEqual(tid, 3)

    def test_covered_within_one_stepsize_tolerance(self):
        # falta só 0.0005 (< step 0.001) → dentro da precisão de lote → COVERED
        v, _ = ers._coverage_verdict([self._rt(0.9995, 3)], [], 1.0, 0.001)
        self.assertEqual(v, ers.Coverage.COVERED)

    def test_unknown_when_need_unknown(self):
        v, tid = ers._coverage_verdict([self._rt(1.0, 5)], [], None, 0.001)
        self.assertEqual(v, ers.Coverage.UNKNOWN)
        self.assertEqual(tid, 5)

    def test_unknown_when_a_qty_is_missing(self):
        v, _ = ers._coverage_verdict([self._rt(None, 5)], [], 1.0, 0.001)
        self.assertEqual(v, ers.Coverage.UNKNOWN)

    def test_decimal_aggregation_no_float_drift(self):
        # 0.1+0.2 em float = 0.30000000000000004; Decimal soma exata cobre 0.3
        v, _ = ers._coverage_verdict([self._rt(0.1, 1), self._rt(0.2, 1)], [self._rt(0.1, 1)], 0.3, 0.0)
        self.assertIn(v, (ers.Coverage.COVERED,))            # agregação exata, sem drift


class HermeticNetTests(unittest.TestCase):
    def test_network_is_blocked(self):
        # usa raiser LOCAL (não o _blocked_net contado) para não poluir _NET_ATTEMPTS
        def _raise(*a, **k):
            raise RuntimeError("blocked")
        with patch.object(_socket, "getaddrinfo", _raise), \
                patch.object(_socket, "create_connection", _raise):
            with self.assertRaises(RuntimeError):
                _socket.getaddrinfo("demo-fapi.binance.com", 443)
            with self.assertRaises(RuntimeError):
                _socket.create_connection(("demo-fapi.binance.com", 443))


# ════════════════════════════════════════════════════════════════════════════
#  P03.1D — transacional: arm-before-persist, resume manual, fresh side,
#  SL/planned_stop, maker states, lease por mutação
# ════════════════════════════════════════════════════════════════════════════
class ArmBeforePersistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._p03_latch_armed = False
        ers._boot_scan_safe = True
        self.shadow, self.risk, self.st, self.rs, self.owners = _install_fake_services()
        self._guard = _bss_guard()

    def tearDown(self):
        self._guard.close()
        ers.set_repo(None)
        _uninstall_fake_services()

    async def test_pause_persisted_before_incident_visible(self):
        # ordem de eventos: quando o upsert grava o incidente, a pausa P03 já está on
        seen = {}

        class _Repo(InMemoryIncidentRepo):
            async def upsert(self, key, defaults):
                seen["paused_at_upsert"] = self.rs_paused()
                return await super().upsert(key, defaults)

            def rs_paused(_self):
                return self.rs["paused"]
        ers.set_repo(_Repo())
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="c1")
        self.assertTrue(seen["paused_at_upsert"])   # pausa já persistida ANTES do incidente existir
        self.assertIn("p03", self.owners)

    async def test_manual_resume_with_open_incident_keeps_paused(self):
        await ers.record_incident(kind=Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT")
        # operador tenta resumir manualmente — como há incidente P03 aberto, NÃO libera
        res = await self.risk.set_manual_pause(False)
        self.assertTrue(res["trading_paused"])
        self.assertIn("p03", self.owners)           # latch P03 preservado
        self.assertNotIn("manual", self.owners)      # só o owner manual saiu


class FreshSideAndSlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._boot_scan_safe = True
        self._arm = patch.object(ers, "_arm_quarantine", AsyncMock()); self._arm.start()
        self._rel = patch.object(ers, "_maybe_release_quarantine", AsyncMock(return_value=False)); self._rel.start()
        self._guard = _bss_guard()

    def tearDown(self):
        self._arm.stop(); self._rel.stop(); self._guard.close(); ers.set_repo(None)

    async def test_fresh_position_preserves_side(self):
        with _patch_bss(get_positions=AsyncMock(return_value={"ok": True, "positions": [
                {"symbol": "BTCUSDT", "size": 0.5, "side": "Sell"}]})):
            fp = await ers._fresh_position("BTC/USDT:USDT")
        self.assertEqual(fp["quality"], "FRESH")
        self.assertEqual(fp["side"], "sell")
        self.assertEqual(fp["size"], 0.5)

    async def test_fresh_stale_is_unknown(self):
        with _patch_bss(get_positions=AsyncMock(return_value={"ok": True, "stale": True, "positions": []})):
            fp = await ers._fresh_position("BTC/USDT:USDT")
        self.assertEqual(fp["quality"], "UNKNOWN")

    async def test_protected_side_mismatch_goes_manual(self):
        await ers.record_incident(kind=Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT",
                                  client_order_id="c1", side="buy", planned_stop=100.0)
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        with patch.object(ers, "_ensure_stop", AsyncMock(return_value=(True, "SLX", "ok"))), \
                _patch_bss(get_order=AsyncMock(return_value={"ok": True, "status": "FILLED", "orig_qty": 1.0}),
                           _fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                           get_positions=AsyncMock(return_value={"ok": True, "positions": [
                               {"symbol": "BTCUSDT", "size": 1.0, "side": "Sell"}]})):  # short vs inc buy
            await ers.reconcile_due()
        self.assertEqual((await ers._get_repo().get(key))["state"], State.MANUAL_REQUIRED)

    async def test_adopt_requires_planned_stop(self):
        inc = {"side": "buy", "symbol": "BTC/USDT:USDT"}    # SEM planned_stop
        listing = {"orders": [_algo(algo_id="A1", side="SELL", close_position=True, trigger_price=100.0)]}
        self.assertFalse(_adopt_live_stop(inc, 1.0, listing)[0])


class LeasePerMutationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._boot_scan_safe = True
        self._arm = patch.object(ers, "_arm_quarantine", AsyncMock()); self._arm.start()
        self._rel = patch.object(ers, "_maybe_release_quarantine", AsyncMock(return_value=False)); self._rel.start()
        self._mrt = patch.object(ers, "_match_real_trade", AsyncMock(return_value={"skip": True})); self._mrt.start()
        self._guard = _bss_guard()

    def tearDown(self):
        self._arm.stop(); self._rel.stop(); self._mrt.stop(); self._guard.close(); ers.set_repo(None)

    async def test_lease_lost_between_cancels_stops_second(self):
        # cleanup OPEN com 2 extras; renew falha após A1 → A2 NÃO é cancelado
        await ers.record_incident(kind=Kind.CLEANUP_PENDING, symbol="ETH/USDT:USDT",
                                  side="buy", planned_stop=50.0,
                                  conditional_ids={"all": ["EX1", "EX2"]})
        key = (await ers._get_repo().list_open())[0]["incident_key"]
        # SL cardinal adotado (A0) + 2 extras EX1/EX2
        listing = {"ok": True, "orders": [
            _algo(algo_id="A0", side="SELL", reduce_only=True, quantity=1.0, trigger_price=50.0),
            _algo(algo_id="EX1", client_algo_id="EX1"), _algo(algo_id="EX2", client_algo_id="EX2")]}
        cancel = AsyncMock(return_value={"ok": True})
        renew_calls = {"n": 0}

        async def _renew(k, o):
            renew_calls["n"] += 1
            return renew_calls["n"] <= 1    # 1ª renovação ok; a partir da 2ª, lease perdido
        with patch.object(ers, "_renew_or_abort", _renew), \
                _patch_bss(_fresh_position_size=AsyncMock(return_value=(1.0, "ok")),
                           get_open_algo_orders=AsyncMock(return_value=listing),
                           cancel_algo_order=cancel):
            await ers.reconcile_due()
        self.assertLessEqual(cancel.await_count, 1)   # A2 não executou após perder o lease


if __name__ == "__main__":
    unittest.main()
