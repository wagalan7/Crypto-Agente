"""R05D — o TOTAL COM FUNDING participando do limite, pelo gate REAL.

`R05D_GATE_TEST_SOCKET` aponta para /tmp/cw-r05d-sock.* criado pelo runner.
O ledger é produzido pelo CÓDIGO do R05C (normalize_fill/normalize_funding/
merge_accounting/finalize_accounting) — não por um dicionário fabricado — e o
veredito vem de `shadow_trade_service._r05b_entry_gate`, o gate que o executor
usa antes do POST. Sem TCP/DNS, sem exchange, sem ordem.

Reprodução do defeito: `net_trade=+9.92`, `funding=-250`, total `-240.08`,
estado COMPLETE. Com limite de perda 100 e risco proposto 5, o pior cenário
EX-funding é `+4.92` e o gate liberava; com a fonte completa selecionada ele
precisa considerar `-245.08` e bloquear.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("R05D_GATE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r05d-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r05d@/r05ddb?host=" + test_socket
os.environ["R05_FINANCIAL_BREAKER_ENABLED"] = "true"     # cutover ligado no ensaio
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05D")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05D")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R05D")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

SCOPE = "s" * 64
CHECKS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


async def run():
    from unittest.mock import AsyncMock, patch
    import db
    from models.entry_intent import EntryIntent          # reservas do orçamento
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from services import execution_accounting_service as ea
    from services import financial_risk_service as frs
    from services import financial_total_service as fts
    from services import shadow_trade_service as sts

    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[RecommendationSnapshot.__table__, RealTrade.__table__,
                                        EntryIntent.__table__])

    now = datetime.now(timezone.utc)
    # A janela do P&L diário é CALENDÁRIO: ancorar o fechamento dentro da janela
    # vigente deixa o ensaio independente da hora em que ele roda (perto da
    # meia-noite UTC, `now - 2h` cairia no dia anterior e o total sumiria).
    closed_at = max(frs.kill_daily_start(now) + timedelta(minutes=2),
                    now - timedelta(hours=2))
    opened = closed_at - timedelta(minutes=1)

    def ms(moment):
        return str(int(moment.timestamp() * 1000))

    # ── Ledger REAL do R05C: entrada, saída e um funding grande e negativo ──
    identity = ea.build_identity(exchange="binance", symbol="ALFA/USDT:USDT",
                                 side="long", entry_order_id="100",
                                 entry_client_order_id="cw-r05d")
    identity["account_scope"] = SCOPE

    def fill(exec_id, order_id, side, price, qty, realized, commission, moment):
        raw = {"id": exec_id, "orderId": order_id, "symbol": "ALFAUSDT", "side": side,
               "positionSide": "BOTH", "price": price, "qty": qty,
               "realizedPnl": realized, "commission": commission,
               "commissionAsset": "USDT", "time": ms(moment)}
        item, why = ea.normalize_fill(raw, exchange="binance")
        assert item is not None, why
        return item

    entrada = fill("1", "100", "BUY", "100", "1", "0", "0.04", opened)
    saida = fill("2", "200", "SELL", "110", "1", "10", "0.04", closed_at)
    funding_raw = {"incomeType": "FUNDING_FEE", "tranId": "9001", "income": "-250",
                   "asset": "USDT", "symbol": "ALFAUSDT",
                   "time": ms(opened + timedelta(hours=1))}
    funding, why = ea.normalize_funding(funding_raw)
    assert funding is not None, why

    order = ea.normalize_order({"orderId": "200", "clientOrderId": "cw-r05d-sl",
                                "status": "FILLED", "symbol": "ALFAUSDT", "side": "SELL",
                                "positionSide": "BOTH", "executedQty": "1",
                                "avgPrice": "110", "reduceOnly": True, "type": "MARKET",
                                "updateTime": ms(closed_at)})
    entry_order = {**order, "order_id": "100", "side": "BUY", "role": "entry",
                   "client_order_id": "cw-r05d", "reduce_only": False}
    acc = ea.merge_accounting(None, identity=identity, fills=[entrada, saida],
                              funding=[funding], orders=[entry_order, {**order, "role": "exit"}])
    acc = ea.finalize_accounting(acc, entry_order_ids=["100"], exit_order_ids=["200"],
                                 fills_window_complete=True, funding_window_complete=True,
                                 planned_stop=95.0)
    acc["orders"]["200"]["role"] = "exit"
    acc["exclusive_exposure"] = True
    acc["execution_proof"] = {"window_key": ms(closed_at), "complete": True}

    totals = acc.get("totals") or {}
    net_trade = float(totals.get("net_trade"))
    net_total = float(totals.get("net_including_funding"))
    check("ledger_r05c_produz_net_ex_funding", abs(net_trade - 9.92) < 1e-9, str(net_trade))
    check("ledger_r05c_produz_total_com_funding", abs(net_total + 240.08) < 1e-9, str(net_total))
    check("ledger_confirmado", acc.get("state") == "CONFIRMED" and
          acc.get("funding_state") == "CONFIRMED",
          f"{acc.get('state')}/{acc.get('funding_state')}")

    projected = ea.project_to_trade_fields(acc)
    async with db.get_session() as session:
        session.add(RealTrade(symbol="ALFA/USDT:USDT", side="long", qty=1.0,
                              entry_price=100.0, planned_stop=95.0, status="closed_tp2",
                              source="auto", opened_at=opened, closed_at=closed_at,
                              pnl_usd=projected.get("pnl_usd", net_trade),
                              execution_accounting=acc))
        await session.commit()

    # `pnl_usd` persistido continua EX-funding (o R05C não soma funding nele).
    async with db.get_session() as session:
        from sqlalchemy import select
        stored = (await session.execute(select(RealTrade.pnl_usd))).scalars().all()
    check("pnl_usd_permanece_ex_funding", abs(float(stored[0]) - net_trade) < 1e-6,
          str(stored))

    # ── Gate REAL, com equity e limite controlados (sem exchange) ───────────
    equity = {"quality": "OK", "total_usd": 1000.0, "reason_code": None}
    limite = {"quality": "OK", "value": 100.0, "reason_code": None}

    async def gate(side="long", entry=100.0, stop=95.0, qty=1.0):
        frs.reset_cache()
        checks: dict = {}
        with patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)), \
                patch.object(frs, "daily_loss_limit_usd", AsyncMock(return_value=limite)), \
                patch.object(fts, "current_account_scope", lambda: SCOPE):
            return await sts._r05b_entry_gate(side=side, final_entry=entry, stop=stop,
                                              final_qty=qty, checks=checks), checks

    os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"
    verdict, checks = await gate()
    check("legado_libera_com_pior_cenario_ex_funding", verdict is None, str(verdict))
    check("legado_reporta_pior_cenario_positivo",
          abs(float(checks["r05b_financial"]["worst_case_daily_usd"]) - 4.92) < 1e-6,
          str(checks.get("r05b_financial")))

    os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "accounting_total"
    verdict, checks = await gate()
    check("fonte_completa_bloqueia", isinstance(verdict, dict) and verdict.get("ok") is False,
          str(verdict))
    check("bloqueio_usa_o_total_com_funding",
          abs(float(checks["r05b_financial"]["worst_case_daily_usd"]) + 245.08) < 1e-6,
          str(checks.get("r05b_financial")))
    check("motivo_e_o_limite_diario",
          verdict.get("reason_code") == frs.BLOCK_REASON, str(verdict.get("reason_code")))

    # Total dentro do limite: a mesma fonte completa NÃO bloqueia.
    limite_alto = {"quality": "OK", "value": 1000.0, "reason_code": None}
    frs.reset_cache()
    with patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)), \
            patch.object(frs, "daily_loss_limit_usd", AsyncMock(return_value=limite_alto)), \
            patch.object(fts, "current_account_scope", lambda: SCOPE):
        dentro = await sts._r05b_entry_gate(side="long", final_entry=100.0, stop=95.0,
                                            final_qty=1.0, checks={})
    check("total_dentro_do_limite_libera", dentro is None, str(dentro))

    # Conta divergente: total não é adotado, e sem ele não há aumento de exposição.
    frs.reset_cache()
    with patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)), \
            patch.object(frs, "daily_loss_limit_usd", AsyncMock(return_value=limite)), \
            patch.object(fts, "current_account_scope", lambda: "o" * 64):
        outra_conta = await sts._r05b_entry_gate(side="long", final_entry=100.0, stop=95.0,
                                                 final_qty=1.0, checks={})
    check("conta_divergente_nao_libera",
          isinstance(outra_conta, dict) and outra_conta.get("ok") is False, str(outra_conta))

    # Ausência de leitura do ledger não vira zero conhecido.
    frs.reset_cache()
    with patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)), \
            patch.object(frs, "daily_loss_limit_usd", AsyncMock(return_value=limite)), \
            patch.object(fts, "current_account_scope", lambda: SCOPE), \
            patch.object(fts, "fresh_total", AsyncMock(side_effect=RuntimeError("banco fora"))):
        sem_leitura = await sts._r05b_entry_gate(side="long", final_entry=100.0, stop=95.0,
                                                 final_qty=1.0, checks={})
    check("leitura_indisponivel_bloqueia",
          isinstance(sem_leitura, dict) and sem_leitura.get("ok") is False, str(sem_leitura))

    # Janela comprovadamente VAZIA é zero conhecido: a primeira operação passa.
    async with db.get_session() as session:
        from sqlalchemy import delete
        await session.execute(delete(RealTrade))
        await session.commit()
    frs.reset_cache()
    with patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)), \
            patch.object(frs, "daily_loss_limit_usd", AsyncMock(return_value=limite)), \
            patch.object(fts, "current_account_scope", lambda: SCOPE):
        vazia = await sts._r05b_entry_gate(side="long", final_entry=100.0, stop=95.0,
                                           final_qty=1.0, checks={})
        snap = await frs.financial_snapshot(force=True)
    check("janela_vazia_comprovada_e_zero_conhecido", vazia is None, str(vazia))
    check("janela_vazia_tem_pnl_zero",
          snap["kill_daily"]["quality"] == "OK" and snap["kill_daily"]["pnl_usd"] == 0.0,
          str(snap.get("kill_daily")))
    check("rotulo_declara_funding_incluso",
          snap["kill_daily"]["pnl_label"] == frs.PNL_LABEL_TOTAL,
          str(snap["kill_daily"].get("pnl_label")))

    os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"
    frs.reset_cache()
    snap_legado = await frs.financial_snapshot(force=True)
    check("default_preserva_rotulo_ex_funding",
          snap_legado["kill_daily"]["pnl_label"] == frs.PNL_LABEL,
          str(snap_legado["kill_daily"].get("pnl_label")))

    await db._engine.dispose()
    print(f"R05D_GATE_PG_OK: {len(CHECKS)} verificações — ledger R05C real, gate real, "
          "sem exchange")


if __name__ == "__main__":
    asyncio.run(run())
