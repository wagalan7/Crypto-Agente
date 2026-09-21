"""P03 (lote final) — contrato puro da intenção e ligação com o executor.

Hermético: sem rede, banco ou exchange. A exclusividade transacional REAL é
provada em `tests/pg_integration_p03_intent.py` (PostgreSQL descartável).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import socket as _socket
import unittest
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste P03")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import entry_intent_service as intents   # noqa: E402

TRIGGER = 1_760_000_100_000


def identity(**over):
    values = dict(account_ref="binance:mainnet", exchange="binance", symbol="BTC-USDT-USDT",
                  quote="USDT", side="long", position_side="BOTH", timeframe="4h",
                  playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2", purpose="ENTRY",
                  trigger_candle_ms=TRIGGER)
    values.update(over)
    return intents.EntryIdentity(**values)


def payload(**over):
    values = {"entry": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0, "leverage": 3}
    values.update(over)
    return values


class IdentityContract(unittest.TestCase):
    def test_key_ignores_qty_score_price_and_clock(self):
        base = identity()
        self.assertEqual(base.intent_key, identity().intent_key)
        self.assertEqual(base.client_order_id, identity().client_order_id)
        # payload material muda o fingerprint, não a chave
        self.assertNotEqual(intents.payload_fingerprint(payload()),
                            intents.payload_fingerprint(payload(entry=101.0)))
        self.assertEqual(intents.payload_fingerprint(payload()),
                         intents.payload_fingerprint(payload()))

    def test_new_trigger_is_a_new_decision(self):
        self.assertNotEqual(identity().intent_key,
                            identity(trigger_candle_ms=TRIGGER + 300_000).intent_key)

    def test_purpose_side_and_account_are_part_of_the_identity(self):
        base = identity().intent_key
        for over in ({"purpose": "HEDGE"}, {"purpose": "FLIP"}, {"purpose": "UPGRADE"},
                     {"side": "short"}, {"account_ref": "binance:testnet"},
                     {"timeframe": "1h"}, {"playbook": "TREND_PULLBACK"},
                     {"playbook_version": "SCORE_V3"}, {"symbol": "ETH-USDT-USDT"},
                     {"position_side": "LONG"}):
            with self.subTest(over=over):
                self.assertNotEqual(base, identity(**over).intent_key)

    def test_client_order_id_respects_exchange_contract(self):
        coid = identity().client_order_id
        self.assertLessEqual(len(coid), intents.MAX_CLIENT_ORDER_ID)
        self.assertRegex(coid, r"^[A-Za-z0-9_-]+$")
        self.assertTrue(coid.startswith("cw-"))

    def test_invalid_identity_is_refused(self):
        for over in ({"side": "LONG"}, {"side": "neutral"}, {"trigger_candle_ms": 0},
                     {"trigger_candle_ms": True}, {"trigger_candle_ms": 1.5},
                     {"symbol": ""}, {"timeframe": "4 h"}, {"account_ref": "conta secreta"}):
            with self.subTest(over=over), self.assertRaises(ValueError):
                identity(**over)

    def test_payload_rejects_non_finite_and_keeps_absence(self):
        for bad in (float("nan"), float("inf"), "100", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                intents.payload_fingerprint(payload(entry=bad))
        absent = intents.payload_fingerprint(payload(tp1=None))
        self.assertNotEqual(absent, intents.payload_fingerprint(payload()))

    def test_capacity_rule_counts_pending_reservations(self):
        capacity = intents.Capacity(risk_usd=10.0, max_open_risk_usd=25.0, open_risk_usd=10.0)
        self.assertIsNone(intents._capacity_reason(capacity, 0, 0.0))
        self.assertEqual(intents._capacity_reason(capacity, 1, 10.0), "MAX_OPEN_RISK")
        slots = intents.Capacity(max_open_positions=2, open_positions=1)
        self.assertIsNone(intents._capacity_reason(slots, 0, 0.0))
        self.assertEqual(intents._capacity_reason(slots, 1, 0.0), "MAX_OPEN_POSITIONS")

    def test_reserve_without_database_never_authorises_a_post(self):
        async def broken():
            raise RuntimeError("db fora")
        import asyncio
        reservation = asyncio.run(intents.reserve(broken, identity(), payload(), owner="w1"))
        self.assertEqual(reservation.decision, intents.UNAVAILABLE)
        self.assertFalse(reservation.granted)


class ExecutorWiring(unittest.TestCase):
    SOURCE = (BACKEND / "services" / "shadow_trade_service.py").read_text()

    def test_reservation_precedes_every_order_mutation(self):
        tree = ast.parse(self.SOURCE)
        function = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "open_shadow_for_recs")
        body = ast.unparse(function)
        reserve_at = body.index("_reserve_entry_intent(")
        for dispatch in ("await _maker_fn(", "await exchange_service.place_order("):
            self.assertLess(reserve_at, body.index(dispatch), dispatch)
            guard_at = body.rindex("_intent_dispatch_guard(_intent)", 0, body.index(dispatch))
            self.assertLess(guard_at, body.index(dispatch))
        self.assertLess(body.index("_settle_entry_intent("), body.index("_close_entry_intent("))
        self.assertIn("client_order_id = _intent['client_order_id']", body)

    def test_no_toggle_disables_the_intent_and_no_delete(self):
        for forbidden in ("INTENT_ENABLED", "SKIP_INTENT", "ignore_intent", "delete(EntryIntent"):
            self.assertNotIn(forbidden, self.SOURCE)
        service = (BACKEND / "services" / "entry_intent_service.py").read_text()
        for forbidden in ("delete(", "DROP", "TRUNCATE", "getenv"):
            self.assertNotIn(forbidden, service)

    def test_state_machine_has_no_automatic_resend(self):
        service = (BACKEND / "services" / "entry_intent_service.py").read_text()
        self.assertIn("LEASE_EXPIRED_AFTER_DISPATCH", service)
        tree = ast.parse(service)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for forbidden in ("place_order", "cancel_order", "post", "request"):
            self.assertNotIn(forbidden, names)


class SettlementContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import shadow_trade_service as sts
        self.sts = sts
        self.calls = []

        async def record(kind):
            self.calls.append(kind)
            return True
        self.record = record

    async def test_ack_alone_does_not_confirm_the_intent(self):
        sts = self.sts
        intent = {"granted": True, "dispatched": True, "intent_key": "k", "state": "SENDING"}
        with patch.object(sts, "_close_entry_intent", AsyncMock()) as close:
            with patch("services.entry_intent_service.mark_unknown", AsyncMock(return_value=True)) as unknown, \
                    patch("services.entry_intent_service.mark_terminal", AsyncMock(return_value=True)) as terminal:
                await sts._settle_entry_intent(intent, {"ok": True})
                unknown.assert_not_awaited()
                terminal.assert_not_awaited()
            close.assert_not_awaited()
        self.assertEqual(intent["state"], "SENDING")

    async def test_uncertain_outcomes_become_unknown(self):
        sts = self.sts
        for result in ({"manual_intervention_required": True}, {"quarantine_required": True},
                       {"safety_state": "ENTRY_SUBMISSION_UNKNOWN"}, {"ok": False},
                       {"emergency_close_attempted": True}):
            intent = {"granted": True, "dispatched": True, "intent_key": "k", "state": "SENDING"}
            with patch("services.entry_intent_service.mark_unknown", AsyncMock(return_value=True)) as unknown:
                await sts._settle_entry_intent(intent, result)
            with self.subTest(result=result):
                unknown.assert_awaited_once()
                self.assertEqual(intent["state"], "UNKNOWN")

    async def test_explicit_no_post_and_no_fill_are_terminal(self):
        sts = self.sts
        for result, reason in (({"entry_not_submitted": True}, "ENTRY_NOT_SUBMITTED"),
                               ({"no_fill": True}, "NO_FILL")):
            intent = {"granted": True, "dispatched": True, "intent_key": "k", "state": "SENDING"}
            with patch("services.entry_intent_service.mark_terminal", AsyncMock(return_value=True)) as terminal:
                await sts._settle_entry_intent(intent, result)
            with self.subTest(reason=reason):
                self.assertEqual(terminal.await_args.kwargs["reason"], reason)
                self.assertEqual(intent["state"], "TERMINAL")

    async def test_pending_or_failed_persistence_never_confirms(self):
        sts = self.sts
        intent = {"granted": True, "dispatched": True, "intent_key": "k", "state": "SENDING"}
        with patch("services.entry_intent_service.mark_unknown", AsyncMock(return_value=True)) as unknown, \
                patch("services.entry_intent_service.mark_confirmed", AsyncMock(return_value=True)) as confirmed:
            await sts._close_entry_intent(intent, {"id": 7}, pending_entry=True)
            self.assertEqual(unknown.await_args.kwargs["reason"], "PENDING_ENTRY_ORDER")
            confirmed.assert_not_awaited()
            intent["state"] = "SENDING"
            await sts._close_entry_intent(intent, None)
            self.assertEqual(unknown.await_count, 2)
            confirmed.assert_not_awaited()

    async def test_confirmation_links_the_real_trade(self):
        sts = self.sts
        intent = {"granted": True, "dispatched": True, "intent_key": "k", "state": "SENDING"}
        with patch("services.entry_intent_service.mark_confirmed", AsyncMock(return_value=True)) as confirmed:
            await sts._close_entry_intent(intent, {"id": 42})
        self.assertEqual(confirmed.await_args.kwargs["real_trade_id"], 42)
        self.assertEqual(intent["state"], "CONFIRMED")

    async def test_reservation_without_dispatch_is_released(self):
        sts = self.sts
        intent = {"granted": True, "dispatched": False, "intent_key": "k", "state": "RESERVED"}
        with patch("services.entry_intent_service.release_reserved", AsyncMock(return_value=True)) as release:
            await sts._close_entry_intent(intent, None)
        self.assertEqual(release.await_args.kwargs["reason"], "NOT_DISPATCHED")

    async def test_guard_requires_lease_before_dispatch(self):
        sts = self.sts
        intent = {"granted": True, "dispatched": False, "intent_key": "k"}
        with patch("services.entry_intent_service.mark_sending", AsyncMock(return_value=False)):
            self.assertFalse(await sts._intent_dispatch_guard(intent))
        with patch("services.entry_intent_service.mark_sending", AsyncMock(return_value=True)), \
                patch("services.entry_intent_service.may_dispatch", AsyncMock(return_value=True)):
            self.assertTrue(await sts._intent_dispatch_guard(intent))
        self.assertTrue(intent["dispatched"])
        with patch("services.entry_intent_service.may_dispatch", AsyncMock(return_value=False)):
            self.assertFalse(await sts._intent_dispatch_guard(intent))
        self.assertFalse(await sts._intent_dispatch_guard({"granted": False}))

    def test_identity_from_recommendation_uses_decision_candle(self):
        sts = self.sts
        rec = {"symbol": "BTC/USDT:USDT", "timeframe": "4h", "leverage": 3,
               "score_provenance": {"formula_effective": "SCORE_V2"},
               "signal": {"data_freshness": {"candle": {"close_time_ms": TRIGGER}}}}
        with patch.object(sts, "_active_exchange_name", lambda: "binance"), \
                patch.object(sts, "_exchange_is_production", lambda: True):
            first = sts._entry_intent_identity(rec, "long")
            same_decision_other_snapshot = sts._entry_intent_identity(dict(rec, _snapshot_id=999), "long")
        self.assertIsNotNone(first)
        self.assertEqual(first.intent_key, same_decision_other_snapshot.intent_key)
        self.assertEqual(first.quote, "USDT")
        with patch.object(sts, "_active_exchange_name", lambda: "binance"), \
                patch.object(sts, "_exchange_is_production", lambda: True):
            self.assertIsNone(sts._entry_intent_identity({"symbol": "BTC/USDT:USDT"}, "long"))

    def test_no_network(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
