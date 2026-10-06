"""P03 — intenção e incidente resolvidos no MESMO ciclo lógico (PostgreSQL real).

`P03_CYCLE_TEST_SOCKET` aponta para /tmp/cw-p03cyc-sock.* criado pelo runner.
Roda o ciclo REAL (`reconcile_due`/`boot_reconcile`) com exchange FALSA que só
responde consultas e conta chamadas. Sem TCP/DNS, sem ordem real.

Reprodução do defeito: intenção UNKNOWN com id conhecido e ordem REJECTED
(qty 0) fazia `_reconcile_one` resolver o incidente (FLAT) e, logo depois,
`recover_entry_intents` reabri-lo pela MESMA prova — `open_now=1` para sempre,
sem nunca resolver a intenção nem devolver slot/risco.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("P03_CYCLE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-p03cyc-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://p03cyc@/p03cycdb?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-ciclo")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-ciclo")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste P03-ciclo")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
MUTACOES: list = []          # qualquer tentativa de MUTAR a exchange
CONSULTAS: list = []
SIMBOLO = "ALFA/USDT:USDT"
GUARDADO = "ALFA-USDT-USDT"


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
    from models.execution_incident import ExecutionIncident
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from models.risk_state import RiskState
    from services import binance_signed_service as bss
    from services import entry_intent_service as intents
    from services import execution_reconciliation_service as ers

    # Ownership com prova FRESCA é pré-requisito da admissão: sem o registro de
    # reconhecimento manual e sem a época da conta, toda reserva é negada com
    # MANUAL_POSITION_SYMBOL_BLOCKED. A fixture cria as tabelas e declara a conta
    # liberada — não desliga o guard.
    from models.account_margin_epoch import AccountMarginEpoch
    from models.manual_position_ack import ManualPositionAcknowledgement
    from services import manual_position_service as mps
    from sqlalchemy import text as _sql_text
    tabelas = [RecommendationSnapshot.__table__, RealTrade.__table__,
               EntryIntent.__table__, ExecutionIncident.__table__, RiskState.__table__,
               AccountMarginEpoch.__table__, ManualPositionAcknowledgement.__table__]
    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tabelas)
    async with db.get_session() as session:
        for _escopo in ("c" * 64,):
            await session.execute(_sql_text(
                "INSERT INTO account_margin_epochs (account_scope, exchange, market, "
                "generation, manual_validation_generation, manual_validation_blocked, "
                "updated_at) VALUES (:s, 'binance', 'usdm_futures', 0, 0, false, now()) "
                "ON CONFLICT (account_scope, exchange, market) DO UPDATE SET "
                "manual_validation_blocked = false"), {"s": _escopo})
        await session.commit()
    mps.reset_local_validation_state()

    ORDENS: dict = {}
    POSICOES: dict = {"positions": []}

    async def fake_get_order(symbol, order_id=None, client_order_id=None, **kwargs):
        CONSULTAS.append(("get_order", client_order_id or order_id))
        resposta = ORDENS.get(client_order_id)
        return resposta if resposta is not None else {"ok": False, "error": "sem resposta"}

    async def fake_get_positions(symbol=None, force=False, **kwargs):
        CONSULTAS.append(("get_positions", symbol))
        return {"ok": True, **POSICOES}

    ALGOS: dict = {"orders": []}

    async def fake_algo_orders(symbol=None, **kwargs):
        CONSULTAS.append(("algo_orders", symbol))
        return {"ok": True, **ALGOS}

    def mutacao(nome):
        async def _mutacao(*args, **kwargs):
            MUTACOES.append(nome)
            raise AssertionError(f"mutação proibida no teste: {nome}")
        return _mutacao

    patches = [
        patch.object(bss, "get_order", fake_get_order),
        patch.object(bss, "get_positions", fake_get_positions),
        patch.object(bss, "get_open_algo_orders", fake_algo_orders),
    ]
    for nome in ("cancel_order", "cancel_algo_order", "place_order",
                 "place_protection_orders", "close_position_market"):
        if hasattr(bss, nome):
            patches.append(patch.object(bss, nome, mutacao(nome)))
    for item in patches:
        item.start()

    def identidade(coid_suffix: str, trigger: int):
        return intents.EntryIdentity(
            account_ref="c" * 64, exchange="binance", symbol=GUARDADO, quote="USDT",
            side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2", purpose="ENTRY",
            trigger_candle_ms=trigger)

    async def intencao_unknown(trigger: int, *, ids=None, reason="DISPATCH_OUTCOME_UNKNOWN"):
        """Cria a intenção pelo caminho REAL: reserva → sending → ids → unknown."""
        ident = identidade("x", trigger)
        reserva = await intents.reserve(db.get_session, ident,
                                        {"entry": 100.0, "stop_loss": 95.0,
                                         "tp1": 105.0, "tp2": 110.0, "leverage": 3},
                                        owner="w1")
        assert reserva.granted, reserva
        assert await intents.mark_sending(db.get_session, ident.intent_key, owner="w1")
        for dispatch_id in (ids or [ident.client_order_id]):
            assert await intents.register_dispatch(db.get_session, ident.intent_key,
                                                   owner="w1", dispatch_id=dispatch_id)
        assert await intents.mark_unknown(db.get_session, ident.intent_key,
                                          owner="w1", reason=reason)
        return ident

    async def estado(intent_key):
        row = await intents.get_intent(db.get_session, intent_key)
        return (row.state, row.real_trade_id) if row else (None, None)

    async def incidentes_abertos():
        async with db.get_session() as session:
            return int((await session.execute(
                select(func.count(ExecutionIncident.id))
                .where(ExecutionIncident.resolved_at.is_(None)))).scalar() or 0)

    async def pendentes():
        async with db.get_session() as session:
            return int((await session.execute(
                select(func.count(EntryIntent.intent_key))
                .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN"))))).scalar() or 0)

    ers.set_repo(None)
    ers._boot_scan_safe = True

    # ── 1. REJECTED com qty zero: sem execução, intenção encerra ───────────
    ident1 = await intencao_unknown(1_760_000_100_000)
    ORDENS[ident1.client_order_id] = {"ok": True, "status": "REJECTED",
                                      "executedQty": "0", "avgPrice": "0",
                                      "clientOrderId": ident1.client_order_id}
    await ers.recover_entry_intents()          # abre o incidente pela primeira vez
    check("incidente_aberto_para_intencao_incerta", await incidentes_abertos() == 1)
    ciclo = await ers.reconcile_due()          # reconcilia e resolve o incidente
    check("ciclo_resolve_o_incidente", ciclo["open_now"] == 0, str(ciclo))
    estado1 = await estado(ident1.intent_key)
    check("intencao_encerra_sem_execucao", estado1 == ("TERMINAL", None), str(estado1))
    check("reserva_devolvida", await pendentes() == 0, str(await pendentes()))

    # ── 2. Repetição por três ciclos: nada reabre pela MESMA prova ─────────
    for _ in range(3):
        resultado = await ers.reconcile_due()
    check("mesma_prova_nao_reabre", resultado["open_now"] == 0, str(resultado))
    check("intencao_permanece_encerrada",
          (await estado(ident1.intent_key))[0] == "TERMINAL")

    # ── 3. Execução comprovada e protegida: CONFIRMED vinculada ao RealTrade ──
    # Caminho REAL: o executor grava o incidente (kind por safety_state, com
    # stop/qty planejados) e a recuperação da intenção apenas o encontra.
    ident2 = await intencao_unknown(1_760_000_200_000)
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=1.0,
                          entry_price=100.0, planned_stop=95.0, status="open",
                          source="auto", client_order_id=ident2.client_order_id,
                          opened_at=datetime.now(timezone.utc))
        session.add(trade)
        await session.commit()
        trade_id = trade.id
    await ers.record_incident(**ers.assemble_entry_incident(
        {"client_order_id": ident2.client_order_id, "was_maker": False,
         "safety_state": "ENTRY_SUBMISSION_UNKNOWN", "submitted_qty": 1.0,
         "executed_qty": 1.0},
        {"symbol": SIMBOLO, "direction": "long", "stop_loss": 95.0, "qty": 1.0}))
    # Contrato NORMALIZADO do transport (snake_case) + `raw` da exchange.
    ORDENS[ident2.client_order_id] = {"ok": True, "status": "FILLED",
                                      "executed_qty": 1.0, "orig_qty": 1.0,
                                      "avg_price": 100.0,
                                      "raw": {"executedQty": "1", "avgPrice": "100"}}
    POSICOES["positions"] = [{"symbol": "ALFAUSDT", "side": "buy", "size": 1.0}]
    ALGOS["orders"] = [{"algo_id": "SL-ALFA-1", "symbol": "ALFAUSDT", "side": "SELL",
                        "type": "STOP_MARKET", "status": "NEW",
                        "close_position": True, "trigger_price": 95.0,
                        "quantity": 1.0}]
    await ers.recover_entry_intents()
    ciclo2 = await ers.reconcile_due()
    async with db.get_session() as session:
        incidente2 = (await session.execute(
            select(ExecutionIncident.state, ExecutionIncident.resolved_at)
            .where(ExecutionIncident.client_order_id == ident2.client_order_id))).one()
    check("incidente_do_fill_resolve_protegido",
          incidente2[0] == "PROTECTED" and incidente2[1] is not None, str(incidente2))
    # MESMO ciclo lógico: provado o fill, a intenção fecha CONFIRMED vinculada ao
    # RealTrade — nunca encerrada como "sem execução".
    estado2 = await estado(ident2.intent_key)
    check("execucao_vincula_o_real_trade", estado2 == ("CONFIRMED", trade_id),
          f"{estado2} / {ciclo2}")
    check("reserva_vira_exposicao_sem_slot_pendente", await pendentes() == 0,
          str(await pendentes()))
    # Repetir o ciclo não reabre o incidente nem reverte a intenção confirmada.
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    check("confirmada_nao_volta_a_unknown",
          (await estado(ident2.intent_key)) == ("CONFIRMED", trade_id))
    POSICOES["positions"] = []
    ALGOS["orders"] = []

    # ── 4. Primária zero com filha `-mfb`: sem prova da filha, nada resolve ─
    ident3 = await intencao_unknown(1_760_000_300_000)
    filha = f"{ident3.client_order_id[:32]}-mfb"[:36]
    assert await intents.register_dispatch(db.get_session, ident3.intent_key,
                                           owner="w1", dispatch_id=filha) is False
    async with db.get_session() as session:   # a filha existe no ledger da intenção
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == ident3.intent_key)
                              .values(dispatch_ids=[ident3.client_order_id, filha]))
        await session.commit()
    ORDENS[ident3.client_order_id] = {"ok": True, "status": "REJECTED", "executedQty": "0",
                                      "avgPrice": "0"}
    ORDENS[filha] = {"ok": False, "error": "consulta indisponível"}
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    estado3 = await estado(ident3.intent_key)
    check("primaria_rejeitada_nao_prova_ausencia_na_filha",
          estado3[0] == "UNKNOWN", str(estado3))
    check("incidente_da_filha_existe", await incidentes_abertos() >= 1)

    # Com a filha comprovadamente sem fill, aí sim encerra. O backoff do retry
    # é tempo, não lógica: adianta-se o relógio da fila, nada mais.
    ORDENS[filha] = {"ok": True, "status": "EXPIRED", "executed_qty": 0.0,
                     "raw": {"executedQty": "0"}}
    async with db.get_session() as session:
        await session.execute(update(ExecutionIncident)
                              .where(ExecutionIncident.resolved_at.is_(None))
                              .values(next_retry_at=datetime.now(timezone.utc)
                                      - timedelta(minutes=5)))
        await session.commit()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    estado3b = await estado(ident3.intent_key)
    check("com_prova_das_duas_pernas_encerra", estado3b[0] == "TERMINAL", str(estado3b))

    # ── 5. Consulta incerta permanece incerta ──────────────────────────────
    ident4 = await intencao_unknown(1_760_000_400_000)
    ORDENS[ident4.client_order_id] = {"ok": False, "error": "timeout"}
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    check("consulta_incerta_nao_resolve",
          (await estado(ident4.intent_key))[0] == "UNKNOWN")
    check("quarentena_segue_ativa_com_incerteza", await incidentes_abertos() >= 1)

    # ── 6. Restart: o estado persiste e o ciclo não perde a prova ──────────
    await db._engine.dispose()
    ORDENS[ident4.client_order_id] = {"ok": True, "status": "CANCELED",
                                      "executed_qty": 0.0, "raw": {"executedQty": "0"}}
    async with db.get_session() as session:
        await session.execute(update(ExecutionIncident)
                              .where(ExecutionIncident.resolved_at.is_(None))
                              .values(next_retry_at=datetime.now(timezone.utc)
                                      - timedelta(minutes=5)))
        await session.commit()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    check("restart_preserva_e_resolve",
          (await estado(ident4.intent_key))[0] == "TERMINAL")

    # ── 7. Duas conexões concorrentes: uma resolução, sem duplicar ─────────
    ident5 = await intencao_unknown(1_760_000_500_000)
    ORDENS[ident5.client_order_id] = {"ok": True, "status": "REJECTED", "executedQty": "0",
                                      "avgPrice": "0"}
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    resultados = await asyncio.gather(ers.recover_entry_intents(),
                                      ers.recover_entry_intents())
    resolvidas = sum(int(item.get("resolved") or 0) for item in resultados)
    check("concorrencia_resolve_uma_vez", resolvidas <= 1, str(resultados))
    check("estado_final_da_concorrencia",
          (await estado(ident5.intent_key))[0] == "TERMINAL")

    # ── 8. Callback tardio não rebaixa a confirmação ───────────────────────
    ident6 = await intencao_unknown(1_760_000_600_000)
    assert await intents.mark_confirmed(db.get_session, ident6.intent_key, real_trade_id=777)
    await ers.recover_entry_intents()
    check("confirmacao_nao_e_rebaixada",
          (await estado(ident6.intent_key)) == ("CONFIRMED", 777),
          str(await estado(ident6.intent_key)))

    # ── 9. Nenhuma mutação de exchange em todo o ensaio ────────────────────
    check("zero_mutacoes_na_exchange", MUTACOES == [], str(MUTACOES))
    check("houve_consulta_real", any(nome == "get_order" for nome, _ in CONSULTAS))

    for item in reversed(patches):
        item.stop()
    ers.set_repo(None)
    await db._engine.dispose()
    print(f"P03_CYCLE_PG_OK: {len(CHECKS)} verificações — ciclo real, exchange falsa, "
          "sem mutação")


if __name__ == "__main__":
    asyncio.run(run())
