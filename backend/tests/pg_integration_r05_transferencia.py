"""R05 C — a transferência reserva→posição não pode escapar do orçamento diário.

`R05_TRANSF_TEST_SOCKET` aponta para /tmp/cw-r05tr-sock.* criado pelo runner.
Admissão REAL (`entry_intent_service.reserve`/`admit_final_risk`) sobre
PostgreSQL 16 descartável, com DUAS conexões e barreiras determinísticas.
Sem TCP/DNS, sem exchange, sem produção.

Defeito: a base financeira do dia era calculada ANTES da lock. Sob a lock só as
reservas eram relidas; a base continuava antiga. Uma reserva alheia que virava
POSIÇÃO no intervalo sumia da soma (pending → 0) e o risco aberto novo só
entrava no teto separado de exposição:

    base -92, outra reserva 6 vira posição, proposta 3, limite diário 100
    → admitia com -92-0-3 = -95; o cenário correto é -92-6-3 = -101 (bloqueio).
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("R05_TRANSF_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r05tr-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r05tr@/r05trdb?host=" + test_socket
os.environ["R05_FINANCIAL_BREAKER_ENABLED"] = "true"     # cutover ligado no ensaio
os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"      # fonte default preservada
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-transferência")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-transferência")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R05-transferência")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CONTA = "c" * 64
CHECKS: list = []
ENVIOS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


async def run():
    from unittest.mock import AsyncMock, patch
    from sqlalchemy import func, select, update
    import db
    from models.entry_intent import EntryIntent
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from services import entry_intent_service as intents
    from services import financial_risk_service as frs
    from services import financial_total_service as fts

    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[RecommendationSnapshot.__table__,
                                        RealTrade.__table__, EntryIntent.__table__])
    await db.init_db()

    agora = datetime.now(timezone.utc)
    # Fechamento ancorado DENTRO da janela calendário vigente (independe da hora).
    fechado = max(frs.kill_daily_start(agora) + timedelta(minutes=1),
                  agora - timedelta(hours=2))

    async def zerar():
        async with db.get_session() as session:
            await session.execute(update(EntryIntent).values(state="TERMINAL"))
            await session.execute(RealTrade.__table__.delete())
            await session.commit()

    async def prejuizo_do_dia(valor=-92.0):
        async with db.get_session() as session:
            session.add(RealTrade(symbol="ALFA/USDT:USDT", exchange="binance",
                                  side="long", qty=1.0, entry_price=100.0,
                                  planned_stop=95.0, status="closed_stop",
                                  source="auto", pnl_usd=valor, entry_fee=0.0,
                                  exit_fee=0.0, opened_at=fechado - timedelta(minutes=30),
                                  closed_at=fechado))
            await session.commit()

    def identidade(trigger: int):
        return intents.EntryIdentity(
            account_ref=CONTA, exchange="binance", symbol="BETA-USDT-USDT",
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

    async def reservar(trigger, risco, *, budget, owner="w-novo", conta=CONTA,
                       teto_risco=50.0):
        ident = intents.EntryIdentity(
            account_ref=conta, exchange="binance", symbol="BETA-USDT-USDT",
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)
        capacidade = intents.Capacity(risk_usd=risco, max_open_positions=5,
                                      max_open_risk_usd=teto_risco)
        return ident, await intents.reserve(
            db.get_session, ident, {"entry": 100.0, "stop_loss": 99.0},
            owner=owner, capacity=capacidade,
            budget=intents.DailyBudget(base_usd=budget.get("base_usd"),
                                       limit_usd=budget.get("limit_usd"),
                                       complete=bool(budget.get("complete"))),
            decision={"entry": 100.0, "stop_loss": 99.0, "qty": risco})

    async def transferir(ident, *, risco=6.0):
        """Conexão A: a reserva alheia vira POSIÇÃO (trade aberto) e some do pending."""
        async with db.get_session() as session:
            trade = RealTrade(symbol="BETA/USDT:USDT", exchange="binance", side="long",
                              qty=risco, entry_price=100.0, planned_stop=99.0,
                              sl_order_id="sl-1", sl_current_price=99.0,
                              status="open", source="auto",
                              client_order_id=ident.client_order_id, opened_at=agora)
            session.add(trade)
            await session.flush()
            await session.execute(update(EntryIntent)
                                  .where(EntryIntent.intent_key == ident.intent_key)
                                  .values(real_trade_id=trade.id))
            await session.commit()

    # ── 1. REPRO: base lida antes, transferência no intervalo, proposta depois ─
    await zerar()
    await prejuizo_do_dia()
    base = await orcamento()
    check("base_do_dia_e_menos_92",
          base["complete"] and abs(base["base_usd"] + 92.0) < 1e-9, str(base))
    alheia, reserva_alheia = await reservar(1_760_000_100_000, 6.0, budget=base,
                                            owner="w-alheia")
    check("reserva_alheia_concedida", reserva_alheia.granted, str(reserva_alheia))

    # Barreira determinística entre as DUAS conexões: a admissão só começa
    # depois que a transferência da outra conexão commitou.
    transferida = asyncio.Event()

    async def conexao_a():
        await transferir(alheia)
        transferida.set()

    async def conexao_b():
        await transferida.wait()
        return await reservar(1_760_000_110_000, 3.0, budget=base)

    _, (proposta, veredito) = await asyncio.gather(conexao_a(), conexao_b())
    async with db.get_session() as session:
        vinculo = (await session.execute(
            select(EntryIntent.real_trade_id)
            .where(EntryIntent.intent_key == alheia.intent_key))).scalar()
    check("reserva_alheia_virou_posicao", vinculo is not None, str(vinculo))
    check("transferencia_nao_escapa_do_orcamento",
          veredito.decision == intents.BLOCKED_CAPACITY
          and veredito.reason == "DAILY_LOSS_LIMIT", str(veredito))

    # ── 2. Vínculo AINDA pendente: conta como reserva, e uma vez só ────────
    await zerar()
    await prejuizo_do_dia()
    base = await orcamento()
    alheia2, ok2 = await reservar(1_760_000_200_000, 6.0, budget=base, owner="w-alheia")
    check("segunda_reserva_alheia_concedida", ok2.granted, str(ok2))
    _, negada = await reservar(1_760_000_210_000, 3.0, budget=base)
    check("reserva_pendente_ja_contava",
          negada.decision == intents.BLOCKED_CAPACITY
          and negada.reason == "DAILY_LOSS_LIMIT", str(negada))

    # ── 3. Posição fechando (resultado persistindo) entre leitura e lock ───
    await zerar()
    await prejuizo_do_dia(-80.0)
    base = await orcamento()
    check("base_inicial_menos_80", abs(base["base_usd"] + 80.0) < 1e-9, str(base))
    fechou = asyncio.Event()

    async def fecha_perdendo():
        await prejuizo_do_dia(-18.0)     # outra perda persiste no intervalo
        fechou.set()

    async def admite_depois():
        await fechou.wait()
        return await reservar(1_760_000_300_000, 3.0, budget=base)

    _, (_, tardio) = await asyncio.gather(fecha_perdendo(), admite_depois())
    check("resultado_persistido_no_intervalo_conta",
          tardio.decision == intents.BLOCKED_CAPACITY
          and tardio.reason == "DAILY_LOSS_LIMIT", str(tardio))

    # ── 4. Preço adverso na readmissão final usa a base ATUAL ─────────────
    await zerar()
    await prejuizo_do_dia(-90.0)
    base = await orcamento()
    minha, concedida = await reservar(1_760_000_400_000, 3.0, budget=base,
                                      owner="w-final")
    check("proposta_cabe_no_inicio", concedida.granted, str(concedida))
    await prejuizo_do_dia(-5.0)          # o dia piora antes do POST
    veredito_final = await intents.admit_final_risk(
        db.get_session, minha.intent_key, owner="w-final", risk_usd=6.0,
        capacity=intents.Capacity(risk_usd=6.0, max_open_positions=5,
                                  max_open_risk_usd=50.0),
        budget=intents.DailyBudget(base_usd=base["base_usd"],
                                   limit_usd=base["limit_usd"], complete=True))
    check("readmissao_usa_a_base_atual",
          veredito_final.decision == intents.BLOCKED_CAPACITY
          and veredito_final.reason == "DAILY_LOSS_LIMIT", str(veredito_final))
    async with db.get_session() as session:
        guardado = float((await session.execute(
            select(EntryIntent.reserved_risk_usd)
            .where(EntryIntent.intent_key == minha.intent_key))).scalar() or 0.0)
    check("negacao_nao_grava_risco_maior", abs(guardado - 3.0) < 1e-9, str(guardado))

    # ── 5. Igualdade com o limite JÁ bloqueia (>=, não >) ──────────────────
    await zerar()
    await prejuizo_do_dia(-97.0)
    base = await orcamento()
    _, igual = await reservar(1_760_000_500_000, 3.0, budget=base)
    check("igualdade_com_o_limite_bloqueia",
          igual.decision == intents.BLOCKED_CAPACITY
          and igual.reason == "DAILY_LOSS_LIMIT", str(igual))

    # ── 6. Própria proposta não é contada duas vezes ──────────────────────
    await zerar()
    await prejuizo_do_dia(-90.0)
    base = await orcamento()
    minha2, ok6 = await reservar(1_760_000_600_000, 5.0, budget=base, owner="w6")
    check("propria_reserva_cabe", ok6.granted, str(ok6))
    readmite = await intents.admit_final_risk(
        db.get_session, minha2.intent_key, owner="w6", risk_usd=5.0,
        capacity=intents.Capacity(risk_usd=5.0, max_open_positions=5,
                                  max_open_risk_usd=50.0),
        budget=intents.DailyBudget(base_usd=base["base_usd"],
                                   limit_usd=base["limit_usd"], complete=True))
    check("propria_reserva_nao_conta_duas_vezes", readmite.granted, str(readmite))

    # ── 7. Base indisponível BLOQUEIA (desconhecido não vira zero) ────────
    await zerar()
    await prejuizo_do_dia()
    base = await orcamento()
    async with db.get_session() as session:      # posição aberta sem stop legível
        session.add(RealTrade(symbol="GAMA/USDT:USDT", exchange="binance", side="long",
                              qty=1.0, entry_price=100.0, planned_stop=None,
                              status="open", source="auto", opened_at=agora))
        await session.commit()
    _, sem_base = await reservar(1_760_000_700_000, 1.0, budget=base)
    check("base_ilegivel_bloqueia",
          sem_base.decision == intents.BLOCKED_CAPACITY
          and sem_base.reason in ("OPEN_RISK_UNKNOWN", "DAILY_BASE_UNAVAILABLE"),
          str(sem_base))

    # ── 8. Sem I/O de exchange dentro da transação ────────────────────────
    async def proibido(*args, **kwargs):
        ENVIOS.append(args)
        raise AssertionError("nenhuma chamada de exchange sob a lock")

    await zerar()
    await prejuizo_do_dia(-10.0)
    base = await orcamento()
    with patch.object(frs, "fetch_equity", proibido):
        _, livre = await reservar(1_760_000_800_000, 1.0, budget=base)
    check("admissao_nao_chama_exchange", livre.granted and ENVIOS == [],
          f"{livre} / {ENVIOS}")

    await db._engine.dispose()
    print(f"R05_TRANSFERENCIA_PG_OK: {len(CHECKS)} verificações — duas conexões, "
          "barreira determinística, base recalculada sob a lock")


if __name__ == "__main__":
    asyncio.run(run())
