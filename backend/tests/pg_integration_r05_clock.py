"""R05 C — o corte temporal do snapshot é o da INSTRUÇÃO, não o da transação.

`R05_CLOCK_TEST_SOCKET` aponta para /tmp/cw-r05clk-sock.* criado pelo runner.
PostgreSQL 16 descartável UTF-8, driver real, socket Unix, DUAS conexões, com
espera REAL pela advisory lock confirmada em `pg_locks`. Sem TCP/DNS.

Defeito reproduzido: `now()` no PostgreSQL é o início da TRANSAÇÃO. A admissão
começa a transação, fica ESPERANDO a advisory lock e, quando enfim lê, enxerga
os commits novos — mas filtra o P&L do dia por um `until` antigo:

    realizado -92; posição aberta arrisca 6; base correta -98.
    O escritor segura a lock, fecha a posição com perda 6 usando o horário
    ATUAL (posterior ao início do leitor) e libera.
    O leitor via a posição fora das ABERTAS e a perda fora da JANELA:
    proposta 3 era aceita com -95; o correto é -101 e `DAILY_LOSS_LIMIT`.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("R05_CLOCK_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r05clk-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r05clk@/r05clkdb?host=" + test_socket
os.environ["R05_FINANCIAL_BREAKER_ENABLED"] = "true"
os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-relógio")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-relógio")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R05-relógio")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CONTA = "c" * 64
CHECKS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


async def run():
    from unittest.mock import AsyncMock, patch
    from sqlalchemy import select, text, update
    import db
    from models.entry_intent import EntryIntent
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from services import entry_intent_service as intents
    from services import financial_risk_service as frs
    from services import financial_total_service as fts

    for _ in range(2):
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[RecommendationSnapshot.__table__,
                                        RealTrade.__table__, EntryIntent.__table__])
    await db.init_db()

    LOCK = intents.RISK_LOCK_KEY

    async def zerar():
        async with db.get_session() as session:
            await session.execute(update(EntryIntent).values(state="TERMINAL"))
            await session.execute(RealTrade.__table__.delete())
            await session.commit()

    async def realizado(valor, *, minutos_atras=1):
        """Fechamento DENTRO da janela calendário vigente."""
        async with db.get_session() as session:
            agora = datetime.now(timezone.utc)
            quando = max(frs.kill_daily_start(agora) + timedelta(seconds=30),
                         agora - timedelta(minutes=minutos_atras))
            session.add(RealTrade(symbol="ALFA/USDT:USDT", exchange="binance",
                                  side="long", qty=1.0, entry_price=100.0,
                                  planned_stop=95.0, status="closed_stop",
                                  source="auto", pnl_usd=valor, entry_fee=0.0,
                                  exit_fee=0.0, opened_at=quando - timedelta(seconds=10),
                                  closed_at=quando))
            await session.commit()

    async def posicao_aberta(*, risco=6.0):
        async with db.get_session() as session:
            trade = RealTrade(symbol="BETA/USDT:USDT", exchange="binance", side="long",
                              qty=risco, entry_price=100.0, planned_stop=99.0,
                              sl_order_id="sl-1", sl_current_price=99.0,
                              status="open", source="auto", entry_fee=0.0,
                              opened_at=datetime.now(timezone.utc))
            session.add(trade)
            await session.commit()
            return trade.id

    def identidade(trigger: int):
        return intents.EntryIdentity(
            account_ref=CONTA, exchange="binance", symbol="GAMA-USDT-USDT",
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)

    equity = {"quality": "OK", "total_usd": 1000.0, "reason_code": None}

    def ambiente(limite_usd=100.0):
        frs.reset_cache()
        return (patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)),
                patch.object(frs, "daily_loss_limit_usd", AsyncMock(
                    return_value={"quality": "OK", "value": limite_usd,
                                  "reason_code": None})),
                patch.object(fts, "current_account_scope", lambda: CONTA))

    async def orcamento(limite_usd=100.0):
        a, b, c = ambiente(limite_usd)
        with a, b, c:
            return await frs.daily_budget()

    def budget_de(base):
        return intents.DailyBudget(base_usd=base.get("base_usd"),
                                   limit_usd=base.get("limit_usd"),
                                   complete=bool(base.get("complete")))

    async def esperando_a_lock() -> bool:
        """Espera REAL: alguém bloqueado no advisory lock, visto em `pg_locks`."""
        async with db.get_session() as session:
            total = int((await session.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND NOT granted AND objid = :k"), {"k": LOCK})).scalar() or 0)
        return total > 0

    async def com_lock_segura(fechar_id, *, comecou: asyncio.Event,
                              liberar: asyncio.Event, fechado: asyncio.Event):
        """ESCRITOR: segura a advisory lock, espera o leitor entrar na fila,
        fecha a posição com `closed_at` ATUAL e só então commita/libera."""
        async with db.get_session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
            comecou.set()
            await liberar.wait()                    # leitor já está na fila
            await session.execute(
                update(RealTrade).where(RealTrade.id == fechar_id)
                .values(status="closed_stop", pnl_usd=-6.0, exit_fee=0.0,
                        closed_at=text("statement_timestamp()")))
            await session.commit()                  # libera a lock (xact)
        fechado.set()

    async def leitor_espera_e_admite(fabrica_admissao, *, comecou: asyncio.Event,
                                     liberar: asyncio.Event):
        await comecou.wait()
        tarefa = asyncio.create_task(fabrica_admissao())
        # Confirma a espera REAL na fila da advisory lock antes de liberar.
        na_fila = False
        for _ in range(200):
            await asyncio.sleep(0.02)
            if await esperando_a_lock():
                na_fila = True
                break
        liberar.set()
        return na_fila, await tarefa

    # ══════════════════════════════════════════════════════════════════════
    #  1. RESERVA: fechamento durante a espera pela lock
    # ══════════════════════════════════════════════════════════════════════
    await zerar()
    await realizado(-92.0)
    aberta = await posicao_aberta(risco=6.0)
    base = await orcamento()
    check("base_coerente_antes_da_espera",
          base["complete"] and abs(base["base_usd"] + 98.0) < 1e-9, str(base))

    comecou, liberar, fechado = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def admitir():
        ident = identidade(1_760_000_100_000)
        return await intents.reserve(
            db.get_session, ident, {"entry": 100.0, "stop_loss": 99.0},
            owner="w-espera",
            capacity=intents.Capacity(risk_usd=3.0, max_open_positions=5,
                                      max_open_risk_usd=50.0),
            budget=budget_de(base),
            decision={"entry": 100.0, "stop_loss": 99.0, "qty": 3.0})

    escritor = asyncio.create_task(
        com_lock_segura(aberta, comecou=comecou, liberar=liberar, fechado=fechado))
    na_fila, veredito = await leitor_espera_e_admite(
        admitir, comecou=comecou, liberar=liberar)
    await escritor
    check("houve_espera_real_pela_lock", na_fila, "pg_locks não mostrou espera")
    async with db.get_session() as session:
        status_final = (await session.execute(
            select(RealTrade.status, RealTrade.closed_at)
            .where(RealTrade.id == aberta))).one()
    check("escritor_fechou_com_horario_atual",
          status_final[0] == "closed_stop" and status_final[1] is not None,
          str(status_final))
    check("reserva_bloqueia_apos_a_espera",
          veredito.decision == intents.BLOCKED_CAPACITY
          and veredito.reason == "DAILY_LOSS_LIMIT", str(veredito))
    async with db.get_session() as session:
        base_depois = await frs.daily_base_in_session(session)
    check("base_posterior_confirma_menos_98",
          abs(base_depois["value"] + 98.0) < 1e-9, str(base_depois))

    # ══════════════════════════════════════════════════════════════════════
    #  2. READMISSÃO FINAL: mesma espera, mesmo bloqueio
    # ══════════════════════════════════════════════════════════════════════
    await zerar()
    await realizado(-92.0)
    aberta2 = await posicao_aberta(risco=6.0)
    base = await orcamento()
    minha = identidade(1_760_000_200_000)
    concedida = await intents.reserve(
        db.get_session, minha, {"entry": 100.0, "stop_loss": 99.0}, owner="w-final",
        capacity=intents.Capacity(risk_usd=1.0, max_open_positions=5,
                                  max_open_risk_usd=50.0),
        budget=budget_de(base), decision={"entry": 100.0, "stop_loss": 99.0, "qty": 1.0})
    check("reserva_pequena_cabe", concedida.granted, str(concedida))

    comecou2, liberar2, fechado2 = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def readmitir():
        return await intents.admit_final_risk(
            db.get_session, minha.intent_key, owner="w-final", risk_usd=3.0,
            capacity=intents.Capacity(risk_usd=3.0, max_open_positions=5,
                                      max_open_risk_usd=50.0),
            budget=budget_de(base))

    escritor2 = asyncio.create_task(
        com_lock_segura(aberta2, comecou=comecou2, liberar=liberar2, fechado=fechado2))
    na_fila2, readmissao = await leitor_espera_e_admite(
        readmitir, comecou=comecou2, liberar=liberar2)
    await escritor2
    check("readmissao_tambem_esperou_a_lock", na_fila2, "pg_locks não mostrou espera")
    check("readmissao_bloqueia_apos_a_espera",
          readmissao.decision == intents.BLOCKED_CAPACITY
          and readmissao.reason == "DAILY_LOSS_LIMIT", str(readmissao))

    # ══════════════════════════════════════════════════════════════════════
    #  3. Sem corrida, o mesmo caminho ADMITE o que cabe
    # ══════════════════════════════════════════════════════════════════════
    await zerar()
    await realizado(-10.0)
    base = await orcamento()
    livre = await intents.reserve(
        db.get_session, identidade(1_760_000_300_000),
        {"entry": 100.0, "stop_loss": 99.0}, owner="w-livre",
        capacity=intents.Capacity(risk_usd=3.0, max_open_positions=5,
                                  max_open_risk_usd=50.0),
        budget=budget_de(base), decision={"entry": 100.0, "stop_loss": 99.0, "qty": 3.0})
    check("sem_corrida_admite_o_que_cabe", livre.granted, str(livre))

    # ══════════════════════════════════════════════════════════════════════
    #  4. O corte é o da INSTRUÇÃO: linha fechada AGORA entra na janela
    # ══════════════════════════════════════════════════════════════════════
    await zerar()
    await realizado(-50.0)
    async with db.get_session() as leitura:
        # Transação do leitor começa aqui; a perda é persistida DEPOIS.
        await leitura.execute(text("SELECT 1"))
        async with db.get_session() as escrita:
            agora_sql = text("statement_timestamp()")
            escrita.add(RealTrade(symbol="ZETA/USDT:USDT", exchange="binance",
                                  side="long", qty=1.0, entry_price=100.0,
                                  planned_stop=95.0, status="closed_stop",
                                  source="auto", pnl_usd=-30.0, entry_fee=0.0,
                                  exit_fee=0.0,
                                  opened_at=datetime.now(timezone.utc),
                                  closed_at=datetime.now(timezone.utc)))
            await escrita.commit()
        base_tardia = await frs.daily_base_in_session(leitura)
    check("linha_fechada_depois_do_begin_entra_na_janela",
          abs(base_tardia["value"] + 80.0) < 1e-9, str(base_tardia))

    await db._engine.dispose()
    print(f"R05_CLOCK_PG_OK: {len(CHECKS)} verificações — espera real pela lock "
          "(pg_locks), corte da instrução, reserva e readmissão")


if __name__ == "__main__":
    asyncio.run(run())
