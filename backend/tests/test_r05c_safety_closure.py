"""R05C audit regressions. Synthetic account, fake GETs, no credentials/network."""
import asyncio
import copy
import inspect
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

from services import execution_accounting_service as ea
from tests import test_r05c_execution_accounting as fixtures
from tests.test_r05c_execution_accounting import _FakeClient, _view, NOW
from tests.test_r05c_execution_accounting import setUpModule, tearDownModule


def case():
    fixture = fixtures.Coleta()
    return _view(), _FakeClient(fills=fixture._fills(), orders=fixture._order())


class Safety(unittest.IsolatedAsyncioTestCase):
    async def test_complete_transport_and_persistence_contract(self):
        view, client = case()
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertEqual(acc["state"], "CONFIRMED", acc.get("reason_code"))
        merged = ea.merge_observation(view["execution_accounting"], acc, view=view)
        self.assertEqual(merged["state"], "CONFIRMED", merged)
        self.assertEqual(ea.project_to_trade_fields(merged)["pnl_usd"], 9.92)

    async def test_entry_ack_and_partial_not_terminal(self):
        for status in ("NEW", "PARTIALLY_FILLED", None, "MYSTERY"):
            view, client = case()
            client.orders["100"]["status"] = status
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertNotEqual(acc["state"], "CONFIRMED")
            self.assertIsNone(ea.project_to_trade_fields(acc)["pnl_usd"])

    async def test_terminal_qty_must_match_fills(self):
        for oid in ("100", "200"):
            view, client = case()
            client.orders[oid]["executedQty"] = "2"
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertNotEqual(acc["state"], "CONFIRMED")

    async def test_missing_entry_query_never_confirms(self):
        view, client = case()
        del client.orders["100"]
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertNotEqual(acc["state"], "CONFIRMED")

    async def test_late_exit_requeries_incomplete_trade(self):
        view, client = case()
        last = client.fills.pop()
        partial = await ea.collect_trade_accounting(view, client=client, now=NOW)
        view["execution_accounting"] = partial
        client.fills.append(last)
        done = await ea.collect_trade_accounting(view, client=client, now=NOW+timedelta(minutes=1))
        self.assertEqual(done["state"], "CONFIRMED", done)

    async def test_funding_wrong_market_or_asset_unavailable(self):
        for key, value in (("symbol", "OTHERUSDT"), ("asset", "USDC")):
            view, client = case()
            item = {"incomeType": "FUNDING_FEE", "tranId": "1", "symbol": "ALFAUSDT",
                    "asset": "USDT", "income": "1", "time": client.fills[0]["time"]}
            item[key] = value
            client.income = [item]
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertEqual(acc["state"], "CONFIRMED")
            self.assertIsNone(acc["totals"]["funding_net"])

    async def test_partial_updates_only_proven_entry_metadata(self):
        view, client = case()
        view.update(status="open", closed_at=None)
        client.fills = client.fills[:1]
        acc = await ea.collect_trade_accounting(view, client=client, now=view["opened_at"]+timedelta(hours=2))
        projected = ea.project_to_trade_fields(acc)
        self.assertEqual(projected["entry_price"], 100.)
        self.assertEqual(projected["qty_initial"], 1.)
        self.assertIsNone(projected["pnl_usd"])

    async def test_client_id_lookup_resolves_entry(self):
        view, client = case()
        view["exchange_order_id"] = None
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertEqual(acc["state"], "CONFIRMED")
        self.assertEqual(acc["identity"]["entry_order_id"], "100")

    async def test_false_reduce_only_cannot_upgrade_on_apply(self):
        for value in (False, "false", None, "True", 1):
            view, client = case()
            client.orders["200"]["reduceOnly"] = value
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertNotEqual(acc["state"], "CONFIRMED")
            merged = ea.merge_observation(view["execution_accounting"], acc, view=view)
            self.assertNotEqual(merged["state"], "CONFIRMED")

    async def test_identity_exchange_and_account_guards_no_calls(self):
        for key, value in (("exchange", "bybit"), ("account_scope", "other-account")):
            view, client = case()
            if key == "exchange":
                view[key] = value
            else:
                view["execution_accounting"]["identity"][key] = value
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertNotEqual(acc["state"], "CONFIRMED")
            self.assertEqual(client.calls, [])

    async def test_mismatched_row_rejects_without_merge(self):
        view, client = case()
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        for key, value in (("symbol", "BETAUSDT"), ("side", "short"),
                           ("exchange_order_id", "999"), ("client_order_id", "other")):
            target = {**view, key: value}
            self.assertIsNone(ea.merge_observation(view["execution_accounting"], acc, view=target))

    async def test_conflict_survives_collector_and_locked_merge(self):
        view, client = case()
        old = await ea.collect_trade_accounting(view, client=client, now=NOW)
        view["execution_accounting"] = old
        # Force re-observation of a previously incomplete window.
        view["execution_accounting"]["execution_proof"]["complete"] = False
        client.fills[1]["price"] = "999"
        observed = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertEqual(observed["state"], "CONFLICT")
        merged = ea.merge_observation(old, observed, view=view)
        self.assertEqual(merged["state"], "CONFLICT")
        self.assertEqual(list(merged["fills"].values())[1]["price"], "110")
        self.assertIsNone(ea.project_to_trade_fields(merged)["pnl_usd"])

    async def test_stale_partial_does_not_downgrade_confirmed(self):
        view, client = case()
        partial = await ea.collect_trade_accounting(view, client=_FakeClient(fills=client.fills[:1], orders=client.orders), now=NOW-timedelta(seconds=1))
        confirmed = await ea.collect_trade_accounting(view, client=client, now=NOW)
        view["execution_accounting"] = confirmed
        merged = ea.merge_observation(confirmed, partial, view=view)
        self.assertEqual(merged["state"], "CONFIRMED", merged)

    async def test_healthy_open_polls_do_not_exhaust_retry(self):
        view, client = case()
        view.update(status="open", closed_at=None)
        client.fills = client.fills[:1]
        for i in range(12):
            acc = await ea.collect_trade_accounting(view, client=client, now=view["opened_at"]+timedelta(hours=2, minutes=i))
            self.assertNotEqual(acc["state"], "FAILED")
            self.assertTrue(acc["coverage"]["entry_order_proven"])
            self.assertEqual(acc["attempts"], 0)
            self.assertIsNone(ea.project_to_trade_fields(acc)["pnl_usd"])
            view["execution_accounting"] = acc

    async def test_open_operational_trade_never_confirmed(self):
        view, client = case()
        view["status"] = "open"
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertNotEqual(acc["state"], "CONFIRMED")

    async def test_funding_failure_independent_and_retryable(self):
        view, client = case()
        client.income_error = "temporary failure"
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertEqual(acc["state"], "CONFIRMED")
        self.assertIsNone(acc["totals"]["funding_net"])
        self.assertTrue(ea.is_retry_due(acc, now=NOW+timedelta(days=1)))
        view["execution_accounting"] = acc
        client.income_error = None
        client.calls.clear()
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW+timedelta(days=1))
        self.assertEqual(acc["funding_state"], "CONFIRMED")
        self.assertEqual([c[1] for c in client.calls], ["income"])

    async def test_overlap_unknown_or_true_blocks_attribution(self):
        for exclusive in (False, None):
            view, client = case()
            view["exclusive_exposure"] = exclusive
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertNotEqual(acc["state"], "CONFIRMED")
            self.assertIsNone(acc["totals"]["funding_net"])

    async def test_funding_uses_exact_exposure_not_padding(self):
        view, client = case()
        begin, end = (int(f["time"]) for f in client.fills)
        client.income = [{"incomeType": "FUNDING_FEE", "tranId": str(i),
                          "symbol": "ALFAUSDT", "asset": "USDT", "income": "-1", "time": str(t)}
                         for i, t in enumerate((begin-1, begin+1, end+1), 1)]
        acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
        self.assertEqual(acc["totals"]["funding_net"], "-1")

    async def test_bad_fill_not_silently_omitted(self):
        for key, val in (("positionSide", None), ("time", None), ("qty", "NaN")):
            view, client = case()
            bad = dict(client.fills[1], id="bad")
            bad[key] = val
            async def raw_page(*a, **kw):
                return {"ok": True, "raw": client.fills+[bad]}
            client.get_executions = raw_page
            acc = await ea.collect_trade_accounting(view, client=client, now=NOW)
            self.assertNotEqual(acc["state"], "CONFIRMED")

    async def test_deadline_and_cancellation(self):
        view, client = case()
        async def slow(*a, **kw):
            await asyncio.sleep(10)
        client.get_executions = slow
        begin = time.monotonic()
        acc = await ea.collect_trade_accounting(view, client=client, budget=ea.ReadBudget(seconds=.03), now=NOW)
        self.assertLess(time.monotonic()-begin, .3)
        self.assertNotEqual(acc["state"], "CONFIRMED")
        task = asyncio.create_task(ea.collect_trade_accounting(view, client=client, now=NOW))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_rate_limit_stops_shared_budget(self):
        view, client = case()
        client.fills_error = "429 rate limit"
        budget = ea.ReadBudget()
        await ea.collect_trade_accounting(view, client=client, budget=budget, now=NOW)
        self.assertTrue(budget.stopped)
        self.assertEqual(len(client.calls), 1)

    async def test_financial_readers_exclude_numeric_pending(self):
        from services.financial_risk_service import financial_window
        row = {"id": 1, "source": "auto", "status": "closed_stop", "closed_at": NOW,
               "pnl_usd": 123, "execution_accounting": ea.empty_accounting()}
        result = financial_window([row], since=NOW-timedelta(days=1), until=NOW+timedelta(seconds=1), kind="daily")
        self.assertEqual(result["quality"], "UNKNOWN")

    def test_equal_decimal_representation_is_not_conflict(self):
        self.assertEqual(ea._conflicts({"price": "100"}, {"price": "100.0"}, ["price"]), [])

    def test_public_summary_contains_no_identity_or_fills(self):
        acc = ea.empty_accounting(identity={"account_scope": "secret-binding"})
        summary = ea.public_summary(acc)
        self.assertNotIn("identity", summary)
        self.assertNotIn("fills", summary)
        self.assertFalse(summary["financial_confirmed"])


if __name__ == "__main__":
    unittest.main()
