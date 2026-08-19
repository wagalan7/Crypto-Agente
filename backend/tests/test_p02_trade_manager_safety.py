"""P02: contratos fail-safe do gerenciador de operações reais."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import trade_manager_service as trade_manager  # noqa: E402
from services import shadow_trade_service as shadow_trade  # noqa: E402


class ExchangePositionFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_empty_response_is_unknown_not_flat(self):
        response = {"ok": True, "positions": [], "stale": True}

        with patch(
            "services.exchange_service.get_positions",
            AsyncMock(return_value=response),
        ):
            position = await trade_manager._fetch_exchange_position("BTCUSDT")

        self.assertEqual(position, (None, None))

    async def test_rate_limited_empty_response_is_unknown_not_flat(self):
        response = {"ok": True, "positions": [], "rate_limited": True}

        with patch(
            "services.exchange_service.get_positions",
            AsyncMock(return_value=response),
        ):
            position = await trade_manager._fetch_exchange_position("ETHUSDT")

        self.assertEqual(position, (None, None))

    async def test_fresh_empty_response_still_means_flat(self):
        response = {"ok": True, "positions": []}
        get_positions = AsyncMock(return_value=response)

        with patch(
            "services.exchange_service.get_positions",
            get_positions,
        ):
            position = await trade_manager._fetch_exchange_position("SOLUSDT")

        self.assertEqual(position, (0.0, None))
        get_positions.assert_awaited_once_with(symbol="SOLUSDT", force=True)

    async def test_client_without_force_support_uses_compatible_fallback(self):
        get_positions = AsyncMock(side_effect=[
            TypeError("unexpected keyword argument 'force'"),
            {"ok": True, "positions": [{"size": 0.5, "entry_price": 100.0}]},
        ])

        with patch("services.exchange_service.get_positions", get_positions):
            position = await trade_manager._fetch_exchange_position("XRPUSDT")

        self.assertEqual(position, (0.5, 100.0))
        self.assertEqual(get_positions.await_count, 2)


class ExecutionQuarantineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _trade(*, exchange: str, notes: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            id=77,
            symbol="BTCUSDT",
            exchange=exchange,
            notes=notes,
        )

    async def _assert_process_guarded(self, trade, *, active_exchange: str):
        from services import exchange_service

        fetch_qty = AsyncMock(return_value=1.0)
        is_paused = AsyncMock(return_value=False)
        set_pause = AsyncMock(return_value={"ok": True})
        arm_quarantine = MagicMock()

        with (
            patch.object(exchange_service, "ACTIVE_EXCHANGE", active_exchange),
            patch.object(trade_manager, "_fetch_exchange_qty", fetch_qty),
            patch(
                "services.shadow_trade_service._arm_execution_quarantine",
                arm_quarantine,
            ),
            patch("services.risk_service.is_paused", is_paused),
            patch("services.risk_service.set_manual_pause", set_pause),
        ):
            await trade_manager._process_trade(trade)

        fetch_qty.assert_not_awaited()
        arm_quarantine.assert_called_once()
        is_paused.assert_awaited_once()
        set_pause.assert_awaited_once()

    async def test_open_trade_failure_in_auto_mode_persists_quarantine(self):
        open_trade = AsyncMock(side_effect=RuntimeError("database unavailable"))
        persist_quarantine = AsyncMock()
        notify = AsyncMock()

        with (
            patch("services.real_trade_service.open_trade", open_trade),
            patch.object(
                shadow_trade,
                "_persist_execution_quarantine",
                persist_quarantine,
            ),
            patch("services.push_service.notify_alert", notify),
        ):
            result = await shadow_trade._open_trade_fail_closed(
                symbol="BTCUSDT",
                source="auto",
                side="long",
            )

        self.assertIsNone(result)
        persist_quarantine.assert_awaited_once()
        reason = persist_quarantine.await_args.args[0]
        self.assertIn("BTCUSDT", reason)
        self.assertIn("persistência falhou", reason)

    async def test_process_trade_blocks_non_binance_active_exchange(self):
        await self._assert_process_guarded(
            self._trade(exchange="bybit"),
            active_exchange="bybit",
        )

    async def test_process_trade_blocks_trade_from_different_exchange(self):
        await self._assert_process_guarded(
            self._trade(exchange="bybit"),
            active_exchange="binance",
        )

    async def test_pending_entry_order_freezes_before_position_lookup(self):
        await self._assert_process_guarded(
            self._trade(
                exchange="binance",
                notes="CRITICAL_EXECUTION_INCIDENT PENDING_ENTRY_ORDER",
            ),
            active_exchange="binance",
        )

    async def test_backfill_blocks_bybit_before_db_or_protection(self):
        from services import binance_signed_service, exchange_service

        get_session = MagicMock()
        place_protection = AsyncMock()
        with (
            patch.object(trade_manager, "DB_ENABLED", True),
            patch.object(exchange_service, "ACTIVE_EXCHANGE", "bybit"),
            patch.object(trade_manager, "get_session", get_session),
            patch.object(
                binance_signed_service,
                "place_protection_orders",
                place_protection,
            ),
        ):
            result = await trade_manager.backfill_protection()

        self.assertFalse(result["ok"])
        self.assertEqual(result["processed"], 0)
        self.assertIn("apenas para Binance", result["error"])
        get_session.assert_not_called()
        place_protection.assert_not_awaited()

    async def test_live_contract_rejects_env_typo_even_with_binance_dispatcher(self):
        from services import exchange_service

        with (
            patch.dict(os.environ, {"EXCHANGE": "binanec"}),
            patch.object(exchange_service, "ACTIVE_EXCHANGE", "binance"),
        ):
            allowed, reason = shadow_trade._live_execution_contract_guard()

        self.assertFalse(allowed)
        self.assertIn("inválida", reason)

    async def test_critical_incident_bypasses_autoheal_grace(self):
        from services import exchange_service

        trade = SimpleNamespace(
            id=88,
            symbol="ETHUSDT",
            exchange="binance",
            side="long",
            phase="pre_tp1",
            created_at=datetime.now(timezone.utc),
            opened_at=None,
            notes="CRITICAL_EXECUTION_INCIDENT: protection persistence unknown",
            sl_current_price=None,
            entry_price=100.0,
            planned_stop=95.0,
            planned_tp1=None,
            planned_tp2=None,
            sl_order_id=None,
            tp1_order_id=None,
            tp2_order_id=None,
            qty_initial=1.0,
            qty=1.0,
        )
        place_protection = AsyncMock(
            return_value={"sl_ok": False, "sl_msg": "exchange unavailable"}
        )

        with (
            patch.object(trade_manager, "PROTECTION_AUTOHEAL_ENABLED", True),
            patch.object(trade_manager, "PROTECTION_VERIFY_LIVE", False),
            patch.object(exchange_service, "ACTIVE_EXCHANGE", "binance"),
            patch.object(
                exchange_service,
                "place_protection_orders",
                place_protection,
                create=True,
            ),
        ):
            healed = await trade_manager._ensure_protection(trade, qty_now=1.0)

        self.assertFalse(healed)
        place_protection.assert_awaited_once()
        self.assertEqual(place_protection.await_args.args[:2], ("ETHUSDT", "Buy"))


if __name__ == "__main__":
    unittest.main()
