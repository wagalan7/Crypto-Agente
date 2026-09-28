"""R05 C — duas consultas na mesma transação não formam um snapshot consistente.

`R05_SNAP_TEST_SOCKET` aponta para /tmp/cw-r05snp-sock.* criado pelo runner.
PostgreSQL 16 descartável UTF-8, driver real, socket Unix, DUAS conexões e
barreira determinística ENTRE as leituras da admissão. Sem TCP/DNS, sem exchange.

Defeito reproduzido (fonte legacy):

    realizado -92; posição aberta arrisca 6; base coerente -98.
    A admissão pega a lock e lê FECHADAS (-92); a segunda conexão fecha a
    posição com perda 6 e commita; a admissão lê ABERTAS: vazias.
    A posição sumiu das DUAS parcelas e `_readmit` admitia proposta 3 com -95;
    o correto é -101 (limite 100) e `DAILY_LOSS_LIMIT`.

A barreira é injetada no ponto REAL de leitura (o executor da sessão da
admissão), não substituindo o cálculo, a lock ou a readmissão.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("R05_SNAP_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r05snp-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r05snp@/r05snpdb?host=" + test_socket
os.environ["R05_FINANCIAL_BREAKER_ENABLED"] = "true"
os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-snapshot")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-snapshot")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R05-snapshot")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CONTA = "c" * 64
#: Trecho que aparece SÓ na leitura das linhas FECHADAS do dia — é entre ela e
#: a leitura das ABERTAS que a corrida da auditoria acontece.
GATILHO_FECHADAS = "closed_at >="
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

    agora = datetime.now(timezone.utc)
    fechado = max(frs.kill_daily_start(agora) + timedelta(minutes=1),
                  agora - timedelta(hours=2))

    async def zerar():
        async with db.get_session() as session:
            await session.execute(update(EntryIntent).values(state="TERMINAL"))
            await session.execute(RealTrade.__table__.delete())
            await session.commit()

    async def realizado(valor):
        async with db.get_session() as session:
            session.add(RealTrade(symbol="ALFA/USDT:USDT", exchange="binance",
                                  side="long", qty=1.0, entry_price=100.0,
                                  planned_stop=95.0, status="closed_stop",
                                  source="auto", pnl_usd=valor, entry_fee=0.0,
                                  exit_fee=0.0, opened_at=fechado - timedelta(minutes=30),
                                  closed_at=fechado))
            await session.commit()

    async def posicao_aberta(*, risco=6.0):
        """Posição com risco `risco` (entry-stop = 1 × qty) e stop legível."""
        async with db.get_session() as session:
            trade = RealTrade(symbol="BETA/USDT:USDT", exchange="binance", side="long",
                              qty=risco, entry_price=100.0, planned_stop=99.0,
                              sl_order_id="sl-1", sl_current_price=99.0,
                              status="open", source="auto", entry_fee=0.0,
                              opened_at=agora)
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

    def budget_de(base, limite=100.0):
        return intents.DailyBudget(base_usd=base.get("base_usd"),
                                   limit_usd=base.get("limit_usd", limite),
                                   complete=bool(base.get("complete")))

    async def reservar(trigger, risco, *, budget, owner="w-novo"):
        ident = identidade(trigger)
        capacidade = intents.Capacity(risk_usd=risco, max_open_positions=5,
                                      max_open_risk_usd=50.0)
        return ident, await intents.reserve(
            db.get_session, ident, {"entry": 100.0, "stop_loss": 99.0},
            owner=owner, capacity=capacidade, budget=budget,
            decision={"entry": 100.0, "stop_loss": 99.0, "qty": risco})

    # ── Barreira determinística DENTRO da leitura da admissão ─────────────
    class BarreiraDeLeitura:
        """Suspende a admissão no meio da leitura e deixa o ESCRITOR commitar.

        Envolve `AsyncSession.execute` da sessão da admissão: quando a primeira
        instrução de leitura das parcelas do dia passa, libera o escritor e
        espera o commit dele antes de seguir. Nada do cálculo é substituído.
        """

        def __init__(self, gatilho: str):
            self.gatilho = gatilho
            self.chegou = asyncio.Event()
            self.liberar = asyncio.Event()
            self.disparou = False

        def instalar(self):
            from sqlalchemy.ext.asyncio import AsyncSession
            original = AsyncSession.execute
            barreira = self

            async def execute(self, statement, *args, **kwargs):
                texto = str(statement)
                if not barreira.disparou and barreira.gatilho in texto:
                    barreira.disparou = True
                    resultado = await original(self, statement, *args, **kwargs)
                    barreira.chegou.set()
                    await barreira.liberar.wait()
                    return resultado
                return await original(self, statement, *args, **kwargs)

            return patch.object(AsyncSession, "execute", execute)

    async def corrida(*, trigger, risco_proposto, base, fechar_id):
        """Escritor REAL fecha a posição entre as leituras da admissão."""
        barreira = BarreiraDeLeitura(GATILHO_FECHADAS)

        async def escritor():
            await barreira.chegou.wait()
            async with db.get_session() as session:   # SEGUNDA conexão
                await session.execute(
                    update(RealTrade).where(RealTrade.id == fechar_id)
                    .values(status="closed_stop", closed_at=fechado,
                            pnl_usd=-6.0, exit_fee=0.0))
                await session.commit()
            barreira.liberar.set()

        async def admissao():
            with barreira.instalar():
                return await reservar(trigger, risco_proposto, budget=budget_de(base))

        tarefa = asyncio.create_task(escritor())
        try:
            resultado = await admissao()
        finally:
            barreira.liberar.set()
            await tarefa
        return resultado

    # ── 1. REPRO: fechamento entre as leituras da admissão ────────────────
    await zerar()
    await realizado(-92.0)
    aberta = await posicao_aberta(risco=6.0)
    base = await orcamento()
    check("base_coerente_antes_da_corrida",
          base["complete"] and abs(base["base_usd"] + 98.0) < 1e-9, str(base))
    _, veredito = await corrida(trigger=1_760_000_100_000, risco_proposto=3.0,
                                base=base, fechar_id=aberta)
    check("fechamento_entre_leituras_nao_some_das_duas_parcelas",
          veredito.decision == intents.BLOCKED_CAPACITY
          and veredito.reason == "DAILY_LOSS_LIMIT", str(veredito))
    async with db.get_session() as session:
        base_depois = await frs.daily_base_in_session(session)
    check("base_consistente_depois_da_corrida",
          abs(base_depois["value"] + 98.0) < 1e-9, str(base_depois))

    # ── 2. Mesma corrida na READMISSÃO final ──────────────────────────────
    await zerar()
    await realizado(-92.0)
    aberta2 = await posicao_aberta(risco=6.0)
    base = await orcamento()
    minha, concedida = await reservar(1_760_000_200_000, 1.0, budget=budget_de(base),
                                      owner="w-final")
    check("reserva_pequena_cabe", concedida.granted, str(concedida))
    barreira2 = BarreiraDeLeitura(GATILHO_FECHADAS)

    async def escritor2():
        await barreira2.chegou.wait()
        async with db.get_session() as session:
            await session.execute(
                update(RealTrade).where(RealTrade.id == aberta2)
                .values(status="closed_stop", closed_at=fechado, pnl_usd=-6.0,
                        exit_fee=0.0))
            await session.commit()
        barreira2.liberar.set()

    tarefa2 = asyncio.create_task(escritor2())
    try:
        with barreira2.instalar():
            readmissao = await intents.admit_final_risk(
                db.get_session, minha.intent_key, owner="w-final", risk_usd=3.0,
                capacity=intents.Capacity(risk_usd=3.0, max_open_positions=5,
                                          max_open_risk_usd=50.0),
                budget=budget_de(base))
    finally:
        barreira2.liberar.set()
        await tarefa2
    check("readmissao_tambem_ve_visao_consistente",
          readmissao.decision == intents.BLOCKED_CAPACITY
          and readmissao.reason == "DAILY_LOSS_LIMIT", str(readmissao))

    # ── 3. Sem corrida: o mesmo caminho ADMITE o que cabe ─────────────────
    await zerar()
    await realizado(-10.0)
    base = await orcamento()
    _, livre = await reservar(1_760_000_300_000, 3.0, budget=budget_de(base))
    check("sem_corrida_admite_o_que_cabe", livre.granted, str(livre))

    # ── 4. Reserva alheia pendente conta; depois do vínculo, vira exposição ─
    await zerar()
    await realizado(-90.0)
    base = await orcamento()
    alheia, ok_alheia = await reservar(1_760_000_400_000, 6.0, budget=budget_de(base),
                                       owner="w-alheia")
    check("reserva_alheia_concedida", ok_alheia.granted, str(ok_alheia))
    _, bloqueada = await reservar(1_760_000_410_000, 5.0, budget=budget_de(base))
    check("reserva_pendente_ja_conta",
          bloqueada.reason == "DAILY_LOSS_LIMIT", str(bloqueada))
    trade_vinculado = await posicao_aberta(risco=6.0)
    async with db.get_session() as session:
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == alheia.intent_key)
                              .values(real_trade_id=trade_vinculado))
        await session.commit()
    _, ainda_bloqueada = await reservar(1_760_000_420_000, 5.0, budget=budget_de(base))
    check("apos_vinculo_vira_exposicao_sem_lacuna",
          ainda_bloqueada.reason == "DAILY_LOSS_LIMIT", str(ainda_bloqueada))

    # ── 5. Igualdade com o limite bloqueia ────────────────────────────────
    await zerar()
    await realizado(-97.0)
    base = await orcamento()
    _, igual = await reservar(1_760_000_500_000, 3.0, budget=budget_de(base))
    check("igualdade_com_o_limite_bloqueia",
          igual.reason == "DAILY_LOSS_LIMIT", str(igual))

    # ── 6. Leitura incerta bloqueia aumento ───────────────────────────────
    await zerar()
    await realizado(-10.0)
    async with db.get_session() as session:      # posição aberta sem stop legível
        session.add(RealTrade(symbol="DELTA/USDT:USDT", exchange="binance", side="long",
                              qty=1.0, entry_price=100.0, planned_stop=None,
                              status="open", source="auto", opened_at=agora))
        await session.commit()
    base = await orcamento()
    _, incerta = await reservar(1_760_000_600_000, 1.0, budget=budget_de(base))
    check("leitura_incerta_bloqueia_aumento",
          incerta.decision == intents.BLOCKED_CAPACITY
          and incerta.reason in ("OPEN_RISK_UNKNOWN", "DAILY_BASE_UNAVAILABLE"),
          str(incerta))

    # ── 7. Fonte com funding é OUTRO contrato: legacy não prova funding ────
    await zerar()
    await realizado(-92.0)
    os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "accounting_total"
    try:
        async with db.get_session() as session:
            base_total = await frs.daily_base_in_session(session)
        check("fonte_com_funding_sem_ledger_nao_libera",
              base_total["quality"] != "OK", str(base_total)[:200])
        base_cfg = await orcamento()
        _, com_funding = await reservar(1_760_000_700_000, 1.0,
                                        budget=intents.DailyBudget(
                                            base_usd=-92.0, limit_usd=100.0,
                                            complete=True))
        check("admissao_bloqueia_com_fonte_indisponivel",
              com_funding.decision == intents.BLOCKED_CAPACITY, str(com_funding))
    finally:
        os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"

    # ── 8. Uma instrução por leitura: o snapshot é único ──────────────────
    instrucoes: list = []
    from sqlalchemy.ext.asyncio import AsyncSession
    original_execute = AsyncSession.execute

    async def contando(self, statement, *args, **kwargs):
        texto = str(statement)
        if "real_trades" in texto or "entry_intents" in texto:
            instrucoes.append(texto.split("\n")[0][:60])
        return await original_execute(self, statement, *args, **kwargs)

    await zerar()
    await realizado(-10.0)
    async with db.get_session() as session:
        with patch.object(AsyncSession, "execute", contando):
            await frs.daily_base_in_session(session)
    check("base_do_dia_usa_uma_unica_instrucao", len(instrucoes) == 1, str(instrucoes))

    await db._engine.dispose()
    print(f"R05_SNAPSHOT_PG_OK: {len(CHECKS)} verificações — duas conexões, "
          "barreira entre leituras, snapshot único pós-lock")


if __name__ == "__main__":
    asyncio.run(run())
