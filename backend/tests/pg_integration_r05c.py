"""R05C — integração PostgreSQL REAL (SQLAlchemy + asyncpg).

Exercita o CÓDIGO REAL num cluster PostgreSQL DESCARTÁVEL (socket unix, sem
TCP, nunca Railway): migração aditiva idempotente, merge transacional com
bloqueio de linha, concorrência entre dois "workers" e a guarda que impede o
`close_trade` legado de sobrescrever contabilidade confirmada.

Sai 0 apenas com `R05C_PG_INTEGRATION_OK`. `DATABASE_URL` é setado pelo runner.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


async def main() -> None:
    from db import get_session, init_db
    from models.real_trade import RealTrade
    from services import execution_accounting_service as ea
    from services import real_trade_service
    from sqlalchemy import select, text

    # ── 1. Migração aditiva IDEMPOTENTE pelo mecanismo real ─────────────────
    await init_db()
    await init_db()                      # rodar duas vezes não pode falhar
    async with get_session() as session:
        col = (await session.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='real_trades' AND column_name='execution_accounting'"
        ))).scalar_one_or_none()
    check("migracao_coluna_jsonb", col == "jsonb", f"data_type={col}")

    # ── 2. Trade novo NASCE no contrato R05C, em PENDING ────────────────────
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    created = await real_trade_service.open_trade(
        symbol="ALFA/USDT:USDT", side="long", qty=1.0, entry_price=100.0,
        source="auto", exchange="binance", exchange_order_id="100",
        client_order_id="cw-1", planned_stop=95.0, planned_tp1=105.0,
        planned_tp2=110.0)
    trade_id = created["id"]
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id))).scalar_one()
        acc = row.execution_accounting
    check("novo_trade_no_contrato", isinstance(acc, dict)
          and acc.get("schema_version") == ea.SCHEMA_VERSION
          and acc.get("state") == ea.STATE_PENDING, str(acc)[:80])

    # ── 3. Registro LEGADO permanece NULL e não é selecionado pelo retry ────
    legacy = await real_trade_service.open_trade(
        symbol="BETA/USDT:USDT", side="long", qty=1.0, entry_price=10.0,
        source="manual")
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == legacy["id"]))).scalar_one()
        check("legado_permanece_null", row.execution_accounting is None)
    pend = await ea.pending_trade_ids(limit=5)
    check("retry_ignora_legado", legacy["id"] not in pend, str(pend))
    check("retry_seleciona_novo", trade_id in pend, str(pend))

    # ── 4. Merge transacional: dois workers, respostas DIFERENTES ───────────
    def fill(exec_id, order_id, side, price, qty, realized, commission):
        raw = {"id": exec_id, "orderId": order_id, "symbol": "ALFAUSDT",
               "side": side, "positionSide": "BOTH", "price": price, "qty": qty,
               "realizedPnl": realized, "commission": commission,
               "commissionAsset": "USDT", "time": "1700000000000"}
        return ea.normalize_fill(raw, exchange="binance")[0]

    identity = ea.build_identity(exchange="binance", symbol="ALFA/USDT:USDT",
                                 side="long", entry_order_id="100")
    entrada = fill("1", "100", "BUY", "100", "1", "0", "0.04")
    saida = fill("2", "200", "SELL", "110", "1", "10", "0.04")
    order = ea.normalize_order({"orderId": "200", "clientOrderId": "cw-1-sl",
                                "status": "FILLED", "symbol": "ALFAUSDT",
                                "side": "SELL", "positionSide": "BOTH",
                                "executedQty": "1", "avgPrice": "110",
                                "reduceOnly": True, "type": "MARKET",
                                "updateTime": "1700000000000"})

    def payload(fills):
        acc = ea.merge_accounting(None, identity=identity, fills=fills,
                                  orders=[order])
        acc = ea.finalize_accounting(
            acc, entry_order_ids=["100"], exit_order_ids=["200"],
            fills_window_complete=True, funding_window_complete=True,
            planned_stop=95.0)
        acc["orders"]["200"]["role"] = "exit"
        return acc

    # workers concorrentes, cada um com METADE dos eventos
    await asyncio.gather(
        ea.apply_accounting(trade_id, payload([entrada])),
        ea.apply_accounting(trade_id, payload([saida])),
    )
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id))).scalar_one()
        acc = row.execution_accounting
    check("concorrencia_sem_perda_de_evento", len(acc.get("fills") or {}) == 2,
          str(sorted((acc.get("fills") or {}))))
    check("estado_confirmado", acc.get("state") == ea.STATE_CONFIRMED,
          f'{acc.get("state")}/{acc.get("reason_code")}')
    check("net_trade_correto",
          acc["totals"]["net_trade"] == "9.92", str(acc["totals"]["net_trade"]))
    check("projecao_pnl", abs((row.pnl_usd or 0) - 9.92) < 1e-9, str(row.pnl_usd))
    check("projecao_entry", abs((row.entry_price or 0) - 100.0) < 1e-9,
          str(row.entry_price))
    check("projecao_qty_initial", abs((row.qty_initial or 0) - 1.0) < 1e-9,
          str(row.qty_initial))

    # ── 5. Reaplicar o MESMO conjunto é idempotente (não duplica) ───────────
    await ea.apply_accounting(trade_id, payload([entrada, saida]))
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id))).scalar_one()
        acc2 = row.execution_accounting
    check("idempotente_apos_reaplicar",
          len(acc2.get("fills") or {}) == 2
          and acc2["totals"]["net_trade"] == "9.92",
          str(acc2["totals"]["net_trade"]))

    # ── 6. Mesmo exec_id com conteúdo CONFLITANTE não sobrescreve ───────────
    conflito = fill("2", "200", "SELL", "999", "1", "10", "0.04")
    await ea.apply_accounting(trade_id, payload([conflito]))
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id))).scalar_one()
        acc3 = row.execution_accounting
    guardado = (acc3.get("fills") or {}).get(saida["key"], {})
    check("conflito_preserva_primeiro", guardado.get("price") == "110",
          str(guardado.get("price")))
    check("conflito_marcado", bool(acc3.get("conflicts")), str(acc3.get("conflicts")))

    # ── 7. `close_trade` legado NÃO sobrescreve contabilidade confirmada ────
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id))).scalar_one()
        row.execution_accounting = {**acc2}          # volta ao estado CONFIRMED
        row.status = "open"
        await session.commit()
    antes_pnl, antes_exit = 9.92, 110.0
    fechado = await real_trade_service.close_trade(
        trade_id, exit_price=1.0, status="closed_stop", exit_fee=0.0,
        notes="tentativa legada")
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id))).scalar_one()
    check("close_legado_nao_sobrescreve_pnl",
          abs((row.pnl_usd or 0) - antes_pnl) < 1e-9, str(row.pnl_usd))
    check("close_legado_nao_sobrescreve_exit",
          abs((row.exit_price or 0) - antes_exit) < 1e-9, str(row.exit_price))
    check("close_legado_fecha_operacionalmente",
          row.status == "closed_stop" and row.closed_at is not None, str(row.status))

    # ── 8. Contabilidade PENDENTE não vira zero em coluna legada ────────────
    pendente = await real_trade_service.open_trade(
        symbol="GAMA/USDT:USDT", side="short", qty=2.0, entry_price=50.0,
        source="auto", exchange="binance", exchange_order_id="300",
        client_order_id="cw-3", planned_stop=52.0)
    acc_pend = ea.finalize_accounting(
        ea.merge_accounting(None, identity=ea.build_identity(
            exchange="binance", symbol="GAMA/USDT:USDT", side="short",
            entry_order_id="300")),
        entry_order_ids=["300"], fills_window_complete=False)
    await ea.apply_accounting(pendente["id"], acc_pend)
    async with get_session() as session:
        row = (await session.execute(
            select(RealTrade).where(RealTrade.id == pendente["id"]))).scalar_one()
    check("pendente_nao_projeta_pnl_zero", row.pnl_usd is None, str(row.pnl_usd))
    check("pendente_estado_explicavel",
          row.execution_accounting.get("state") in (ea.STATE_PENDING, ea.STATE_PARTIAL),
          str(row.execution_accounting.get("state")))

    print()
    if FAILURES:
        print(f"R05C_PG_INTEGRATION_FAILED: {FAILURES}")
        sys.exit(1)
    print("R05C_PG_INTEGRATION_OK")


if __name__ == "__main__":
    asyncio.run(main())
