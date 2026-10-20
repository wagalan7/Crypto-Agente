"""Réplica de gestão LIVE: caller real, trace sintético, zero conta/DB/rede.

As respostas das interfaces são explícitas. Nenhum preço, fill ou confirmação
é inventado para completar um trace; economia e autoridade LIVE ficam ausentes.
"""
from __future__ import annotations

import copy
import math
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import live_management_replica_service as replica

T0 = 1_800_000_000_000
SYMBOL = "SYNTHETICUSDT"


def config(**changes):
    values = {
        "TIME_STOP_ENABLED": False,
        "TIME_STOP_SCALP_MIN": 240,
        "TIME_STOP_DAY_MIN": 1440,
        "TIME_STOP_SWING_MIN": 10080,
        "PROTECTION_AUTOHEAL_ENABLED": False,
        "PROTECTION_HEAL_GRACE_SECONDS": 60.0,
        "PROTECTION_VERIFY_LIVE": True,
        "PROTECTION_TP1_QTY_PCT": 0.45,
        "TRADE_MANAGER_DUST_USD": 1.0,
        "BE_STRUCTURAL_ENABLED": True,
        "BE_STRUCT_ATR_BUFFER": 0.25,
        "BE_MAX_GIVEBACK_R": 0.5,
        "PRE_TP1_PROTECT_ENABLED": True,
        "PRE_TP1_PROTECT_TRIGGER_FRAC": 0.6,
        "PRE_TP1_PROTECT_LOCK_R": 0.5,
        "RUNNER_ENABLED": False,
        "RUNNER_QTY_PCT": 0.2,
        "RUNNER_DETECT_FRAC": 0.35,
        "RUNNER_ATR_MULT": 3.0,
        "RUNNER_LOOKBACK": 22,
        "RUNNER_ATR_TF_FALLBACK": "15m",
        "RUNNER_MIN_STEP_ATR": 0.25,
    }
    values.update(changes)
    return values


def state(**changes):
    values = {
        "id": 17, "symbol": SYMBOL, "side": "long", "source": "auto",
        "exchange": "binance", "status": "open", "phase": "pre_tp1",
        "qty": 10.0, "qty_initial": 10.0, "entry_price": 100.0,
        "opened_at_ms": T0 - 60_000, "planned_stop": 98.0,
        "created_at_ms": None, "client_order_id": None,
        "planned_tp1": 105.0, "planned_tp2": 110.0,
        "sl_order_id": "SL-OLD", "tp1_order_id": "TP1",
        "tp2_order_id": "TP2", "sl_current_price": 98.0,
        "adaptive_tp1_qty_pct": None, "adaptive_runner_atr_mult": None,
        "adaptive_runner_qty_pct": None, "notes": None, "timeframe": "15m",
    }
    values.update(changes)
    return values


def positions(qty=10.0, *, side="long", entry=100.0):
    return {"ok": True, "positions": [] if qty == 0 else [
        {"symbol": SYMBOL, "side": "Buy" if side == "long" else "Sell",
         "size": qty, "entry_price": entry, "position_side": "BOTH"}
    ]}


def tick(event_id="event-1", at_ms=T0, **ports):
    return {"event_id": event_id, "at_ms": at_ms, "ports": {
        port: [{"observed_at_ms": at_ms, "available_at_ms": at_ms,
                "value": copy.deepcopy(value)} for value in values]
        for port, values in ports.items()
    }}


def owned_tick(*, qty=10.0, side="long", event_id="event-1", at_ms=T0,
               **ports):
    return tick(event_id, at_ms, ownership_guard=[{"allowed": True}],
                get_positions=[positions(qty, side=side)], **ports)


def pre_tick(mark=103.0, *, qty=10.0, side="long", succeeds=True,
             event_id="event-1", at_ms=T0):
    ports = {"fetch_ticker": [{"last": mark}],
             "place_protection_orders": [
                 {"sl_ok": succeeds, "sl_order_id": "SL-NEW" if succeeds else None,
                  "sl_msg": None if succeeds else "SYNTHETIC_REJECTION"}]}
    if succeeds:
        ports["cancel_algo_order"] = [{"ok": True}]
    return owned_tick(qty=qty, side=side, event_id=event_id, at_ms=at_ms,
                      **ports)


def be_tick(qty=5.5, *, side="long", candles=None, event_id="event-1", at_ms=T0):
    return tick(event_id, at_ms, ownership_guard=[{"allowed": True}],
                get_positions=[positions(qty, side=side), positions(qty, side=side)],
                fetch_ohlcv=[candles],
                place_protection_orders=[{"sl_ok": True, "sl_order_id": "SL-BE"}],
                cancel_algo_order=[{"ok": True}],
                get_executions=[{"ok": True, "fills": []}])


def candles(*, count=60, high=102.0, low=100.0, close=101.0, at_ms=T0):
    return {"columns": {
        "timestamp": [at_ms - (count - index) * 60_000 for index in range(count)],
        "open": [close] * count, "high": [high] * count,
        "low": [low] * count, "close": [close] * count, "volume": [1.0] * count,
    }}


def request(*, cfg=None, initial=None, ticks=None):
    # Fixture construction only: explicit synthetic receipts are bound to the
    # real caller's observed requests in an INCOMPLETE, non-evidence preview.
    # The second execution validates all hashes/causal indices. No operator
    # trace can enter this preview mode or inherit these generated bindings.
    events = copy.deepcopy([pre_tick()] if ticks is None else ticks)
    for event in events:
        for responses in event["ports"].values():
            for response in responses:
                response.update(call_index=None, request_hash=None, after_action_index=None)
    preview = replica.make_request(config=config() if cfg is None else cfg,
        initial_state=state() if initial is None else initial, ticks=events,
        source="SYNTHETIC_TEST_ONLY", binding_mode="SYNTHETIC_PREVIEW")
    observed = replica.run_trace(preview)
    if observed["status"] != "INCOMPLETE_TRACE":
        raise AssertionError("preview must never be complete evidence")
    calls = [item for item in observed["reads"] + observed["actions"]
             if "call_index" in item]
    used = set()
    for item in calls:
        if "response_index" not in item:
            continue
        event = next(e for e in events if e["event_id"] == item["event_id"])
        response = event["ports"][item["port"]][item["response_index"]]
        response.update(call_index=item["call_index"],
                        request_hash=item["expected_request_hash"],
                        after_action_index=item["expected_after_action_index"])
        used.add(item["call_index"])
    unused_index = max(used, default=0) + 1
    for event in events:
        for port, responses in event["ports"].items():
            for response in responses:
                if response["call_index"] is None:
                    response.update(call_index=unused_index,
                        request_hash=replica.call_hash(port, ["UNUSED_FIXTURE"], {}),
                        after_action_index=0)
                    unused_index += 1
    return replica.make_request(config=preview["config"],
        initial_state=preview["initial_state"], ticks=events,
        source="SYNTHETIC_TEST_ONLY", binding_mode="BOUND")


class HermeticCase(unittest.TestCase):
    def setUp(self):
        self.network_attempts = []
        def forbidden(*args, **kwargs):
            self.network_attempts.append((args, kwargs))
            raise AssertionError("REDE REAL PROIBIDA")
        for target in ("socket.getaddrinfo", "socket.create_connection",
                       "socket.socket.connect", "socket.socket.connect_ex"):
            guard = patch(target, side_effect=forbidden)
            guard.start()
            self.addCleanup(guard.stop)

    def tearDown(self):
        self.assertEqual(self.network_attempts, [])

    def run_trace(self, **kwargs):
        report = replica.run_trace(request(**kwargs))
        self.assertEqual(report["status"], "COMPLETE_TRACE", report)
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["blocked_network_attempts"], [])
        return report

    @staticmethod
    def actions(report, port):
        return [item for item in report["actions"] if item["port"] == port]

    @staticmethod
    def persisted(report):
        return report["snapshots"][-1]["state"]

    @staticmethod
    def boundary_actions(report):
        ports = {"place_protection_orders", "cancel_algo_order", "place_order",
                 "cleanup_orphan_conditionals", "close_trade"}
        return [item for item in report["actions"] if item["port"] in ports]


class RequestContracts(HermeticCase):
    def test_config_is_complete_explicit_and_closed(self):
        for missing in config():
            invalid = config()
            del invalid[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                request(cfg=invalid)
        with self.assertRaises(ValueError):
            request(cfg=config(UNRECOGNIZED_LIVE_KNOB=1))

    def test_state_is_complete_explicit_and_closed(self):
        for missing in state():
            invalid = state()
            del invalid[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                request(initial=invalid)
        with self.assertRaises(ValueError):
            request(initial=state(timeframe_from_future="1m"))

    def test_numeric_bool_nan_infinity_and_invalid_geometry_are_rejected(self):
        for value in (True, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                request(cfg=config(PRE_TP1_PROTECT_LOCK_R=value))
            with self.subTest(state_value=repr(value)), self.assertRaises(ValueError):
                request(initial=state(entry_price=value))
        for change in ({"qty": -1.0}, {"entry_price": 0.0},
                       {"planned_stop": 101.0}, {"side": "unknown"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                request(initial=state(**change))

    def test_duplicate_event_ids_and_backwards_ticks_are_rejected(self):
        duplicate = [owned_tick(), owned_tick(at_ms=T0 + 1)]
        reversed_ticks = [owned_tick(event_id="late", at_ms=T0 + 1),
                          owned_tick(event_id="early", at_ms=T0)]
        for invalid in (duplicate, reversed_ticks):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                request(ticks=invalid)

    def test_future_or_inverted_response_availability_is_rejected(self):
        for change in ({"observed_at_ms": T0 + 1},
                       {"available_at_ms": T0 + 1},
                       {"observed_at_ms": T0, "available_at_ms": T0 - 1}):
            invalid = owned_tick()
            invalid["ports"]["get_positions"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                request(ticks=[invalid])

    def test_changed_config_state_response_or_hash_is_rejected(self):
        original = request()
        invalid_requests = []
        changed = copy.deepcopy(original)
        changed["config"]["PRE_TP1_PROTECT_LOCK_R"] = 0.4
        invalid_requests.append(changed)
        changed = copy.deepcopy(original)
        changed["initial_state"]["qty"] = 9.0
        invalid_requests.append(changed)
        changed = copy.deepcopy(original)
        changed["ticks"][0]["ports"]["fetch_ticker"][0]["value"]["last"] = 104.0
        invalid_requests.append(changed)
        changed = copy.deepcopy(original)
        changed["source_fingerprint"] = "0" * 64
        invalid_requests.append(changed)
        changed = copy.deepcopy(original)
        changed["unexpected_receipt_authority"] = True
        invalid_requests.append(changed)
        for invalid in invalid_requests:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replica.validate_request(invalid)

    def test_source_revision_is_bound_before_the_worker_runs(self):
        original = request()
        self.assertEqual(replica.validate_request(
            original, expected_source_fingerprint=original["source_fingerprint"]), original)
        with self.assertRaises(ValueError):
            replica.validate_request(original, expected_source_fingerprint="0" * 64)

    def test_legacy_replay_manifests_and_acceptance_never_admit_this_engine(self):
        from services import offline_replay_service as legacy
        from services import preselection_experiment_service as catalog
        from services import research_acceptance_service as acceptance
        req = request()
        for incompatible in (req, req["config"]):
            self.assertFalse(catalog.validate_replay_manifest(incompatible)["ok"])
        with self.assertRaises((TypeError, ValueError)):
            legacy.ReplayConfig(**req["config"])
        for purpose in ("PROMOTION", "CANARY", "STRUCTURAL"):
            self.assertFalse(acceptance.verify_acceptance(
                req, now_ms=T0, purpose=purpose)["ok"])

    def test_unknown_ports_and_nested_nonfinite_values_are_rejected(self):
        with self.assertRaises(ValueError):
            request(ticks=[owned_tick(real_exchange_post=[{"ok": True}])])
        for value in (math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                request(ticks=[owned_tick(fetch_ticker=[{"last": value}])])


class ActualCallerParity(HermeticCase):
    def test_report_never_claims_economics_protection_or_live_authority(self):
        report = self.run_trace()
        self.assertIsNone(report["economics"])
        self.assertIs(report["live_equivalent"], False)
        self.assertIs(report["promotable"], False)
        self.assertIs(report["authorizes_live"], False)
        self.assertEqual([item["port"] for item in self.boundary_actions(report)],
                         ["place_protection_orders", "cancel_algo_order"])

    def test_repeated_execution_is_deterministic_and_does_not_mutate_inputs(self):
        req = request()
        before = copy.deepcopy(req)
        first, second = replica.run_trace(req), replica.run_trace(req)
        self.assertEqual(req, before)
        self.assertEqual(first, second)

    def test_pre_tp1_long_equality_activates_and_below_threshold_does_not(self):
        for mark, moves in ((102.999999, False), (103.0, True), (103.000001, True)):
            ports = {"fetch_ticker": [{"last": mark}]}
            event = pre_tick(mark) if moves else owned_tick(**ports)
            with self.subTest(mark=mark):
                report = self.run_trace(ticks=[event])
                actions = self.actions(report, "place_protection_orders")
                self.assertEqual(bool(actions), moves)
                if moves:
                    self.assertEqual(actions[0]["kwargs"]["stop_loss"], 99.0)
                    self.assertEqual(self.persisted(report)["sl_current_price"], 99.0)

    def test_pre_tp1_short_is_symmetric_at_numeric_boundary(self):
        initial = state(side="short", planned_stop=102.0, planned_tp1=95.0,
                        planned_tp2=90.0, sl_current_price=102.0)
        for mark, moves in ((97.000001, False), (97.0, True), (96.999999, True)):
            event = pre_tick(mark, side="short") if moves else owned_tick(
                side="short", fetch_ticker=[{"last": mark}])
            with self.subTest(mark=mark):
                report = self.run_trace(initial=initial, ticks=[event])
                actions = self.actions(report, "place_protection_orders")
                self.assertEqual(bool(actions), moves)
                if moves:
                    self.assertEqual(actions[0]["kwargs"]["stop_loss"], 101.0)

    def test_failed_new_stop_never_cancels_old_protection(self):
        report = self.run_trace(ticks=[pre_tick(succeeds=False)])
        self.assertEqual(len(self.actions(report, "place_protection_orders")), 1)
        self.assertEqual(self.actions(report, "cancel_algo_order"), [])
        self.assertEqual(self.persisted(report)["sl_order_id"], "SL-OLD")
        self.assertEqual(self.persisted(report)["sl_current_price"], 98.0)

    def test_pre_tp1_is_idempotent_across_ticks_and_persisted_restart(self):
        report = self.run_trace(ticks=[pre_tick(), owned_tick(event_id="event-2", at_ms=T0 + 1)])
        self.assertEqual(len(self.actions(report, "place_protection_orders")), 1)
        restarted = state(sl_current_price=99.0, sl_order_id="SL-NEW")
        resumed = self.run_trace(initial=restarted, ticks=[owned_tick()])
        self.assertEqual(resumed["actions"], [])
        self.assertEqual(self.persisted(resumed)["sl_current_price"], 99.0)

    def test_unknown_initial_position_or_mark_never_infers_flat_or_zero(self):
        for value in ({"ok": False, "positions": None},
                      {"ok": True, "positions": [], "stale": True},
                      {"ok": True, "positions": [], "rate_limited": True}, None):
            event = tick(ownership_guard=[{"allowed": True}], get_positions=[value])
            with self.subTest(value=value):
                report = self.run_trace(ticks=[event])
                self.assertEqual(report["actions"], [])
                self.assertEqual(self.persisted(report)["status"], "open")
        report = self.run_trace(ticks=[owned_tick(fetch_ticker=[None])])
        self.assertEqual(report["actions"], [])

    def test_tp1_detection_uses_fixed_inclusive_point_six_not_adaptive_partial(self):
        cfg = config(PRE_TP1_PROTECT_ENABLED=False)
        for qty, transitions in ((6.000001, False), (6.0, True), (5.5, True)):
            event = be_tick(qty) if transitions else owned_tick(qty=qty)
            with self.subTest(qty=qty):
                report = self.run_trace(cfg=cfg, initial=state(adaptive_tp1_qty_pct=0.2),
                                        ticks=[event])
                self.assertEqual(self.persisted(report)["phase"],
                                 "post_tp1" if transitions else "pre_tp1")
                if transitions:
                    self.assertEqual(self.persisted(report)["qty"], qty)
                    self.assertEqual(self.actions(report, "place_protection_orders")[0]
                                     ["kwargs"]["stop_loss"], 100.0)
        # LIVE only freezes on the INITIAL UNKNOWN. A second read in the BE
        # transition can fallback to persisted qty/entry; reproduce, don't fix.
        event = be_tick()
        event["ports"]["get_positions"][1]["value"] = {"ok": False, "positions": None}
        report = self.run_trace(cfg=cfg, ticks=[event])
        self.assertEqual(self.persisted(report)["phase"], "post_tp1")
        self.assertEqual(self.actions(report, "place_protection_orders")[0]["kwargs"]["qty"], 5.5)

    def test_structural_be_clamps_to_actual_partial_financing_both_sides(self):
        for side, stop, tp1, current, expected in (
                ("long", 98.0, 100.4, 100.5, 99.67272727),
                ("short", 102.0, 99.6, 99.5, 100.32727273)):
            initial = state(side=side, planned_stop=stop, planned_tp1=tp1,
                            planned_tp2=110.0 if side == "long" else 90.0,
                            sl_current_price=stop)
            frame = candles(count=150, high=101.0, low=99.0, close=current)
            with self.subTest(side=side):
                report = self.run_trace(cfg=config(PRE_TP1_PROTECT_ENABLED=False),
                                        initial=initial, ticks=[be_tick(side=side, candles=frame)])
                self.assertEqual(self.actions(report, "place_protection_orders")[0]
                                 ["kwargs"]["stop_loss"], expected)

    def test_pre_tp1_runs_before_partial_detection_in_the_same_tick(self):
        event = be_tick()
        event["ports"]["fetch_ticker"] = tick(fetch_ticker=[{"last": 103.0}])["ports"]["fetch_ticker"]
        event["ports"]["place_protection_orders"].insert(0,
            {"observed_at_ms": T0, "available_at_ms": T0,
             "value": {"sl_ok": True, "sl_order_id": "SL-PRE"}})
        event["ports"]["cancel_algo_order"].append(
            {"observed_at_ms": T0, "available_at_ms": T0, "value": {"ok": True}})
        report = self.run_trace(ticks=[event])
        stops = self.actions(report, "place_protection_orders")
        self.assertEqual([item["kwargs"]["stop_loss"] for item in stops], [99.0, 100.0])
        self.assertEqual([item["port"] for item in self.boundary_actions(report)],
                         ["place_protection_orders", "cancel_algo_order",
                          "place_protection_orders", "cancel_algo_order"])

    def test_runner_transition_is_inclusive_enabled_only_and_trails_immediately(self):
        initial = state(phase="post_tp1", qty=5.5, sl_current_price=99.0)
        for enabled, qty, armed in ((True, 3.500001, False), (True, 3.5, True),
                                    (True, 2.0, True), (False, 2.0, False)):
            extra = {"fetch_ohlcv": [None]} if armed else {}
            with self.subTest(enabled=enabled, qty=qty):
                report = self.run_trace(cfg=config(PRE_TP1_PROTECT_ENABLED=False,
                                                    RUNNER_ENABLED=enabled),
                                        initial=initial, ticks=[owned_tick(qty=qty, **extra)])
                self.assertEqual(self.persisted(report)["phase"],
                                 "runner" if armed else "post_tp1")
                if armed:
                    self.assertEqual(self.persisted(report)["qty"], qty)
                    self.assertTrue(any(item["port"] == "fetch_ohlcv"
                                        for item in report["reads"]))

    def test_runner_requires_strict_min_step_and_safe_side_of_mark(self):
        for side, old_stop, close, moves in (
                ("long", 99.5, 101.0, False),
                ("long", 99.499999, 101.0, True),
                ("long", 99.0, 100.0, False),
                ("short", 100.5, 99.0, False),
                ("short", 100.500001, 99.0, True),
                ("short", 101.0, 100.0, False)):
            frame = candles(high=102.0 if side == "long" else 100.0,
                            low=100.0 if side == "long" else 98.0, close=close)
            extra = {"fetch_ohlcv": [frame]}
            if moves:
                extra.update(place_protection_orders=[{"sl_ok": True, "sl_order_id": "SL-RUN"}],
                             cancel_algo_order=[{"ok": True}])
            initial = state(side=side, phase="runner", qty=2.0,
                            planned_stop=98.0 if side == "long" else 102.0,
                            planned_tp1=105.0 if side == "long" else 95.0,
                            planned_tp2=110.0 if side == "long" else 90.0,
                            sl_current_price=old_stop, adaptive_runner_atr_mult=1.0)
            with self.subTest(side=side, old_stop=old_stop, close=close):
                report = self.run_trace(cfg=config(RUNNER_ENABLED=True,
                                                    PRE_TP1_PROTECT_ENABLED=False),
                                        initial=initial,
                                        ticks=[owned_tick(qty=2.0, side=side, **extra)])
                actions = self.actions(report, "place_protection_orders")
                self.assertEqual(bool(actions), moves, report)
                if moves:
                    self.assertEqual(actions[0]["kwargs"]["stop_loss"], 100.0)

    def test_time_stop_threshold_equality_and_post_tp1_exemption(self):
        cfg = config(TIME_STOP_ENABLED=True, PRE_TP1_PROTECT_ENABLED=False)
        for age_ms, triggered in ((240 * 60_000 - 1, False), (240 * 60_000, True)):
            extra = {"place_order": [{"ok": True}]} if triggered else {}
            event = owned_tick(**extra)
            if triggered:
                event["ports"]["get_positions"].append(
                    {"observed_at_ms": T0, "available_at_ms": T0,
                     "value": positions(10.0)})
            with self.subTest(age_ms=age_ms):
                report = self.run_trace(cfg=cfg, initial=state(opened_at_ms=T0 - age_ms),
                                        ticks=[event])
                orders = self.actions(report, "place_order")
                self.assertEqual(bool(orders), triggered)
                if triggered:
                    self.assertIs(orders[0]["kwargs"]["reduce_only"], True)
                    self.assertEqual(self.persisted(report)["status"], "open")
        for phase in ("post_tp1", "runner"):
            report = self.run_trace(cfg=cfg, initial=state(phase=phase,
                opened_at_ms=T0 - 999 * 60_000), ticks=[owned_tick()])
            self.assertEqual(report["actions"], [])

    def test_time_stop_uses_actual_timeframe_categories_and_missing_open_time(self):
        cfg = config(TIME_STOP_ENABLED=True, TIME_STOP_SCALP_MIN=2,
                     TIME_STOP_DAY_MIN=3, TIME_STOP_SWING_MIN=4,
                     PRE_TP1_PROTECT_ENABLED=False)
        # The production lower() maps monthly '1M' to '1m'. Preserve and label
        # this behavior, not an assertion that the convention is desirable.
        for tf, threshold in (("1m", 2), ("15m", 2), ("30m", 3),
                              ("2h", 3), ("4h", 4), (None, 3), ("1M", 2)):
            for age, triggered in ((threshold * 60_000 - 1, False),
                                   (threshold * 60_000, True)):
                event = owned_tick()
                if triggered:
                    event["ports"]["place_order"] = tick(place_order=[{"ok": True}])["ports"]["place_order"]
                    event["ports"]["get_positions"].extend(
                        tick(get_positions=[positions()])["ports"]["get_positions"])
                with self.subTest(timeframe=tf, age=age):
                    report = self.run_trace(cfg=cfg,
                        initial=state(timeframe=tf, opened_at_ms=T0 - age), ticks=[event])
                    self.assertEqual(bool(self.actions(report, "place_order")), triggered)
        report = self.run_trace(cfg=cfg, initial=state(opened_at_ms=None), ticks=[owned_tick()])
        self.assertEqual(self.actions(report, "place_order"), [])

    def test_time_stop_acceptance_never_proves_flat_and_unknown_keeps_bracket(self):
        cfg = config(TIME_STOP_ENABLED=True, PRE_TP1_PROTECT_ENABLED=False)
        for after in (positions(3.0), {"ok": False, "positions": None}):
            event = tick(ownership_guard=[{"allowed": True}],
                         get_positions=[positions(), after], place_order=[{"ok": True}])
            with self.subTest(after=after):
                report = self.run_trace(cfg=cfg,
                    initial=state(opened_at_ms=T0 - 240 * 60_000), ticks=[event])
                self.assertEqual(self.persisted(report)["status"], "open")
                self.assertEqual(self.persisted(report)["sl_order_id"], "SL-OLD")
                self.assertEqual(self.actions(report, "cancel_algo_order"), [])
                self.assertEqual(self.actions(report, "cleanup_orphan_conditionals"), [])

    def test_time_stop_precedes_partial_detection_and_flat_requires_cleanup(self):
        cfg = config(TIME_STOP_ENABLED=True, PRE_TP1_PROTECT_ENABLED=False)
        event = tick(ownership_guard=[{"allowed": True}],
                     get_positions=[positions(5.5), positions(0), positions(0)],
                     place_order=[{"ok": False, "error": "SYNTHETIC_TIMEOUT"}],
                     cleanup_orphan_conditionals=[{"confirmed_absent": True}],
                     close_trade=[{"applied": True}])
        report = self.run_trace(cfg=cfg, initial=state(opened_at_ms=T0 - 240 * 60_000),
                                ticks=[event])
        self.assertEqual(self.persisted(report)["status"], "closed_manual")
        self.assertEqual(self.actions(report, "place_protection_orders"), [])
        self.assertEqual([item["port"] for item in self.boundary_actions(report)],
                         ["place_order", "cleanup_orphan_conditionals", "close_trade"])
        self.assertIn("time_stop", self.persisted(report)["notes"])

    def test_autoheal_unknown_order_read_does_not_invent_vanished_legs(self):
        cfg = config(PROTECTION_AUTOHEAL_ENABLED=True,
                     PROTECTION_HEAL_GRACE_SECONDS=0.0,
                     PRE_TP1_PROTECT_ENABLED=False)
        report = self.run_trace(cfg=cfg, ticks=[owned_tick(
            get_open_algo_orders=[{"ok": False, "orders": None}])])
        self.assertEqual(report["actions"], [])
        self.assertEqual(self.persisted(report)["sl_order_id"], "SL-OLD")

    def test_omitted_boundary_receipts_remain_gaps_even_when_caller_swallows_them(self):
        for event in (tick(), tick(ownership_guard=[{"allowed": True}]),
                      owned_tick(fetch_ticker=[{"last": 103.0}])):
            report = replica.run_trace(request(ticks=[event]))
            self.assertEqual(report["status"], "INCOMPLETE_TRACE", report)
            self.assertTrue(report["gaps"])
            self.assertIsNone(report["economics"])

    def test_unconsumed_receipts_are_reported_not_silently_used(self):
        event = tick(ownership_guard=[{"allowed": True}],
                     get_positions=[{"ok": False, "positions": None}],
                     fetch_ticker=[{"last": 103.0}])
        report = self.run_trace(ticks=[event])
        self.assertTrue(report["unused_responses"])
        self.assertEqual(report["actions"], [])


class Isolation(HermeticCase):
    def test_runtime_and_models_are_bound_to_source_identity(self):
        self.assertIn("models/trade_signal.py", replica.SOURCE_FILES)
        self.assertIn("models/recommendation_snapshot.py", replica.SOURCE_FILES)
        self.assertIn("pydantic", replica.runtime_identity()["packages"])
        self.assertIn("pydantic_core", replica.runtime_identity()["packages"])
        req = request()
        with patch.object(replica, "source_fingerprint", side_effect=[
                req["source_fingerprint"], "0" * 64]):
            with self.assertRaisesRegex(ValueError, "DRIFT_DURING_EXECUTION"):
                replica.run_trace(req)

    def test_parent_synthetic_credentials_are_not_forwarded_to_worker(self):
        fake_credentials = {"DATABASE_URL": "postgresql://synthetic.invalid/test_only",
                            "BINANCE_API_KEY": "SYNTHETIC_KEY_DO_NOT_USE",
                            "BINANCE_API_SECRET": "SYNTHETIC_SECRET_DO_NOT_USE"}
        with patch.dict(os.environ, fake_credentials), patch.object(
                subprocess, "run", wraps=subprocess.run) as spawn:
            report = self.run_trace()
        self.assertTrue(spawn.called)
        for call in spawn.call_args_list:
            for key in fake_credentials:
                self.assertNotIn(key, call.kwargs["env"])
        self.assertIs(report["isolation"]["input_credentials_inherited"], False)
        self.assertIs(report["isolation"]["real_environment_inherited"], False)
        self.assertEqual(report["isolation"]["database"], "memory_only")

    def test_worker_does_not_replace_parent_execution_modules(self):
        names = ("db", "services.trade_manager_service", "services.exchange_service",
                 "services.binance_signed_service", "services.real_trade_service",
                 "services.notification_service", "services.manual_position_service")
        before = {name: sys.modules.get(name) for name in names}
        report = self.run_trace()
        after = {name: sys.modules.get(name) for name in names}
        self.assertEqual(before, after)
        self.assertIs(report["isolation"]["actual_exchange_clients_imported"], False)


def reseal(req):
    req["trace_hash"] = replica.digest({k: v for k, v in req.items() if k != "trace_hash"})
    return req


class BoundariesAndState(HermeticCase):
    def test_unbound_preview_is_never_evidence_and_never_accepts_operator_data(self):
        req = request()
        req["binding_mode"] = "SYNTHETIC_PREVIEW"
        for event in req["ticks"]:
            for responses in event["ports"].values():
                for response in responses:
                    response.update(call_index=None, request_hash=None, after_action_index=None)
        report = replica.run_trace(reseal(req))
        self.assertEqual(report["status"], "INCOMPLETE_TRACE")
        self.assertIn("UNBOUND_SYNTHETIC_PREVIEW_NOT_EVIDENCE",
                      [g["reason"] for g in report["gaps"]])
        req["source"] = "OPERATOR_EVENT_TRACE"
        with self.assertRaisesRegex(ValueError, "PREVIEW_NOT_OPERATOR_EVIDENCE"):
            replica.validate_request(reseal(req))

    def test_resealed_wrong_request_and_causal_identity_are_refused(self):
        for port, field, value in (
                ("get_positions", "request_hash", replica.call_hash(
                    "get_positions", [], {"symbol": "OTHERUSDC", "force": True})),
                ("place_protection_orders", "request_hash", "0" * 64),
                ("cancel_algo_order", "after_action_index", 0),
                ("cancel_algo_order", "call_index", 999)):
            req = request()
            req["ticks"][0]["ports"][port][0][field] = value
            with self.subTest(port=port, field=field):
                report = replica.run_trace(reseal(req))
                self.assertEqual(report["status"], "INCOMPLETE_TRACE")
                self.assertIn("RECEIPT_CALL_BINDING_MISMATCH",
                              [g["reason"] for g in report["gaps"]])
                self.assertIsNone(report["economics"])

    def test_pre_order_position_cannot_confirm_post_order_flat(self):
        cfg = config(TIME_STOP_ENABLED=True, PRE_TP1_PROTECT_ENABLED=False)
        event = tick(ownership_guard=[{"allowed": True}],
            get_positions=[positions(), positions(0)], place_order=[{"ok": True}])
        req = request(cfg=cfg, initial=state(opened_at_ms=T0 - 240 * 60_000), ticks=[event])
        req["ticks"][0]["ports"]["get_positions"][1]["observed_at_ms"] = T0 - 1
        report = replica.run_trace(reseal(req))
        self.assertEqual(report["status"], "INCOMPLETE_TRACE")
        self.assertIn("POSITION_OBSERVED_BEFORE_ACTION", [g["reason"] for g in report["gaps"]])
        self.assertEqual(self.persisted(report)["status"], "open")
        self.assertEqual(self.actions(report, "cleanup_orphan_conditionals"), [])
        self.assertEqual(self.actions(report, "close_trade"), [])

    def test_missing_position_completeness_is_not_flat(self):
        for value in ({"ok": True}, {"ok": True, "positions": None},
                      {"ok": True, "positions": [{"symbol": SYMBOL, "side": "Buy"}]},
                      {"ok": True, "positions": [{"size": 1.0, "symbol": SYMBOL}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                request(ticks=[tick(ownership_guard=[{"allowed": True}], get_positions=[value])])

    def test_created_at_controls_healing_grace_instead_of_opened_at(self):
        cfg = config(PROTECTION_AUTOHEAL_ENABLED=True,
            PROTECTION_VERIFY_LIVE=False, PRE_TP1_PROTECT_ENABLED=False)
        for created, opened, heals in ((T0 - 59_999, T0 - 600_000, False),
                                      (T0 - 60_000, T0 - 1_000, True),
                                      (None, T0 - 60_000, True)):
            extra = {"place_protection_orders": [{"sl_ok": True, "sl_order_id": "SL-HEALED"}]} if heals else {}
            with self.subTest(created=created, opened=opened):
                report = self.run_trace(cfg=cfg,
                    initial=state(created_at_ms=created, opened_at_ms=opened, sl_order_id=None),
                    ticks=[owned_tick(**extra)])
                self.assertEqual(bool(self.actions(report, "place_protection_orders")), heals)
                self.assertEqual(self.persisted(report)["sl_order_id"], "SL-HEALED" if heals else None)

    def test_cleanup_prefix_is_preserved_even_without_exact_ids(self):
        initial = state(client_order_id="cw-synthetic-17", sl_order_id=None,
                        tp1_order_id=None, tp2_order_id=None)
        event = owned_tick(qty=0, get_executions=[{"ok": True, "fills": []}],
            cleanup_orphan_conditionals=[{"confirmed_absent": True}], close_trade=[{"applied": True}])
        report = self.run_trace(cfg=config(PRE_TP1_PROTECT_ENABLED=False), initial=initial, ticks=[event])
        self.assertEqual(self.actions(report, "cleanup_orphan_conditionals")[0]["args"][1], "cw-synthetic-17")
        self.assertEqual(self.persisted(report)["status"], "closed_stop")
        event["ports"]["cleanup_orphan_conditionals"][0]["value"] = {"confirmed_absent": False}
        report = self.run_trace(cfg=config(PRE_TP1_PROTECT_ENABLED=False), initial=initial, ticks=[event])
        self.assertEqual(self.persisted(report)["status"], "open")
        self.assertEqual(self.actions(report, "close_trade"), [])


if __name__ == "__main__":
    unittest.main()
