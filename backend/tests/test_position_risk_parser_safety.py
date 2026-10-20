"""positionRisk real: desconhecido não é zero, cache não fabrica leitura fresca."""
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import binance_signed_service as bss


class PositionRiskParserSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.payload = []
        self.network_attempts = 0
        self.now = 1_800_000_000.0
        self.patches = [
            patch.object(bss, "is_configured", return_value=True),
            patch.object(bss, "_get_client",
                         return_value=SimpleNamespace(request=self.request)),
            patch.object(bss, "_build_signed_url",
                         side_effect=lambda path, params=None:
                         "https://synthetic.invalid" + path),
            patch.object(bss, "_positions_cache", {"data": None, "ts": 0.0}),
            patch.object(bss, "_ban_until_ms", 0),
            patch.object(bss, "_throttle_until_ms", 0),
            patch.object(bss, "_used_weight_1m", 0),
            patch.object(bss.time, "time", side_effect=lambda: self.now),
            patch.object(socket, "getaddrinfo", side_effect=self.no_network),
            patch.object(socket.socket, "connect", side_effect=self.no_network),
            patch.object(socket.socket, "connect_ex", side_effect=self.no_network),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def no_network(self, *args, **kwargs):
        self.network_attempts += 1
        raise AssertionError("Rede real proibida neste teste")

    async def asyncTearDown(self):
        self.assertEqual(self.network_attempts, 0)

    async def request(self, method, url):
        self.assertEqual(method, "GET")
        self.assertIn("/fapi/v2/positionRisk", url)
        self.calls.append((method, url))
        return SimpleNamespace(status_code=200, headers={},
                               json=lambda: self.payload)

    @staticmethod
    def row(amount="1", symbol="PARSERUSDT"):
        return {"symbol": symbol, "positionAmt": amount,
                "entryPrice": "100", "markPrice": "100",
                "unRealizedProfit": "0", "leverage": "5", "notional": "100",
                "positionSide": "BOTH", "updateTime": 1_700_000_000_000}

    def assert_unknown(self, response):
        self.assertIs(response.get("ok"), False, response)
        self.assertEqual(response.get("quality"), "UNKNOWN", response)
        self.assertIs(response.get("complete"), False, response)
        self.assertIsNone(response.get("positions"), response)
        self.assertEqual(response.get("reason_code"),
                         "POSITION_RISK_INVALID_PAYLOAD", response)

    async def test_unknown_http_body_never_becomes_empty_account(self):
        for payload in (None, False, True, 0, "", {}, {"symbol": "PARSERUSDT"}):
            with self.subTest(payload=payload):
                self.payload = payload
                self.assert_unknown(await bss.get_positions(force=True))
                self.assertIsNone(bss._positions_cache["data"])

    async def test_invalid_http_amount_never_becomes_zero(self):
        for amount in (None, False, True, "", " ", "NaN", "Infinity", "-Infinity",
                       float("nan"), float("inf"), [], {}, "1e9999", "1e-9999"):
            with self.subTest(amount=amount):
                self.payload = [self.row(amount)]
                self.assert_unknown(await bss.get_positions(force=True))
                self.assertIsNone(bss._positions_cache["data"])

    async def test_missing_amount_is_unknown(self):
        row = self.row()
        del row["positionAmt"]
        self.payload = [row]
        self.assert_unknown(await bss.get_positions(force=True))

    async def test_invalid_line_is_not_silently_discarded(self):
        for row in (None, "text", False, []):
            with self.subTest(row=row):
                self.payload = [row]
                self.assert_unknown(await bss.get_positions(force=True))

    async def test_invalid_row_outside_requested_symbol_invalidates_whole_read(self):
        self.payload = [self.row(), self.row(None, "OTHERUSDT")]
        self.assert_unknown(await bss.get_positions("PARSERUSDT", force=True))
        self.assertIsNone(bss._positions_cache["data"])

    async def test_explicit_empty_list_is_known_flat(self):
        self.payload = []
        response = await bss.get_positions(force=True)
        self.assertIs(response["ok"], True)
        self.assertEqual(response["positions"], [])
        self.assertEqual(response["count"], 0)
        self.assertEqual(bss._positions_cache["data"], [])

    async def test_numeric_zero_remains_known_flat_without_active_fields(self):
        for amount in ("0", "-0.0", 0, 0.0):
            with self.subTest(amount=amount):
                self.payload = [{"positionAmt": amount}]
                response = await bss.get_positions(force=True)
                self.assertIs(response["ok"], True)
                self.assertEqual(response["positions"], [])

    async def test_positive_and_negative_amounts_keep_long_short_and_abs_size(self):
        self.payload = [self.row("1.25"), self.row("-2.5", "SHORTUSDT")]
        response = await bss.get_positions(force=True)
        self.assertIs(response["ok"], True)
        self.assertEqual([(p["side"], p["size"]) for p in response["positions"]],
                         [("Buy", 1.25), ("Sell", 2.5)])
        self.assertEqual(response["positions"][0]["position_side"], "BOTH")

    async def test_valid_read_cache_and_symbol_filter_are_preserved(self):
        self.payload = [self.row("1"), self.row("-2", "OTHERUSDT")]
        response = await bss.get_positions(force=True)
        self.assertIs(response["ok"], True)
        cached = await bss.get_positions("OTHER/USDT:USDT")
        self.assertIs(cached["ok"], True)
        self.assertEqual(cached["count"], 1)
        self.assertEqual(cached["positions"][0]["size"], 2.0)
        self.assertEqual(len(self.calls), 1)

    async def test_forced_error_invalidates_freshness_until_new_valid_fetch(self):
        self.payload = [self.row("1")]
        good = await bss.get_positions(force=True)
        snapshot = bss._positions_cache["data"]
        self.assertIs(good["ok"], True)
        self.payload = None
        self.assert_unknown(await bss.get_positions(force=True))
        self.assertIs(bss._positions_cache["data"], snapshot)
        self.assert_unknown(await bss.get_positions())
        self.assertEqual(len(self.calls), 3)
        self.assertIs(bss._positions_cache["data"], snapshot)
        self.payload = [self.row("-2")]
        recovered = await bss.get_positions()
        self.assertIs(recovered["ok"], True)
        self.assertEqual(recovered["positions"][0]["side"], "Sell")
        self.assertEqual(recovered["positions"][0]["size"], 2.0)
        await bss.get_positions()
        self.assertEqual(len(self.calls), 4)

    async def test_invalid_data_does_not_overwrite_cache_with_partial_subset(self):
        self.payload = [self.row("1")]
        await bss.get_positions(force=True)
        snapshot = bss._positions_cache["data"]
        self.payload = [self.row("9"), self.row(None, "OTHERUSDT")]
        self.assert_unknown(await bss.get_positions(force=True))
        self.assertIs(bss._positions_cache["data"], snapshot)
        self.assertEqual(snapshot[0]["size"], 1.0)

    async def test_cooldown_can_only_serve_previous_snapshot_as_stale(self):
        self.payload = [self.row("1")]
        await bss.get_positions(force=True)
        self.payload = None
        self.assert_unknown(await bss.get_positions(force=True))
        bss._ban_until_ms = self.now * 1000 + 1000
        stale = await bss.get_positions()
        self.assertIs(stale["ok"], True)
        self.assertIs(stale["stale"], True)
        self.assertIs(stale["rate_limited"], True)
        self.assertEqual(len(self.calls), 2)

    async def test_invalid_active_field_does_not_leave_cache_fresh(self):
        self.payload = [self.row("1")]
        await bss.get_positions(force=True)
        snapshot = bss._positions_cache["data"]
        row = self.row("2")
        row["entryPrice"] = "invalid"
        self.payload = [row]
        self.assert_unknown(await bss.get_positions(force=True))
        self.assert_unknown(await bss.get_positions())
        self.assertIs(bss._positions_cache["data"], snapshot)
        self.assertEqual(len(self.calls), 3)

    async def test_expired_cache_diagnostic_has_no_infinity(self):
        self.payload = [self.row("1")]
        await bss.get_positions(force=True)
        self.payload = None
        self.assert_unknown(await bss.get_positions(force=True))
        diagnostic = bss.get_positions_ban_status()
        self.assertIsNone(diagnostic["cache_age_s"])
        json.dumps(diagnostic, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
