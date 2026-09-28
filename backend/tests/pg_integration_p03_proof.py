"""P03 A — ausência de prova de quantidade NÃO é execução zero.

`P03_PROOF_TEST_SOCKET` aponta para /tmp/cw-p03prf-sock.* criado pelo runner.
Roda o CICLO REAL (`reconcile_due`/`recover_entry_intents`) sobre PostgreSQL 16
descartável UTF-8, com exchange FALSA que só responde consultas. Sem TCP/DNS,
sem ordem, sem produção.

Defeito reproduzido: um incidente `CLEANUP_PENDING` (montado pelo
`assemble_entry_incident(closed=True)`, com client id e SL exatos e SEM
`executed_qty`) resolve FLAT depois do grace — fresh-flat mais listagem vazia.
O agregador tratava `min_known_fill=None` como `0.0` e a liquidação gravava
`RECONCILED_NO_EXECUTION` sem NUNCA consultar a ordem de entrada. FLAT prova a
posição de agora; lower-bound zero e campo ausente não provam quantidade final.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("P03_PROOF_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-p03prf-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://p03prf@/p03prfdb?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-prova")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-prova")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste P03-prova")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
MUTACOES: list = []
CONSULTAS_ENTRY: list = []
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

    tabelas = [RecommendationSnapshot.__table__, RealTrade.__table__,
               EntryIntent.__table__, ExecutionIncident.__table__, RiskState.__table__]
    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tabelas)
    await db.init_db()

    ORDENS: dict = {}
    POSICOES: dict = {"positions": []}
    ALGOS: dict = {"orders": []}

    async def fake_get_order(symbol, order_id=None, client_order_id=None, **kwargs):
        CONSULTAS_ENTRY.append(client_order_id or order_id)
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

    async def intencao_unknown(trigger: int, *, ids=None, entry=100.0, stop=95.0, qty=1.0):
        ident = identidade(trigger)
        reserva = await intents.reserve(
            db.get_session, ident,
            {"entry": entry, "stop_loss": stop, "tp1": entry + 5, "tp2": entry + 10,
             "leverage": 3},
            owner="w1", decision={"entry": entry, "stop_loss": stop, "qty": qty})
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

    async def incidentes_de(coid):
        async with db.get_session() as session:
            return (await session.execute(
                select(ExecutionIncident.kind, ExecutionIncident.state,
                       ExecutionIncident.resolved_at, ExecutionIncident.min_known_fill,
                       ExecutionIncident.payload)
                .where(ExecutionIncident.client_order_id == coid)
                .order_by(ExecutionIncident.id))).all()

    async def pendentes():
        async with db.get_session() as session:
            return int((await session.execute(
                select(func.count(EntryIntent.intent_key))
                .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN"))))).scalar() or 0)

    async def pausado():
        async with db.get_session() as session:
            row = (await session.execute(select(RiskState))).scalars().first()
            return bool(row and row.trading_paused)

    async def adiantar_retries():
        async with db.get_session() as session:
            await session.execute(update(ExecutionIncident)
                                  .where(ExecutionIncident.resolved_at.is_(None))
                                  .values(next_retry_at=datetime.now(timezone.utc)
                                          - timedelta(minutes=5)))
            await session.commit()

    def incidente_de_cleanup(ident, *, qty=1.0):
        """Incidente REAL do executor: fechamento com condicionais possivelmente
        órfãs (`closed=True`), com client id e SL exatos, SEM `executed_qty`."""
        return ers.assemble_entry_incident(
            {"client_order_id": ident.client_order_id, "was_maker": False,
             "safety_state": "ENTRY_SUBMISSION_UNKNOWN", "submitted_qty": qty,
             "sl_order_id": f"{ident.client_order_id}-sl",
             "entry_order_terminal": True},
            {"symbol": SIMBOLO, "direction": "long", "stop_loss": 95.0, "qty": qty},
            closed=True, local_client_order_id=ident.client_order_id)

    agora = datetime.now(timezone.utc)

    # ═══════════════════════════════════════════════════════════════════════
    #  A1. Cleanup resolve FLAT sem consultar a entry ⇒ NÃO é "sem execução"
    # ═══════════════════════════════════════════════════════════════════════
    # Caminho REALISTA do defeito: enquanto o LEASE está vivo a intenção segue
    # em SENDING e a recuperação não a apresenta — os ciclos resolvem o cleanup
    # sem que nenhum incidente de ENTRY seja aberto. Só depois o lease expira.
    ident_a = identidade(1_760_000_100_000)
    reserva_a = await intents.reserve(
        db.get_session, ident_a,
        {"entry": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0, "leverage": 3},
        owner="w1", decision={"entry": 100.0, "stop_loss": 95.0, "qty": 1.0},
        lease_seconds=3600)
    assert reserva_a.granted, reserva_a
    assert await intents.mark_sending(db.get_session, ident_a.intent_key, owner="w1",
                                      lease_seconds=3600)
    await intents.register_dispatch(db.get_session, ident_a.intent_key, owner="w1",
                                    dispatch_id=ident_a.client_order_id)
    kwargs_a = incidente_de_cleanup(ident_a)
    check("executor_monta_cleanup_sem_qty",
          kwargs_a["kind"] == ers.Kind.CLEANUP_PENDING
          and kwargs_a["min_known_fill"] is None, str(kwargs_a)[:200])
    await ers.record_incident(**kwargs_a)
    # A ordem FILLED EXISTE na exchange; o cleanup só não a consulta.
    ORDENS[ident_a.client_order_id] = {"ok": True, "status": "FILLED",
                                       "executed_qty": 1.0, "orig_qty": 1.0,
                                       "avg_price": 100.0,
                                       "raw": {"executedQty": "1", "avgPrice": "100"}}
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=1.0,
                          entry_price=100.0, planned_stop=95.0, status="closed_tp1",
                          source="auto", client_order_id=ident_a.client_order_id,
                          pnl_usd=3.1, opened_at=agora - timedelta(hours=2),
                          closed_at=agora - timedelta(hours=1))
        session.add(trade)
        await session.commit()
        trade_a = trade.id
    # Dois ciclos reais com fresh-flat e listagem vazia cumprem o grace.
    for _ in range(3):
        await adiantar_retries()
        await ers.reconcile_due()
    linhas_a = await incidentes_de(ident_a.client_order_id)
    cleanup_a = [linha for linha in linhas_a if linha[0] == ers.Kind.CLEANUP_PENDING][0]
    check("cleanup_resolve_flat_pelo_grace",
          cleanup_a[1] == ers.State.FLAT and cleanup_a[2] is not None, str(cleanup_a))
    check("cleanup_nao_tem_prova_de_qty", cleanup_a[3] is None, str(cleanup_a[3]))
    # Lease expira: a intenção vai a UNKNOWN e a liquidação roda tendo como
    # ÚNICA prova resolvida o FLAT do cleanup.
    async with db.get_session() as session:
        await session.execute(update(EntryIntent)
                              .where(EntryIntent.intent_key == ident_a.intent_key)
                              .values(lease_expires_at=datetime.now(timezone.utc)
                                      - timedelta(minutes=5)))
        await session.commit()
    # A LIQUIDAÇÃO não pode transformar essa ausência em "não executou".
    await ers.recover_entry_intents()
    estado_a = await estado(ident_a.intent_key)
    check("flat_sem_prova_nao_vira_sem_execucao",
          estado_a[2] != "RECONCILED_NO_EXECUTION", str(estado_a))
    check("sem_prova_a_intencao_segue_pendente",
          estado_a[0] == "UNKNOWN", str(estado_a))
    check("slot_continua_ocupado", await pendentes() >= 1, str(await pendentes()))
    check("quarentena_mantida", await pausado(), "pausa caiu sem prova")

    # O caminho OFICIAL consulta a identidade exata e acha o fill.
    consultas_antes = len(CONSULTAS_ENTRY)
    for _ in range(2):
        await adiantar_retries()
        await ers.reconcile_due()
        await ers.recover_entry_intents()
    check("caminho_oficial_consultou_a_entry",
          ident_a.client_order_id in CONSULTAS_ENTRY[consultas_antes:],
          str(CONSULTAS_ENTRY[consultas_antes:]))
    estado_a = await estado(ident_a.intent_key)
    check("fill_encontrado_vincula_o_trade",
          estado_a[0] == "CONFIRMED" and estado_a[1] == trade_a, str(estado_a))

    # ═══════════════════════════════════════════════════════════════════════
    #  A2. Zero TERMINAL comprovado encerra como sem execução
    # ═══════════════════════════════════════════════════════════════════════
    ident_b = await intencao_unknown(1_760_000_200_000)
    ORDENS[ident_b.client_order_id] = {"ok": True, "status": "REJECTED",
                                       "executed_qty": 0.0, "orig_qty": 1.0,
                                       "raw": {"executedQty": "0"}}
    await adiantar_retries()
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    estado_b = await estado(ident_b.intent_key)
    check("zero_terminal_comprovado_encerra",
          estado_b[0] == "TERMINAL" and estado_b[2] == "RECONCILED_NO_EXECUTION",
          str(estado_b))
    linhas_b = await incidentes_de(ident_b.client_order_id)
    provas_b = [(linha[4] or {}).get("entry_proof") for linha in linhas_b]
    check("prova_terminal_fica_registrada",
          any(isinstance(p, dict) and p.get("state") == "TERMINAL_ZERO"
              and p.get("client_order_id") == ident_b.client_order_id for p in provas_b),
          str(provas_b))

    # ═══════════════════════════════════════════════════════════════════════
    #  A3. Consulta indisponível (None) não é zero
    # ═══════════════════════════════════════════════════════════════════════
    ident_c = await intencao_unknown(1_760_000_300_000)
    ORDENS[ident_c.client_order_id] = {"ok": False, "error": "timeout"}
    await adiantar_retries()
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    check("consulta_indisponivel_nao_encerra",
          (await estado(ident_c.intent_key))[0] == "UNKNOWN",
          str(await estado(ident_c.intent_key)))

    # ═══════════════════════════════════════════════════════════════════════
    #  A4. Primária zero comprovada + filha desconhecida ⇒ nada encerra
    # ═══════════════════════════════════════════════════════════════════════
    coid_d = identidade(1_760_000_400_000).client_order_id
    filha_d = f"{coid_d[:32]}-mfb"[:36]
    ident_d = await intencao_unknown(1_760_000_400_000, ids=[coid_d, filha_d])
    ORDENS[coid_d] = {"ok": True, "status": "REJECTED", "executed_qty": 0.0,
                      "raw": {"executedQty": "0"}}
    ORDENS[filha_d] = {"ok": False, "error": "consulta indisponível"}
    for _ in range(2):
        await adiantar_retries()
        await ers.recover_entry_intents()
        await ers.reconcile_due()
    check("filha_desconhecida_impede_encerramento",
          (await estado(ident_d.intent_key))[0] == "UNKNOWN",
          str(await estado(ident_d.intent_key)))
    # Com a filha comprovadamente zero, aí sim encerra.
    ORDENS[filha_d] = {"ok": True, "status": "EXPIRED", "executed_qty": 0.0,
                       "raw": {"executedQty": "0"}}
    for _ in range(2):
        await adiantar_retries()
        await ers.reconcile_due()
        await ers.recover_entry_intents()
    check("as_duas_pernas_zero_encerram",
          (await estado(ident_d.intent_key))[0] == "TERMINAL",
          str(await estado(ident_d.intent_key)))

    # ═══════════════════════════════════════════════════════════════════════
    #  A5. Prova CONTRADITÓRIA não libera nada
    # ═══════════════════════════════════════════════════════════════════════
    coid_e = identidade(1_760_000_500_000).client_order_id
    filha_e = f"{coid_e[:32]}-mfb"[:36]
    ident_e = await intencao_unknown(1_760_000_500_000, ids=[coid_e, filha_e],
                                     entry=50.0, stop=45.0, qty=2.0)
    ORDENS[coid_e] = {"ok": True, "status": "REJECTED", "executed_qty": 0.0,
                      "raw": {"executedQty": "0"}}
    ORDENS[filha_e] = {"ok": True, "status": "FILLED", "executed_qty": 2.0,
                       "orig_qty": 2.0, "avg_price": 50.0,
                       "raw": {"executedQty": "2", "avgPrice": "50"}}
    for _ in range(2):
        await adiantar_retries()
        await ers.recover_entry_intents()
        await ers.reconcile_due()
    estado_e = await estado(ident_e.intent_key)
    check("prova_positiva_conflitante_nao_encerra_como_zero",
          estado_e[2] != "RECONCILED_NO_EXECUTION" and estado_e[0] != "TERMINAL",
          str(estado_e))
    check("conflito_mantem_slot", await pendentes() >= 1, str(await pendentes()))

    # ═══════════════════════════════════════════════════════════════════════
    #  A6. Restart/repetição não apagam a prova nem inventam zero
    # ═══════════════════════════════════════════════════════════════════════
    await db._engine.dispose()
    for _ in range(3):
        await adiantar_retries()
        await ers.reconcile_due()
        await ers.recover_entry_intents()
    check("confirmada_permanece_confirmada",
          (await estado(ident_a.intent_key))[:2] == ("CONFIRMED", trade_a),
          str(await estado(ident_a.intent_key)))
    check("terminal_permanece_terminal",
          (await estado(ident_b.intent_key))[0] == "TERMINAL",
          str(await estado(ident_b.intent_key)))
    check("incerta_permanece_incerta",
          (await estado(ident_c.intent_key))[0] == "UNKNOWN",
          str(await estado(ident_c.intent_key)))
    check("zero_mutacao_na_exchange", MUTACOES == [], str(MUTACOES))

    await db._engine.dispose()
    print(f"P03_PROOF_PG_OK: {len(CHECKS)} verificações — ciclo real, prova "
          "terminal por dispatch, exchange falsa")


if __name__ == "__main__":
    asyncio.run(run())
