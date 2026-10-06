"""P03 A/B — ausência de posição não prova ausência de execução, e a recuperação
nasce com os dados point-in-time da decisão.

`P03_SETTLE_TEST_SOCKET` aponta para /tmp/cw-p03set-sock.* criado pelo runner.
Roda o CICLO REAL (`recover_entry_intents`/`reconcile_due`) sobre PostgreSQL 16
descartável, com exchange FALSA que só responde consultas e conta chamadas.
Sem TCP/DNS, sem ordem, sem produção.

Defeito A: ordem FILLED com qty 1 e posição fresh-flat resolvia o incidente como
FLAT, e a intenção virava TERMINAL/`RECONCILED_NO_EXECUTION` — o MESMO rótulo de
"não executou" para uma entrada que foi preenchida e depois encerrada, sem
vínculo contábil nenhum.

Defeito B: o incidente criado pela recuperação não carregava planned_stop/qty,
então `_ensure_stop` não conseguia sequer ADOTAR um SL vivo e válido: o caminho
parava em SL_NOT_CONFIRMED/RETRY_PENDING antes de chegar ao RealTrade.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("P03_SETTLE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-p03set-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://p03set@/p03setdb?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-liquidação")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-liquidação")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste P03-liquidação")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
MUTACOES: list = []
SIMBOLO = "ALFA/USDT:USDT"
GUARDADO = "ALFA-USDT-USDT"


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


async def run():
    from unittest.mock import patch
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
    await db.init_db()      # colunas aditivas (dispatch_ids/decision_payload)

    ORDENS: dict = {}
    POSICOES: dict = {"positions": []}
    ALGOS: dict = {"orders": []}

    async def fake_get_order(symbol, order_id=None, client_order_id=None, **kwargs):
        resposta = ORDENS.get(client_order_id)
        return resposta if resposta is not None else {"ok": False, "error": "sem resposta"}

    async def fake_get_positions(symbol=None, force=False, **kwargs):
        return {"ok": True, **POSICOES}

    async def fake_algo_orders(symbol=None, **kwargs):
        return {"ok": True, **ALGOS}

    def mutacao(nome):
        async def _mutacao(*args, **kwargs):
            MUTACOES.append(nome)
            raise AssertionError(f"mutação proibida no teste: {nome}")
        return _mutacao

    patches = [patch.object(bss, "get_order", fake_get_order),
               patch.object(bss, "get_positions", fake_get_positions),
               patch.object(bss, "get_open_algo_orders", fake_algo_orders)]
    for nome in ("cancel_order", "cancel_algo_order", "place_order",
                 "place_protection_orders", "close_position_market"):
        if hasattr(bss, nome):
            patches.append(patch.object(bss, nome, mutacao(nome)))
    for item in patches:
        item.start()

    ers.set_repo(None)
    ers._boot_scan_safe = True

    def identidade(trigger: int):
        return intents.EntryIdentity(
            account_ref="c" * 64, exchange="binance", symbol=GUARDADO, quote="USDT",
            side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2", purpose="ENTRY",
            trigger_candle_ms=trigger)

    async def intencao_unknown(trigger: int, *, ids=None, decision=None,
                               entry=100.0, stop=95.0, qty=1.0):
        """Caminho REAL: reserva → sending → ids efetivos → unknown."""
        ident = identidade(trigger)
        reserva = await intents.reserve(
            db.get_session, ident,
            {"entry": entry, "stop_loss": stop, "tp1": entry + 5, "tp2": entry + 10,
             "leverage": 3},
            owner="w1",
            decision=(decision if decision is not None else
                      {"entry": entry, "stop_loss": stop, "qty": qty}))
        assert reserva.granted, reserva
        assert await intents.mark_sending(db.get_session, ident.intent_key, owner="w1")
        for dispatch_id in (ids or [ident.client_order_id]):
            await intents.register_dispatch(db.get_session, ident.intent_key,
                                            owner="w1", dispatch_id=dispatch_id)
        assert await intents.mark_unknown(db.get_session, ident.intent_key, owner="w1",
                                          reason="DISPATCH_OUTCOME_UNKNOWN")
        return ident

    async def estado(intent_key):
        row = await intents.get_intent(db.get_session, intent_key)
        return (row.state, row.real_trade_id, row.reason) if row else (None, None, None)

    async def incidente(coid):
        async with db.get_session() as session:
            return (await session.execute(
                select(ExecutionIncident.state, ExecutionIncident.resolved_at,
                       ExecutionIncident.manual_reason, ExecutionIncident.min_known_fill,
                       ExecutionIncident.planned_stop, ExecutionIncident.planned_qty)
                .where(ExecutionIncident.client_order_id == coid))).one()

    async def pendentes():
        async with db.get_session() as session:
            return int((await session.execute(
                select(func.count(EntryIntent.intent_key))
                .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN"))))).scalar() or 0)

    async def pausado():
        from services import risk_service
        async with db.get_session() as session:
            row = (await session.execute(select(RiskState))).scalars().first()
            return bool(row and row.trading_paused)

    def sl_vivo(algo_id="SL-1", stop=95.0, qty=1.0):
        return {"algo_id": algo_id, "symbol": "ALFAUSDT", "side": "SELL",
                "type": "STOP_MARKET", "status": "NEW", "close_position": True,
                "trigger_price": stop, "quantity": qty}

    async def adiantar_retries():
        async with db.get_session() as session:
            await session.execute(update(ExecutionIncident)
                                  .where(ExecutionIncident.resolved_at.is_(None))
                                  .values(next_retry_at=datetime.now(timezone.utc)
                                          - timedelta(minutes=5)))
            await session.commit()

    agora = datetime.now(timezone.utc)

    # ═══════════════════════════════════════════════════════════════════════
    #  A. FILLED → posição já encerrada, COM resultado recuperável
    # ═══════════════════════════════════════════════════════════════════════
    ident_a = await intencao_unknown(1_760_000_100_000)
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=1.0,
                          entry_price=100.0, planned_stop=95.0, status="closed_tp1",
                          source="auto", client_order_id=ident_a.client_order_id,
                          pnl_usd=4.2, opened_at=agora - timedelta(hours=2),
                          closed_at=agora - timedelta(hours=1))
        session.add(trade)
        await session.commit()
        trade_a = trade.id
    ORDENS[ident_a.client_order_id] = {"ok": True, "status": "FILLED",
                                       "executed_qty": 1.0, "orig_qty": 1.0,
                                       "avg_price": 100.0,
                                       "raw": {"executedQty": "1", "avgPrice": "100"}}
    POSICOES["positions"] = []          # fresh-FLAT: a posição já foi encerrada
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    estado_a = await estado(ident_a.intent_key)
    check("fill_encerrado_nao_vira_sem_execucao",
          estado_a[2] != "RECONCILED_NO_EXECUTION", str(estado_a))
    check("fill_encerrado_vincula_o_trade_fechado",
          estado_a[0] == "CONFIRMED" and estado_a[1] == trade_a, str(estado_a))
    check("execucao_provada_fica_registrada_no_incidente",
          (await incidente(ident_a.client_order_id))[3] == 1.0,
          str(await incidente(ident_a.client_order_id)))
    check("slot_devolvido_apos_vinculo", await pendentes() == 0, str(await pendentes()))

    # ═══════════════════════════════════════════════════════════════════════
    #  A2. FILLED → posição encerrada SEM resultado recuperável ⇒ bloqueio
    # ═══════════════════════════════════════════════════════════════════════
    ident_a2 = await intencao_unknown(1_760_000_110_000)
    ORDENS[ident_a2.client_order_id] = {"ok": True, "status": "FILLED",
                                        "executed_qty": 1.0, "orig_qty": 1.0,
                                        "avg_price": 100.0,
                                        "raw": {"executedQty": "1", "avgPrice": "100"}}
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    estado_a2 = await estado(ident_a2.intent_key)
    check("execucao_sem_vinculo_nao_encerra", estado_a2[0] == "UNKNOWN", str(estado_a2))
    check("execucao_sem_vinculo_declara_motivo",
          estado_a2[2] not in (None, "RECONCILED_NO_EXECUTION"), str(estado_a2))
    inc_a2 = await incidente(ident_a2.client_order_id)
    check("execucao_sem_vinculo_escala_manual",
          inc_a2[0] == "MANUAL_REQUIRED" and inc_a2[2], str(inc_a2))
    check("execucao_sem_vinculo_mantem_pausa", await pausado(), "quarentena caiu")
    check("execucao_sem_vinculo_segura_o_slot", await pendentes() >= 1,
          str(await pendentes()))

    # ═══════════════════════════════════════════════════════════════════════
    #  A3. REJECTED/zero comprovado continua encerrando sem execução
    # ═══════════════════════════════════════════════════════════════════════
    ident_a3 = await intencao_unknown(1_760_000_120_000)
    ORDENS[ident_a3.client_order_id] = {"ok": True, "status": "REJECTED",
                                        "executed_qty": 0.0, "orig_qty": 1.0,
                                        "raw": {"executedQty": "0"}}
    await adiantar_retries()
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    estado_a3 = await estado(ident_a3.intent_key)
    check("zero_comprovado_encerra_sem_execucao",
          estado_a3[0] == "TERMINAL" and estado_a3[2] == "RECONCILED_NO_EXECUTION",
          str(estado_a3))

    # ═══════════════════════════════════════════════════════════════════════
    #  B. Primária rejeitada + filha preenchida com SL vivo ⇒ PROTECTED
    # ═══════════════════════════════════════════════════════════════════════
    coid_b = identidade(1_760_000_200_000).client_order_id
    filha = f"{coid_b[:32]}-mfb"[:36]
    # Os dois ids são registrados enquanto a intenção está em SENDING (é assim
    # que o executor faz: grava o id efetivo ANTES de cada POST).
    ident_b = await intencao_unknown(1_760_000_200_000, entry=200.0, stop=190.0,
                                     qty=2.0, ids=[coid_b, filha])
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=2.0,
                          entry_price=200.0, planned_stop=190.0, status="open",
                          source="auto", client_order_id=filha,
                          opened_at=agora - timedelta(minutes=10))
        session.add(trade)
        await session.commit()
        trade_b = trade.id
    ORDENS[ident_b.client_order_id] = {"ok": True, "status": "REJECTED",
                                       "executed_qty": 0.0, "orig_qty": 2.0,
                                       "raw": {"executedQty": "0"}}
    ORDENS[filha] = {"ok": True, "status": "FILLED", "executed_qty": 2.0,
                     "orig_qty": 2.0, "avg_price": 200.0,
                     "raw": {"executedQty": "2", "avgPrice": "200"}}
    POSICOES["positions"] = [{"symbol": "ALFAUSDT", "side": "buy", "size": 2.0}]
    ALGOS["orders"] = [sl_vivo("SL-FILHA", stop=190.0, qty=2.0)]
    await adiantar_retries()
    await ers.recover_entry_intents()
    inc_filha = await incidente(filha)
    check("recuperacao_transporta_stop_e_qty",
          inc_filha[4] == 190.0 and inc_filha[5] == 2.0, str(inc_filha))
    await ers.reconcile_due()
    inc_filha = await incidente(filha)
    check("filha_preenchida_adota_sl_vivo", inc_filha[0] == "PROTECTED", str(inc_filha))
    estado_b = await estado(ident_b.intent_key)
    check("filha_preenchida_vincula_o_trade",
          estado_b[0] == "CONFIRMED" and estado_b[1] == trade_b, str(estado_b))
    POSICOES["positions"] = []
    ALGOS["orders"] = []

    # ═══════════════════════════════════════════════════════════════════════
    #  B2. Crash ANTES do incidente original: a recuperação ainda tem os dados
    # ═══════════════════════════════════════════════════════════════════════
    ident_b2 = await intencao_unknown(1_760_000_300_000, entry=50.0, stop=45.0, qty=3.0)
    async with db.get_session() as session:      # nenhum incidente foi gravado
        gravados = int((await session.execute(
            select(func.count(ExecutionIncident.id))
            .where(ExecutionIncident.client_order_id == ident_b2.client_order_id))).scalar() or 0)
    check("crash_antes_do_incidente_nao_deixa_rastro", gravados == 0, str(gravados))
    await db._engine.dispose()                   # restart do processo/pool
    ORDENS[ident_b2.client_order_id] = {"ok": True, "status": "FILLED",
                                        "executed_qty": 3.0, "orig_qty": 3.0,
                                        "avg_price": 50.0,
                                        "raw": {"executedQty": "3", "avgPrice": "50"}}
    POSICOES["positions"] = [{"symbol": "ALFAUSDT", "side": "buy", "size": 3.0}]
    ALGOS["orders"] = [sl_vivo("SL-B2", stop=45.0, qty=3.0)]
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=3.0,
                          entry_price=50.0, planned_stop=45.0, status="open",
                          source="auto", client_order_id=ident_b2.client_order_id,
                          opened_at=agora)
        session.add(trade)
        await session.commit()
        trade_b2 = trade.id
    await adiantar_retries()
    await ers.recover_entry_intents()
    inc_b2 = await incidente(ident_b2.client_order_id)
    check("incidente_novo_nasce_com_os_dados_da_decisao",
          inc_b2[4] == 45.0 and inc_b2[5] == 3.0, str(inc_b2))
    await ers.reconcile_due()
    estado_b2 = await estado(ident_b2.intent_key)
    check("apos_restart_o_fill_vincula", estado_b2[0] == "CONFIRMED"
          and estado_b2[1] == trade_b2, str(estado_b2))
    POSICOES["positions"] = []
    ALGOS["orders"] = []

    # ═══════════════════════════════════════════════════════════════════════
    #  B3. Legado SEM dados da decisão continua seguro (nada é inventado)
    # ═══════════════════════════════════════════════════════════════════════
    ident_b3 = await intencao_unknown(1_760_000_400_000, entry=70.0, stop=65.0, qty=1.0)
    async with db.get_session() as session:      # simula linha antiga, sem payload
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == ident_b3.intent_key)
                              .values(decision_payload=None))
        await session.commit()
    ORDENS[ident_b3.client_order_id] = {"ok": True, "status": "FILLED",
                                        "executed_qty": 1.0, "orig_qty": 1.0,
                                        "avg_price": 70.0,
                                        "raw": {"executedQty": "1", "avgPrice": "70"}}
    POSICOES["positions"] = [{"symbol": "ALFAUSDT", "side": "buy", "size": 1.0}]
    ALGOS["orders"] = [sl_vivo("SL-B3", stop=65.0, qty=1.0)]
    await adiantar_retries()
    await ers.recover_entry_intents()
    inc_b3 = await incidente(ident_b3.client_order_id)
    check("legado_sem_payload_nao_inventa_stop", inc_b3[4] is None, str(inc_b3))
    await ers.reconcile_due()
    estado_b3 = await estado(ident_b3.intent_key)
    check("legado_sem_prova_nao_encerra", estado_b3[0] == "UNKNOWN", str(estado_b3))
    POSICOES["positions"] = []
    ALGOS["orders"] = []

    # ═══════════════════════════════════════════════════════════════════════
    #  C. Vínculo por coincidência de símbolo é recusado
    # ═══════════════════════════════════════════════════════════════════════
    ident_c = await intencao_unknown(1_760_000_500_000, entry=10.0, stop=9.0, qty=5.0)
    async with db.get_session() as session:      # trade de OUTRA conta/exchange
        session.add(RealTrade(symbol=SIMBOLO, exchange="bybit", side="long", qty=5.0,
                              entry_price=10.0, planned_stop=9.0, status="open",
                              source="auto", client_order_id=ident_c.client_order_id,
                              opened_at=agora))
        await session.commit()
    ORDENS[ident_c.client_order_id] = {"ok": True, "status": "FILLED",
                                       "executed_qty": 5.0, "orig_qty": 5.0,
                                       "avg_price": 10.0,
                                       "raw": {"executedQty": "5", "avgPrice": "10"}}
    await adiantar_retries()
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    estado_c = await estado(ident_c.intent_key)
    check("exchange_divergente_nao_vincula",
          estado_c[0] == "UNKNOWN" and estado_c[1] is None, str(estado_c))

    # ═══════════════════════════════════════════════════════════════════════
    #  D. Repetição e restart não reabrem o que já foi provado
    # ═══════════════════════════════════════════════════════════════════════
    await db._engine.dispose()
    for _ in range(3):
        await ers.reconcile_due()
        await ers.recover_entry_intents()
    check("confirmada_permanece_confirmada",
          (await estado(ident_a.intent_key))[:2] == ("CONFIRMED", trade_a),
          str(await estado(ident_a.intent_key)))
    check("terminal_permanece_terminal",
          (await estado(ident_a3.intent_key))[0] == "TERMINAL",
          str(await estado(ident_a3.intent_key)))
    check("zero_mutacao_na_exchange", MUTACOES == [], str(MUTACOES))

    await db._engine.dispose()
    print(f"P03_SETTLEMENT_PG_OK: {len(CHECKS)} verificações — ciclo real, "
          "exchange falsa, vínculo contábil provado")


if __name__ == "__main__":
    asyncio.run(run())
