"""P03 — exclusividade transacional da entrada econômica em PostgreSQL real.

P03_INTENT_TEST_SOCKET deve apontar para /tmp/cw-p03-sock.* criado pelo teste.
Schema criado só por este harness (migração aditiva rodada 2×). Sem TCP/DNS,
sem DATABASE_URL externo, sem exchange: o "envio" é um adaptador falso que
apenas CONTA chamadas.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("P03_INTENT_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-p03-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://p03@/p03db?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste P03")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

TRIGGER = 1_760_000_100_000
SENDS: list = []          # adaptador de envio FALSO: só conta chamadas


async def fake_dispatch(client_order_id: str, outcome: dict) -> dict:
    SENDS.append(client_order_id)
    return outcome


async def run():
    from sqlalchemy import func, select, update
    import db
    from models.entry_intent import EntryIntent
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK do RealTrade
    from services import entry_intent_service as intents

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

    async def state_of(key):
        async with db.get_session() as session:
            return (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == key))).scalar_one_or_none()

    async def count_intents():
        async with db.get_session() as session:
            return int((await session.execute(select(func.count(EntryIntent.intent_key)))).scalar() or 0)

    async def worker(owner, ident, body, outcome, capacity=None):
        """Mesmo encadeamento do executor: reserva → guard → envio → desfecho."""
        reservation = await intents.reserve(db.get_session, ident, body, owner=owner, capacity=capacity)
        if not reservation.granted:
            return reservation.decision, None
        if not await intents.mark_sending(db.get_session, reservation.intent_key, owner=owner):
            return "MARK_SENDING_REFUSED", None
        if not await intents.may_dispatch(db.get_session, reservation.intent_key, owner=owner):
            return "GUARD_REFUSED", None
        result = await fake_dispatch(reservation.client_order_id, outcome)
        # O executor real se IDENTIFICA ao encerrar: sem dono, um callback
        # anônimo poderia fechar a tentativa viva de outro processo.
        if result.get("ok"):
            await intents.mark_confirmed(db.get_session, reservation.intent_key,
                                         owner=owner, real_trade_id=result.get("trade_id"))
        elif result.get("no_fill"):
            await intents.mark_terminal(db.get_session, reservation.intent_key,
                                        owner=owner, reason="NO_FILL")
        else:
            await intents.mark_unknown(db.get_session, reservation.intent_key,
                                       owner=owner, reason="DISPATCH_OUTCOME_UNKNOWN")
        return reservation.decision, reservation.client_order_id

    # Migração aditiva idempotente (2×), criada apenas pelo harness.
    tables = [RecommendationSnapshot.__table__, RealTrade.__table__, EntryIntent.__table__]
    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tables)

    # 1. Mesma decisão, snapshots distintos: uma intenção, um client id, um envio.
    first = await worker("w1", identity(), payload(), {"ok": True, "trade_id": 1})
    second = await worker("w2", identity(), payload(), {"ok": True, "trade_id": 2})
    assert first[0] == intents.RESERVED_NEW, first
    assert second[0] == intents.BLOCKED_CONFIRMED, second
    assert len(SENDS) == 1, SENDS
    assert await count_intents() == 1
    row = await state_of(identity().intent_key)
    assert row.state == "CONFIRMED" and row.real_trade_id == 1, (row.state, row.real_trade_id)

    # 2. Duas conexões CONCORRENTES na mesma decisão nova: um único envio.
    SENDS.clear()
    concurrent_identity = identity(trigger_candle_ms=TRIGGER + 14_400_000)
    outcomes = await asyncio.gather(
        worker("wA", concurrent_identity, payload(), {"ok": True, "trade_id": 10}),
        worker("wB", concurrent_identity, payload(), {"ok": True, "trade_id": 11}),
    )
    decisions = sorted(decision for decision, _ in outcomes)
    assert len(SENDS) == 1, (SENDS, decisions)
    assert intents.RESERVED_NEW in decisions, decisions
    assert decisions[0] in (intents.BLOCKED_CONFIRMED, intents.BLOCKED_IN_FLIGHT), decisions

    # 3. Payload materialmente divergente é CONFLITO, não nova entrada.
    conflict_identity = identity(trigger_candle_ms=TRIGGER + 28_800_000)
    await intents.reserve(db.get_session, conflict_identity, payload(), owner="w1")
    conflict = await intents.reserve(db.get_session, conflict_identity, payload(stop_loss=90.0), owner="w1")
    assert conflict.decision == intents.CONFLICT_PAYLOAD, conflict
    assert (await state_of(conflict_identity.intent_key)).state == "RESERVED"

    # 4. Crash ANTES do envio: reserva permanece, lease liberado, um envio só.
    SENDS.clear()
    crash_before = identity(trigger_candle_ms=TRIGGER + 43_200_000)
    reservation = await intents.reserve(db.get_session, crash_before, payload(), owner="dead")
    assert reservation.decision == intents.RESERVED_NEW
    async with db.get_session() as session:      # simula processo morto: lease vencido
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == crash_before.intent_key)
                              .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)))
        await session.commit()
    recovered = await intents.recover_stale(db.get_session)
    assert recovered["reserved_released"] == 1 and recovered["to_unknown"] == 0, recovered
    resumed = await worker("alive", crash_before, payload(), {"ok": True, "trade_id": 20})
    assert resumed[0] == intents.RESERVED_RESUMED, resumed
    assert len(SENDS) == 1, SENDS

    # 5. Crash DEPOIS do envio: vira UNKNOWN e NUNCA reenvia.
    SENDS.clear()
    crash_after = identity(trigger_candle_ms=TRIGGER + 57_600_000)
    reservation = await intents.reserve(db.get_session, crash_after, payload(), owner="dead2")
    await intents.mark_sending(db.get_session, reservation.intent_key, owner="dead2")
    await fake_dispatch(reservation.client_order_id, {"ok": True})     # enviou e morreu
    async with db.get_session() as session:
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == crash_after.intent_key)
                              .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)))
        await session.commit()
    recovered = await intents.recover_stale(db.get_session)
    assert recovered["to_unknown"] == 1, recovered
    assert (await state_of(crash_after.intent_key)).state == "UNKNOWN"
    retry = await worker("alive2", crash_after, payload(), {"ok": True, "trade_id": 30})
    assert retry[0] == intents.BLOCKED_UNKNOWN, retry
    assert len(SENDS) == 1, SENDS       # nenhum segundo envio

    # 6. Lease vencido em SENDING detectado na própria reserva (sem recover).
    SENDS.clear()
    stale_lease = identity(trigger_candle_ms=TRIGGER + 72_000_000)
    reservation = await intents.reserve(db.get_session, stale_lease, payload(), owner="dead3")
    await intents.mark_sending(db.get_session, reservation.intent_key, owner="dead3")
    async with db.get_session() as session:
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == stale_lease.intent_key)
                              .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()
    blocked = await worker("alive3", stale_lease, payload(), {"ok": True})
    assert blocked[0] == intents.BLOCKED_UNKNOWN, blocked
    assert SENDS == [], SENDS

    # 7. Callback tardio do reconciliador: vincula o RealTrade e trava o resto.
    assert await intents.mark_confirmed(db.get_session, crash_after.intent_key, real_trade_id=99)
    row = await state_of(crash_after.intent_key)
    assert row.state == "CONFIRMED" and row.real_trade_id == 99
    late = await intents.reserve(db.get_session, crash_after, payload(), owner="alive4")
    assert late.decision == intents.BLOCKED_CONFIRMED, late
    # confirmação é idempotente: não sobrescreve o vínculo existente
    assert not await intents.mark_confirmed(db.get_session, crash_after.intent_key, real_trade_id=100)
    assert (await state_of(crash_after.intent_key)).real_trade_id == 99

    # 8. Parcial/no-fill: terminal explícito, e um NOVO gatilho é aceito.
    SENDS.clear()
    no_fill = identity(trigger_candle_ms=TRIGGER + 86_400_000)
    filled = await worker("w5", no_fill, payload(), {"no_fill": True})
    assert filled[0] == intents.RESERVED_NEW and len(SENDS) == 1
    again = await intents.reserve(db.get_session, no_fill, payload(), owner="w5")
    assert again.decision == intents.BLOCKED_TERMINAL, again
    next_candle = await intents.reserve(db.get_session,
                                        identity(trigger_candle_ms=TRIGGER + 100_800_000),
                                        payload(), owner="w5")
    assert next_candle.decision == intents.RESERVED_NEW, next_candle   # símbolo não fica travado

    # 9. Restart do processo (outro owner) não apaga nem reenvia nada.
    before_restart = await count_intents()
    recovered = await intents.recover_stale(db.get_session)
    assert await count_intents() == before_restart, "intenções pendentes não podem sumir"

    # 10. Capacidade: duas DECISÕES diferentes disputando o último risco livre.
    SENDS.clear()
    capacity = intents.Capacity(risk_usd=10.0, max_open_risk_usd=15.0, open_risk_usd=0.0)
    left = identity(trigger_candle_ms=TRIGGER + 115_200_000, symbol="AAA-USDT-USDT")
    right = identity(trigger_candle_ms=TRIGGER + 115_200_000, symbol="BBB-USDT-USDT")
    race = await asyncio.gather(
        worker("c1", left, payload(), {"ok": True, "trade_id": 40}, capacity=capacity),
        worker("c2", right, payload(), {"ok": True, "trade_id": 41}, capacity=capacity),
    )
    granted = [decision for decision, _ in race if decision == intents.RESERVED_NEW]
    denied = [decision for decision, _ in race if decision == intents.BLOCKED_CAPACITY]
    assert len(granted) == 1 and len(denied) == 1, race
    assert len(SENDS) == 1, SENDS

    # 11. Slots: posição aberta real conta DENTRO da transação de admissão.
    async with db.get_session() as session:
        # Com stop planejado: é uma posição NORMAL (o caso sem stop vira o
        # cenário 14, de risco desconhecido).
        session.add(RealTrade(symbol="ZZZ/USDT:USDT", side="long", qty=1.0, entry_price=10.0,
                              planned_stop=9.0, status="open", source="auto",
                              opened_at=datetime.now(timezone.utc)))
        await session.commit()
    slot = await intents.reserve(db.get_session, identity(trigger_candle_ms=TRIGGER + 129_600_000),
                                 payload(), owner="c3",
                                 capacity=intents.Capacity(max_open_positions=1))
    assert slot.decision == intents.BLOCKED_CAPACITY and slot.reason == "MAX_OPEN_POSITIONS", slot


    # 13. Risco aberto é lido SOB a lock: snapshot velho do chamador não vale.
    async with db.get_session() as session:
        session.add(RealTrade(symbol="RSK/USDT:USDT", side="long", qty=10.0, entry_price=100.0,
                              planned_stop=95.0, status="open", source="auto",
                              opened_at=datetime.now(timezone.utc)))
        await session.commit()
    stale = await intents.reserve(
        db.get_session, identity(trigger_candle_ms=TRIGGER + 144_000_000), payload(), owner="c4",
        capacity=intents.Capacity(risk_usd=1.0, max_open_risk_usd=40.0, open_risk_usd=0.0))
    assert stale.decision == intents.BLOCKED_CAPACITY and stale.reason == "MAX_OPEN_RISK", stale

    # 13b. Dentro do teto, a mesma leitura sob a lock autoriza.
    ok_risk = await intents.reserve(
        db.get_session, identity(trigger_candle_ms=TRIGGER + 147_600_000), payload(), owner="c4",
        capacity=intents.Capacity(risk_usd=1.0, max_open_risk_usd=500.0, open_risk_usd=0.0))
    assert ok_risk.decision == intents.RESERVED_NEW, ok_risk

    # 14. Risco aberto INCOMPLETO não vira zero: admissão falha fechada.
    async with db.get_session() as session:
        session.add(RealTrade(symbol="UNK/USDT:USDT", side="long", qty=5.0, entry_price=50.0,
                              planned_stop=None, sl_current_price=None, status="open",
                              source="auto", opened_at=datetime.now(timezone.utc)))
        await session.commit()
    unknown_risk = await intents.reserve(
        db.get_session, identity(trigger_candle_ms=TRIGGER + 151_200_000), payload(), owner="c5",
        capacity=intents.Capacity(risk_usd=1.0, max_open_risk_usd=10_000.0, open_risk_usd=0.0))
    assert unknown_risk.decision == intents.BLOCKED_CAPACITY, unknown_risk
    assert unknown_risk.reason == "OPEN_RISK_UNKNOWN", unknown_risk
    async with db.get_session() as session:   # limpa a linha incompleta do ensaio
        await session.execute(update(RealTrade).where(RealTrade.symbol == "UNK/USDT:USDT")
                              .values(status="closed"))
        await session.commit()

    # 15. Callback ATRASADO não rebaixa uma confirmação.
    late = identity(trigger_candle_ms=TRIGGER + 154_800_000)
    await intents.reserve(db.get_session, late, payload(), owner="w-late")
    await intents.mark_sending(db.get_session, late.intent_key, owner="w-late")
    await intents.mark_confirmed(db.get_session, late.intent_key, real_trade_id=99)
    assert await intents.mark_unknown(db.get_session, late.intent_key,
                                      owner="w-late", reason="DISPATCH_OUTCOME_UNKNOWN") is False
    assert await intents.mark_terminal(db.get_session, late.intent_key,
                                       owner="outro", reason="NO_FILL") is False
    row = await state_of(late.intent_key)
    assert row.state == "CONFIRMED" and row.real_trade_id == 99, (row.state, row.real_trade_id)

    # 16. Tentativa VIVA de outro dono não é encerrada por callback alheio.
    busy = identity(trigger_candle_ms=TRIGGER + 158_400_000)
    await intents.reserve(db.get_session, busy, payload(), owner="dono")
    await intents.mark_sending(db.get_session, busy.intent_key, owner="dono")
    assert await intents.mark_terminal(db.get_session, busy.intent_key,
                                       owner="estranho", reason="NO_FILL") is False
    assert (await state_of(busy.intent_key)).state == "SENDING"
    assert await intents.mark_terminal(db.get_session, busy.intent_key,
                                       owner="dono", reason="NO_FILL") is True
    assert (await state_of(busy.intent_key)).state == "TERMINAL"


    # 17. IDs efetivos: registrados ANTES do POST, sob o mesmo fencing.
    ids = identity(trigger_candle_ms=TRIGGER + 162_000_000)
    await intents.reserve(db.get_session, ids, payload(), owner="w-ids")
    # Em RESERVED ainda não há despacho: registrar é recusado.
    assert await intents.register_dispatch(db.get_session, ids.intent_key,
                                           owner="w-ids", dispatch_id="cw-x") is False
    await intents.mark_sending(db.get_session, ids.intent_key, owner="w-ids")
    assert await intents.register_dispatch(db.get_session, ids.intent_key,
                                           owner="w-ids", dispatch_id=ids.client_order_id) is True
    # Mesmo id de novo (retry do MESMO envio) não duplica.
    assert await intents.register_dispatch(db.get_session, ids.intent_key,
                                           owner="w-ids", dispatch_id=ids.client_order_id) is True
    fallback_id = f"{ids.client_order_id[:32]}-mfb"[:36]
    assert await intents.register_dispatch(db.get_session, ids.intent_key,
                                           owner="w-ids", dispatch_id=fallback_id) is True
    # Outro dono não registra despacho na tentativa alheia.
    assert await intents.register_dispatch(db.get_session, ids.intent_key,
                                           owner="estranho", dispatch_id="cw-zzz") is False
    registrados = await intents.dispatch_ids(db.get_session, ids.intent_key)
    assert registrados == [ids.client_order_id, fallback_id], registrados

    # 18. Intenção incerta entra na lista de reconciliação; maker pendente não.
    await intents.mark_unknown(db.get_session, ids.intent_key, owner="w-ids",
                               reason="DISPATCH_OUTCOME_UNKNOWN")
    pendente = identity(trigger_candle_ms=TRIGGER + 165_600_000)
    await intents.reserve(db.get_session, pendente, payload(), owner="w-pend")
    await intents.mark_sending(db.get_session, pendente.intent_key, owner="w-pend")
    await intents.mark_unknown(db.get_session, pendente.intent_key, owner="w-pend",
                               reason="PENDING_ENTRY_ORDER")
    fila = await intents.list_needing_reconciliation(db.get_session)
    chaves = {linha["intent_key"] for linha in fila}
    assert ids.intent_key in chaves, chaves
    assert pendente.intent_key not in chaves, "ordem maker viva não é desfecho incerto"


    # 19. Transferência reserva → RealTrade sem janela em que NINGUÉM conta.
    async def usage_now(ident):
        # Mesma garantia de antes, agora pela leitura CONSISTENTE da admissão
        # (uma instrução): reservas e exposição vêm do mesmo snapshot.
        async with db.get_session() as session:
            visao = await intents._admission_view(session, ident, "")
            return ((visao["pending_count"], visao["pending_risk"]),
                    visao["open_risk_usd"], visao["open_risk_complete"])

    transfer = identity(trigger_candle_ms=TRIGGER + 169_200_000)
    await intents.reserve(db.get_session, transfer, payload(), owner="w-tr",
                          capacity=intents.Capacity(risk_usd=25.0, max_open_risk_usd=10_000.0))
    (pend_count, pend_risk), open_risk_before, _c = await usage_now(transfer)
    assert pend_count >= 1 and pend_risk >= 25.0, (pend_count, pend_risk)
    await intents.mark_sending(db.get_session, transfer.intent_key, owner="w-tr")
    async with db.get_session() as session:      # o RealTrade nasce ANTES do confirm
        trade = RealTrade(symbol="TRF/USDT:USDT", side="long", qty=5.0, entry_price=100.0,
                          planned_stop=95.0, status="open", source="auto",
                          opened_at=datetime.now(timezone.utc))
        session.add(trade)
        await session.commit()
        trade_id = trade.id
    (mid_count, mid_risk), open_risk_mid, _c = await usage_now(transfer)
    # Janela conservadora: os dois contam; nunca existe instante com zero.
    assert mid_count >= 1 and mid_risk >= 25.0, (mid_count, mid_risk)
    assert open_risk_mid >= open_risk_before + 25.0, (open_risk_before, open_risk_mid)
    assert await intents.mark_confirmed(db.get_session, transfer.intent_key,
                                        owner="w-tr", real_trade_id=trade_id) is True
    (after_count, after_risk), open_risk_after, _c = await usage_now(transfer)
    assert after_count == mid_count - 1, (mid_count, after_count)
    assert open_risk_after >= open_risk_before + 25.0, (open_risk_before, open_risk_after)

    # 12. Nenhuma intenção apagada em todo o ensaio.
    async with db.get_session() as session:
        pending = int((await session.execute(select(func.count(EntryIntent.intent_key))
                                             .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN"))))).scalar() or 0)
    assert pending >= 1, "reservas pendentes preservadas"
    await db._engine.dispose()
    print("P03_INTENT_PG_OK: schema2x, mesma-decisao-um-envio, concorrencia, conflito, "
          "crash-antes, crash-depois, lease-vencido, callback-tardio, no-fill-terminal, "
          "novo-gatilho, restart-preserva, corrida-de-capacidade, slots, risco-sob-lock, "
          "risco-incompleto-fail-closed, callback-nao-rebaixa, tentativa-alheia, "
          "ids-efetivos, fila-de-reconciliacao, transferencia-sem-janela, sem-exchange")


if __name__ == "__main__":
    asyncio.run(run())
