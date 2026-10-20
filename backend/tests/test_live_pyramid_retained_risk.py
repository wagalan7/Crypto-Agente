"""Reforço confirmado conserva risco até fechar, mas não slot nem margem.

As fontes antigas seguem intactas; o carry é um teto conservador, não P&L.
O probe PG opcional executa este mesmo contrato contra banco descartável.
"""
import asyncio
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from services import financial_risk_service as financial

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row, self.calls = row, []

    async def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        return _Result(self.row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class RetainedRiskTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_retains_only_risk_for_confirmed_pyramid(self):
        session = _Session({"as_of": NOW, "fechadas": [], "abertas": [],
                            "reservas": [
                                {"intent_key": "old", "reserved_risk_usd": 10,
                                 "reserved_margin_usd": 0, "risk_only": True},
                                {"intent_key": "pending", "reserved_risk_usd": 6,
                                 "reserved_margin_usd": 4, "risk_only": False}]})
        with patch("services.financial_total_service.accounting_total_enabled", return_value=False):
            result = await financial.admission_snapshot(
                session, account_ref=ACCOUNT, exchange="binance", exclude_intent_key="self")
        self.assertEqual(result["pending_risk_usd"], 16)
        self.assertEqual(result["pending_margin_usd"], 4)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["open_positions"], 0)
        self.assertEqual(len(session.calls), 1)
        sql, params = session.calls[0]
        self.assertIn("statement_timestamp()", str(sql))
        self.assertIn("UNION ALL", str(sql))
        self.assertEqual(params["exclude_key"], "self")

    async def test_confirmed_risk_without_pending_has_zero_slots_and_margin(self):
        session = _Session({"as_of": NOW, "fechadas": [], "abertas": [],
                            "reservas": [{"reserved_risk_usd": 10,
                                          "reserved_margin_usd": 0, "risk_only": True}]})
        with patch("services.financial_total_service.accounting_total_enabled", return_value=False):
            result = await financial.admission_snapshot(session, account_ref=ACCOUNT)
        self.assertEqual((result["pending_count"], result["pending_margin_usd"],
                          result["pending_risk_usd"]), (0, 0, 10))

    async def test_invalid_confirmed_risk_never_disappears_among_positive_reserves(self):
        for invalid in (None, -2, float("nan"), float("inf"), float("-inf"), True, "10"):
            with self.subTest(invalid=invalid):
                session = _Session({"as_of": NOW, "fechadas": [], "abertas": [],
                                    "reservas": [{"reserved_risk_usd": invalid, "risk_only": True},
                                                 {"reserved_risk_usd": 6, "risk_only": False}]})
                with patch("services.financial_total_service.accounting_total_enabled", return_value=False):
                    result = await financial.admission_snapshot(session, account_ref=ACCOUNT)
                self.assertEqual(result["quality"], "UNKNOWN")
                self.assertIsNone(result["pending_risk_usd"])
                self.assertIsNone(result["open_rows"])

    async def test_zero_confirmed_risk_remains_known_zero(self):
        session = _Session({"as_of": NOW, "fechadas": [], "abertas": [],
                            "reservas": [{"reserved_risk_usd": 0, "reserved_margin_usd": 0,
                                          "risk_only": True}]})
        with patch("services.financial_total_service.accounting_total_enabled", return_value=False):
            result = await financial.admission_snapshot(session, account_ref=ACCOUNT)
        self.assertEqual((result["quality"], result["pending_risk_usd"], result["pending_count"]),
                         ("OK", 0, 0))

    async def test_native_orm_invalid_sum_is_unknown_not_zero(self):
        import db
        session = _Session((0, float("nan")))
        with patch.object(db, "DB_ENABLED", True), patch.object(db, "get_session", lambda: session):
            result = await financial.reserved_risk_usd(account_ref=ACCOUNT)
        self.assertEqual(result["quality"], "UNKNOWN")
        self.assertIsNone(result["value"])

    async def test_native_orm_retains_without_counting_new_trade(self):
        import db
        from sqlalchemy.dialects import postgresql
        session = _Session((0, 10))
        with patch.object(db, "DB_ENABLED", True), patch.object(db, "get_session", lambda: session):
            result = await financial.reserved_risk_usd(
                account_ref=ACCOUNT, exclude_intent_key="self")
        self.assertEqual(result["quality"], "OK")
        self.assertEqual((result["intents"], result["value"]), (0, 10))
        self.assertEqual(len(session.calls), 1)
        query = session.calls[0][0].compile(dialect=postgresql.dialect())
        self.assertIn("EXISTS", str(query))
        self.assertIn("CASE WHEN", str(query))
        self.assertIn("PYRAMID", query.params.values())
        self.assertIn("self", query.params.values())

    def test_budget_does_not_release_risk_after_confirmation(self):
        from services import entry_intent_service as intents
        budget = intents.DailyBudget(base_usd=-92, limit_usd=102, complete=True)
        self.assertIsNone(intents._budget_reason(budget, 0, 3))
        self.assertEqual(intents._budget_reason(budget, 10, 3), "DAILY_LOSS_LIMIT")

    def test_financial_gate_uses_same_retained_risk(self):
        snapshot = {"kill_daily": {"quality": "OK", "pnl_usd": -92},
                    "open_exposure": {"quality": "OK", "open_price_risk_usd": 0,
                                      "open_entry_fees_usd": 0}}
        result = financial.worst_case_daily_usd(
            snapshot, 3, reservations={"quality": "OK", "value": 10, "intents": 0})
        self.assertEqual(result["value"], -105)

    def test_sql_scope_identity_and_closed_release_are_explicit(self):
        sql = financial._ADMISSION_SNAPSHOT_SQL.split("UNION ALL", 1)[1]
        for fragment in ("i.account_ref = :account_ref", "i.exchange = :exchange",
                         "i.state = 'CONFIRMED'", "i.purpose = 'PYRAMID'",
                         "t.status = 'open'", "t.source = :source",
                         "t.exchange = i.exchange", "t.side = i.side",
                         "t.id = i.real_trade_id", "regexp_replace(upper(t.symbol)",
                         "0 AS reserved_margin_usd",
                         "true AS risk_only", "i.intent_key <> :exclude_key"):
            self.assertIn(fragment, sql)


async def _run_pg_probe():
    """Probe real somente com socket privado criado pelo runner desta execução."""
    import re
    import socket
    test_socket = os.environ.get("CW_PYRAMID_RISK_TEST_SOCKET", "")
    if not re.fullmatch(r"/tmp/cw-pyr-risk-sock\.[A-Za-z0-9]+", test_socket):
        raise RuntimeError("Socket descartável obrigatório")
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://pyr_risk@/pyr_risk_db?host=" + test_socket
    original_socket = socket.socket

    class UnixOnlySocket(original_socket):
        def connect(self, address):
            if self.family != socket.AF_UNIX or not str(address).startswith(test_socket + "/"):
                raise AssertionError("Rede/socket alheio bloqueado")
            return super().connect(address)

        def connect_ex(self, address):
            if self.family != socket.AF_UNIX or not str(address).startswith(test_socket + "/"):
                raise AssertionError("Rede/socket alheio bloqueado")
            return super().connect_ex(address)

    def block_dns(*args, **kwargs):
        raise AssertionError("DNS bloqueado")

    socket.socket, socket.getaddrinfo = UnixOnlySocket, block_dns
    import db
    from sqlalchemy import text, update
    from models.entry_intent import EntryIntent
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot
    checks = 0

    def check(value, label):
        nonlocal checks
        if not value:
            raise AssertionError(label)
        checks += 1

    try:
        async with db._engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all, tables=[
                RecommendationSnapshot.__table__, RealTrade.__table__, EntryIntent.__table__])
        now = datetime.now(timezone.utc)
        async with db.get_session() as session:
            trade = RealTrade(symbol="BETA/USDT:USDT", exchange="binance", side="long",
                              source="auto", status="open", qty=2, entry_price=100,
                              planned_stop=95, sl_current_price=100, sl_order_id="sl",
                              entry_fee=0, opened_at=now)
            session.add(trade)
            await session.flush()
            trade_id = trade.id
            session.add(EntryIntent(
                intent_key="b" * 64, client_order_id="cw-pyr-test", account_ref=ACCOUNT,
                exchange="binance", symbol="BETA-USDT-USDT", quote="USDT", side="long",
                position_side="BOTH", timeframe="4h", playbook=f"PYRAMID-{trade_id}-1",
                playbook_version="1", purpose="PYRAMID", setup_id="c" * 64,
                trigger_candle_ms=1000, payload_fingerprint="d" * 64, state="CONFIRMED",
                reserved_risk_usd=10, reserved_margin_usd=99, real_trade_id=trade_id,
                created_at=now, updated_at=now))
            await session.commit()

        async def observe(*, scope=ACCOUNT, exclude=None):
            with patch("services.financial_total_service.accounting_total_enabled", return_value=False):
                async with db.get_session() as session:
                    await session.execute(text("SELECT pg_advisory_xact_lock(917283)"))
                    return await financial.admission_snapshot(
                        session, account_ref=scope, exchange="binance", exclude_intent_key=exclude)

        async def mutate(**values):
            async with db.get_session() as session:
                await session.execute(update(RealTrade).where(RealTrade.id == trade_id).values(**values))
                await session.commit()

        async def mutate_risk(value):
            async with db.get_session() as session:
                await session.execute(update(EntryIntent).where(
                    EntryIntent.intent_key == "b" * 64).values(reserved_risk_usd=value))
                await session.commit()

        result = await observe()
        check(result["pending_risk_usd"] == 10, "risco confirmado preservado")
        check(result["pending_count"] == 0, "nenhum slot adicional")
        check(result["pending_margin_usd"] == 0, "margem não duplicada")
        check(result["base"]["value"] == 0, "base original BE preservada")
        native = await financial.reserved_risk_usd(account_ref=ACCOUNT)
        check(native["quality"] == "OK" and native["value"] == 10 and native["intents"] == 0,
              "ORM e snapshot concordam")
        check((await observe(exclude="b" * 64))["pending_risk_usd"] == 0, "própria reserva excluída")
        check((await observe(scope="e" * 64))["pending_risk_usd"] == 0, "outra conta excluída")
        await mutate_risk(0)
        check((await observe())["pending_risk_usd"] == 0, "zero registrado continua conhecido")
        native = await financial.reserved_risk_usd(account_ref=ACCOUNT)
        check(native["quality"] == "OK" and native["value"] == 0, "ORM aceita zero confirmado")
        # Outra reserva positiva não pode esconder negativo/NaN do reforço.
        async with db.get_session() as session:
            session.add(EntryIntent(
                intent_key="f" * 64, client_order_id="cw-pending-test", account_ref=ACCOUNT,
                exchange="binance", symbol="GAMA-USDT-USDT", quote="USDT", side="long",
                position_side="BOTH", timeframe="4h", playbook="CHAMPION_LEGACY",
                playbook_version="1", purpose="ENTRY", setup_id="c" * 64,
                trigger_candle_ms=1001, payload_fingerprint="d" * 64, state="RESERVED",
                reserved_risk_usd=6, reserved_margin_usd=4,
                created_at=now, updated_at=now))
            await session.commit()
        for invalid in (-2.0, float("nan"), float("inf")):
            await mutate_risk(invalid)
            check((await observe())["quality"] == "UNKNOWN", "snapshot não apaga risco inválido")
            native = await financial.reserved_risk_usd(account_ref=ACCOUNT)
            check(native["quality"] == "UNKNOWN" and native["value"] is None,
                  "ORM não mascara risco inválido com reserva positiva")
        async with db.get_session() as session:
            await session.execute(update(EntryIntent).where(EntryIntent.intent_key == "f" * 64)
                                  .values(state="TERMINAL"))
            await session.commit()
        await mutate_risk(10)
        await mutate(symbol="BETA/USDC:USDC")
        check((await observe())["pending_risk_usd"] == 0, "quote não colapsa")
        await mutate(symbol="BETA/USDT:USDT", side="short")
        check((await observe())["pending_risk_usd"] == 0, "lado diferente excluído")
        await mutate(side="long", source="manual")
        check((await observe())["pending_risk_usd"] == 0, "posição manual excluída")
        await mutate(source="auto", exchange="bybit")
        check((await observe())["pending_risk_usd"] == 0, "exchange diferente excluída")
        await mutate(exchange="binance", status="closed_be", closed_at=now, pnl_usd=0, exit_fee=0)
        check((await observe())["pending_risk_usd"] == 0, "fechamento libera sem escrita na intenção")
        native = await financial.reserved_risk_usd(account_ref=ACCOUNT)
        check(native["value"] == 0, "ORM libera somente após fechamento")
        print(f"PYRAMID_RETAINED_RISK_PG_OK {checks}")
    finally:
        await db._engine.dispose()


if __name__ == "__main__":
    if os.environ.get("CW_PYRAMID_RISK_TEST_SOCKET"):
        asyncio.run(_run_pg_probe())
    else:
        unittest.main()
