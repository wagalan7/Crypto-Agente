"""R05 — reservas de OUTRAS intenções dentro do limite diário (PostgreSQL real).

`R05_RESERVA_TEST_SOCKET` aponta para /tmp/cw-r05res-sock.* criado pelo runner.
O veredito vem do caller financeiro REAL (`shadow_trade_service._r05b_entry_gate`
→ `financial_risk_service.check_new_entry`) e da admissão REAL
(`entry_intent_service.reserve`/`admit_final_risk`). Dispatcher FALSO conta
envios: nenhum POST pode sair. Sem TCP/DNS, sem exchange.

Reprodução do defeito: P&L diário -92, risco aberto 0, taxas 0, limite 100.
Outra intenção tem risco reservado 6 e a entrada proposta arrisca 3.
`check_new_entry` autorizava com pior cenário -95; incluindo a reserva alheia o
pior cenário é -101 e a entrada precisa ser bloqueada.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("R05_RESERVA_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r05res-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r05res@/r05resdb?host=" + test_socket
os.environ["R05_FINANCIAL_BREAKER_ENABLED"] = "true"     # cutover ligado no ensaio
os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"      # fonte default preservada
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-reservas")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R05-reservas")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R05-reservas")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CONTA = "c" * 64
OUTRA_CONTA = "o" * 64
CHECKS: list = []
ENVIOS: list = []          # dispatcher FALSO: qualquer POST aparece aqui


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
    from services import shadow_trade_service as sts

    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[RecommendationSnapshot.__table__,
                                        RealTrade.__table__, EntryIntent.__table__])

    agora = datetime.now(timezone.utc)
    fechado = agora - timedelta(hours=2)
    # P&L diário de -92 (EX-funding, fonte legado preservada), sem posição aberta.
    async with db.get_session() as session:
        session.add(RealTrade(symbol="ALFA/USDT:USDT", exchange="binance", side="long",
                              qty=1.0, entry_price=100.0, planned_stop=95.0,
                              status="closed_stop", source="auto", pnl_usd=-92.0,
                              entry_fee=0.0, exit_fee=0.0,
                              opened_at=fechado - timedelta(hours=1), closed_at=fechado))
        await session.commit()

    equity = {"quality": "OK", "total_usd": 1000.0, "reason_code": None}
    limite = {"quality": "OK", "value": 100.0, "reason_code": None}

    def identidade(trigger: int, *, conta: str = CONTA):
        return intents.EntryIdentity(
            account_ref=conta, exchange="binance", symbol="ALFA-USDT-USDT",
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)

    async def reserva_alheia(trigger: int, risco: float, *, conta: str = CONTA,
                             estado: str = None, trade_id: int = None):
        """Cria a reserva pelo caminho REAL e depois ajusta estado/vínculo."""
        ident = identidade(trigger, conta=conta)
        resultado = await intents.reserve(
            db.get_session, ident, {"entry": 100.0, "stop_loss": 99.0},
            owner="outro", capacity=intents.Capacity(risk_usd=risco))
        assert resultado.granted, resultado
        if estado or trade_id is not None:
            async with db.get_session() as session:
                valores = {}
                if estado:
                    valores["state"] = estado
                if trade_id is not None:
                    valores["real_trade_id"] = trade_id
                await session.execute(update(EntryIntent)
                                      .where(EntryIntent.intent_key == ident.intent_key)
                                      .values(**valores))
                await session.commit()
        return ident

    def ambiente(limite_usd=None):
        alvo = limite if limite_usd is None else {"quality": "OK", "value": limite_usd,
                                                  "reason_code": None}
        frs.reset_cache()
        return (patch.object(frs, "fetch_equity", AsyncMock(return_value=equity)),
                patch.object(frs, "daily_loss_limit_usd", AsyncMock(return_value=alvo)),
                patch.object(fts, "current_account_scope", lambda: CONTA))

    async def gate(*, entry=100.0, stop=99.0, qty=3.0, intent=None, limite_usd=None):
        checks: dict = {}
        a, b, c = ambiente(limite_usd)
        with a, b, c:
            verdict = await sts._r05b_entry_gate(side="long", final_entry=entry, stop=stop,
                                                 final_qty=qty, checks=checks, intent=intent)
        return verdict, checks

    # ── 1. Repro: sem reservas o gate libera -95; com a reserva alheia, -101 ──
    verdict, checks = await gate()
    check("sem_reservas_o_gate_libera", verdict is None, str(verdict))
    check("pior_cenario_sem_reservas_e_95",
          abs(float(checks["r05b_financial"]["worst_case_daily_usd"]) + 95.0) < 1e-6,
          str(checks.get("r05b_financial")))

    await reserva_alheia(1_760_000_100_000, 6.0)
    verdict, checks = await gate()
    check("reserva_alheia_bloqueia", isinstance(verdict, dict) and verdict.get("ok") is False,
          str(verdict))
    check("pior_cenario_com_reserva_e_101",
          abs(float(checks["r05b_financial"]["worst_case_daily_usd"]) + 101.0) < 1e-6,
          str(checks.get("r05b_financial")))
    check("motivo_e_o_limite_diario",
          verdict.get("reason_code") == frs.BLOCK_REASON, str(verdict.get("reason_code")))

    # ── 2. Reserva de OUTRA conta não consome este orçamento ───────────────
    async with db.get_session() as session:      # limpa a reserva desta conta
        await session.execute(update(EntryIntent).values(state="TERMINAL"))
        await session.commit()
    await reserva_alheia(1_760_000_110_000, 6.0, conta=OUTRA_CONTA)
    verdict, _ = await gate()
    check("reserva_de_outra_conta_nao_conta", verdict is None, str(verdict))

    # ── 3. Reserva já transferida para RealTrade não é contada duas vezes ──
    async with db.get_session() as session:
        trade_id = (await session.execute(select(RealTrade.id))).scalars().first()
    await reserva_alheia(1_760_000_120_000, 6.0, trade_id=int(trade_id))
    verdict, _ = await gate()
    check("reserva_virada_trade_nao_conta_de_novo", verdict is None, str(verdict))

    # ── 4. Reserva UNKNOWN (envio incerto) segue consumindo orçamento ──────
    await reserva_alheia(1_760_000_130_000, 6.0, estado="UNKNOWN")
    verdict, checks = await gate()
    check("reserva_unknown_ainda_consome",
          isinstance(verdict, dict) and verdict.get("ok") is False, str(verdict))
    check("unknown_soma_o_mesmo_risco",
          abs(float(checks["r05b_financial"]["reserved_risk_usd"]) - 6.0) < 1e-6,
          str(checks.get("r05b_financial")))

    # ── 5. A PRÓPRIA reserva não é somada de novo ──────────────────────────
    minha = identidade(1_760_000_140_000)
    # MESMO dono do processo: quem reserva é quem despacha (owner+lease).
    dono = sts._INTENT_OWNER
    a, b, c = ambiente()
    with a, b, c:
        orcamento = await frs.daily_budget()
    check("orcamento_tem_base_sem_reservas",
          orcamento.get("enabled") and orcamento.get("complete")
          and abs(float(orcamento["base_usd"]) + 92.0) < 1e-6, str(orcamento))
    reserva = await intents.reserve(
        db.get_session, minha, {"entry": 100.0, "stop_loss": 99.0}, owner=dono,
        capacity=intents.Capacity(risk_usd=3.0),
        budget=intents.DailyBudget(base_usd=orcamento["base_usd"],
                                   limit_usd=orcamento["limit_usd"], complete=True))
    check("admissao_bloqueia_pelo_orcamento",
          reserva.decision == intents.BLOCKED_CAPACITY and reserva.reason == "DAILY_LOSS_LIMIT",
          str(reserva))

    async with db.get_session() as session:      # libera o orçamento (6 → 0)
        await session.execute(update(EntryIntent).values(state="TERMINAL"))
        await session.commit()
    reserva = await intents.reserve(
        db.get_session, minha, {"entry": 100.0, "stop_loss": 99.0}, owner=dono,
        capacity=intents.Capacity(risk_usd=3.0),
        budget=intents.DailyBudget(base_usd=orcamento["base_usd"],
                                   limit_usd=orcamento["limit_usd"], complete=True))
    check("admissao_concede_dentro_do_orcamento", reserva.granted, str(reserva))
    verdict, checks = await gate(intent={"intent_key": minha.intent_key,
                                         "account_ref": CONTA,
                                         "capacity": intents.Capacity(risk_usd=3.0)})
    check("propria_reserva_nao_conta_duas_vezes", verdict is None, str(verdict))
    check("pior_cenario_continua_95",
          abs(float(checks["r05b_financial"]["worst_case_daily_usd"]) + 95.0) < 1e-6,
          str(checks.get("r05b_financial")))
    check("admissao_final_registrada",
          checks.get("r05_admission", {}).get("granted") is True, str(checks.get("r05_admission")))

    # ── 6. Igualdade com o limite JÁ bloqueia (>=, não >) ──────────────────
    verdict, checks = await gate(intent={"intent_key": minha.intent_key,
                                         "account_ref": CONTA}, limite_usd=95.0)
    check("igualdade_com_o_limite_bloqueia",
          isinstance(verdict, dict) and verdict.get("ok") is False, str(verdict))

    # ── 7. Variação adversa antes do POST usa o risco NOVO, não o antigo ───
    # A reserva vale 3; o preço piorou e o risco final vira 9 — com base -92 e
    # limite 100 o cenário é -101 e a admissão final precisa negar.
    veredicto = await intents.admit_final_risk(
        db.get_session, minha.intent_key, owner=dono, risk_usd=9.0,
        budget=intents.DailyBudget(base_usd=orcamento["base_usd"],
                                   limit_usd=orcamento["limit_usd"], complete=True))
    check("risco_adverso_maior_e_negado",
          veredicto.decision == intents.BLOCKED_CAPACITY
          and veredicto.reason == "DAILY_LOSS_LIMIT", str(veredicto))
    async with db.get_session() as session:
        guardado = (await session.execute(
            select(EntryIntent.reserved_risk_usd)
            .where(EntryIntent.intent_key == minha.intent_key))).scalar()
    check("negacao_nao_grava_risco_maior", abs(float(guardado) - 3.0) < 1e-9, str(guardado))
    veredicto = await intents.admit_final_risk(
        db.get_session, minha.intent_key, owner=dono, risk_usd=5.0,
        budget=intents.DailyBudget(base_usd=orcamento["base_usd"],
                                   limit_usd=orcamento["limit_usd"], complete=True))
    check("risco_maior_que_cabe_e_admitido", veredicto.granted, str(veredicto))
    async with db.get_session() as session:
        guardado = (await session.execute(
            select(EntryIntent.reserved_risk_usd)
            .where(EntryIntent.intent_key == minha.intent_key))).scalar()
    check("admissao_atualiza_o_risco_reservado", abs(float(guardado) - 5.0) < 1e-9, str(guardado))

    # ── 8. Duas conexões disputando a última margem: só uma é admitida ─────
    async with db.get_session() as session:
        await session.execute(update(EntryIntent).values(state="TERMINAL"))
        await session.commit()
    # Base -92, limite 100: cabem 8 no total. Duas decisões de 5 não cabem juntas.
    disputa = intents.DailyBudget(base_usd=orcamento["base_usd"],
                                  limit_usd=orcamento["limit_usd"], complete=True)

    async def concorrente(trigger: int):
        return await intents.reserve(
            db.get_session, identidade(trigger), {"entry": 100.0, "stop_loss": 99.0},
            owner=f"w{trigger}", capacity=intents.Capacity(risk_usd=5.0), budget=disputa)

    a_res, b_res = await asyncio.gather(concorrente(1_760_000_200_000),
                                        concorrente(1_760_000_210_000))
    concedidas = [r for r in (a_res, b_res) if r.granted]
    check("so_uma_decisao_consome_a_ultima_margem", len(concedidas) == 1,
          f"{a_res} / {b_res}")
    negada = [r for r in (a_res, b_res) if not r.granted][0]
    check("a_outra_e_negada_pelo_limite_diario", negada.reason == "DAILY_LOSS_LIMIT",
          str(negada))
    async with db.get_session() as session:
        somado = float((await session.execute(
            select(func.coalesce(func.sum(EntryIntent.reserved_risk_usd), 0.0))
            .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN")),
                   EntryIntent.account_ref == CONTA))).scalar() or 0.0)
    check("reservado_nao_ultrapassa_o_orcamento", somado <= 8.0 + 1e-9, str(somado))

    # ── 9. Orçamento incompleto/erro BLOQUEIA (desconhecido nunca vira zero) ─
    for incompleto in (intents.DailyBudget(base_usd=None, limit_usd=100.0, complete=True),
                       intents.DailyBudget(base_usd=-92.0, limit_usd=None, complete=True),
                       intents.DailyBudget(base_usd=-92.0, limit_usd=100.0, complete=False)):
        negada = await intents.reserve(
            db.get_session, identidade(1_760_000_300_000), {"entry": 100.0, "stop_loss": 99.0},
            owner="w9", capacity=intents.Capacity(risk_usd=1.0), budget=incompleto)
        check(f"orcamento_incompleto_bloqueia_{incompleto.base_usd}_{incompleto.limit_usd}_{incompleto.complete}",
              negada.decision == intents.BLOCKED_CAPACITY
              and negada.reason == "DAILY_BUDGET_UNKNOWN", str(negada))
    async with db.get_session() as session:
        sobrou = int((await session.execute(
            select(func.count(EntryIntent.intent_key))
            .where(EntryIntent.trigger_candle_ms == 1_760_000_300_000))).scalar() or 0)
    check("negacao_nao_deixa_intencao_parcial", sobrou == 0, str(sobrou))

    # ── 10. Leitura das reservas indisponível NÃO vira zero ────────────────
    a, b, c = ambiente()
    with a, b, c, patch.object(fts, "current_account_scope", lambda: None):
        sem_conta = await frs.reserved_risk_usd()
    check("conta_nao_identificada_e_unknown",
          sem_conta.get("quality") == "UNKNOWN" and sem_conta.get("value") is None,
          str(sem_conta))
    # Sem identidade da proposta, TODAS as reservas contam — a dúvida pesa
    # contra abrir exposição, nunca a favor.
    verdict, checks = await gate(intent={"intent_key": None, "account_ref": None})
    check("sem_identidade_nenhuma_reserva_e_descontada",
          isinstance(verdict, dict) and verdict.get("ok") is False
          and float(checks["r05b_financial"]["reserved_risk_usd"]) > 0.0,
          str(checks.get("r05b_financial")))

    # ── 11. Fonte com funding: a reserva entra no total completo também ────
    os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "accounting_total"
    try:
        verdict, checks = await gate()
        check("fonte_completa_sem_ledger_nao_libera",
              isinstance(verdict, dict) and verdict.get("ok") is False, str(verdict))
    finally:
        os.environ["R05_FINANCIAL_TOTAL_SOURCE"] = "legacy"

    check("dispatcher_falso_nao_recebeu_envio", ENVIOS == [], str(ENVIOS))
    print(f"R05_RESERVAS_PG_OK: {len(CHECKS)} verificações — "
          f"gate real, admissão serializada, zero envios")


def main():
    import services.binance_signed_service as bss   # dispatcher FALSO

    async def proibido(*args, **kwargs):
        ENVIOS.append(args)
        raise AssertionError("nenhum envio pode sair neste teste")

    for nome in ("place_order", "place_protection_orders", "cancel_order",
                 "close_position_market"):
        if hasattr(bss, nome):
            setattr(bss, nome, proibido)
    asyncio.run(run())


main()
