"""P03 A — conflito TERMINAL do MESMO dispatch não pode ser descartado.

`P03_CONFLICT_TEST_SOCKET` aponta para /tmp/cw-p03cfl-sock.* criado pelo runner.
Ciclo REAL (`reconcile_due`/`recover_entry_intents`) sobre PostgreSQL 16
descartável UTF-8, exchange FALSA que só responde consultas. Sem TCP/DNS.

Defeito reproduzido: para a MESMA ordem (mesmo client id), a exchange respondeu
`CANCELED` com quantidade final 1 (positiva, lower-bound 1) e, na consulta
seguinte, `CANCELED` com quantidade final 0. A contradição era DESCARTADA para
preservar a prova positiva, o incidente resolvia FLAT e, havendo RealTrade, a
liquidação encerrava a intenção como CONFIRMED/RECONCILED_EXECUTION.

Preservar a positiva está certo; apagar a contradição e seguir resolvendo, não.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("P03_CONFLICT_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-p03cfl-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://p03cfl@/p03cfldb?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-conflito")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste P03-conflito")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste P03-conflito")


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

    tabelas = [RecommendationSnapshot.__table__, RealTrade.__table__,
               EntryIntent.__table__, ExecutionIncident.__table__, RiskState.__table__]
    for _ in range(2):
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tabelas)
    await db.init_db()

    ORDENS: dict = {}
    POSICOES: dict = {"positions": []}
    ALGOS: dict = {"orders": []}

    async def fake_get_order(symbol, order_id=None, client_order_id=None, **kwargs):
        resposta = ORDENS.get(client_order_id)
        return resposta if resposta is not None else {"ok": False, "error": "sem resposta"}

    async def fake_get_positions(symbol=None, force=False, **kwargs):
        if POSICOES.get("stale"):
            return {"ok": False, "error": "leitura stale"}   # ⇒ posição UNKNOWN
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
                       ExecutionIncident.manual_reason, ExecutionIncident.payload)
                .where(ExecutionIncident.client_order_id == coid)
                .order_by(ExecutionIncident.id))).all()

    async def pendentes():
        async with db.get_session() as session:
            return int((await session.execute(
                select(func.count(EntryIntent.intent_key))
                .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN"))))).scalar() or 0)

    async def reservado():
        async with db.get_session() as session:
            return float((await session.execute(
                select(func.coalesce(func.sum(EntryIntent.reserved_risk_usd), 0.0))
                .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN")),
                       EntryIntent.real_trade_id.is_(None)))).scalar() or 0.0)

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

    def cancelada(qty):
        """Resposta TERMINAL `CANCELED` com quantidade final explícita."""
        return {"ok": True, "status": "CANCELED", "executed_qty": float(qty),
                "orig_qty": 1.0, "avg_price": 100.0,
                "raw": {"executedQty": str(qty), "avgPrice": "100"}}

    def proof_de(linhas, coid):
        for linha in linhas:
            payload = linha[5] if isinstance(linha[5], dict) else {}
            proof = payload.get("entry_proof")
            if isinstance(proof, dict) and proof.get("client_order_id") == coid:
                return proof, payload
        return None, {}

    agora = datetime.now(timezone.utc)

    # ═══════════════════════════════════════════════════════════════════════
    #  A1. POSITIVA → ZERO na MESMA ordem, COM RealTrade
    # ═══════════════════════════════════════════════════════════════════════
    ident_a = await intencao_unknown(1_760_000_100_000)
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=1.0,
                          entry_price=100.0, planned_stop=95.0, status="open",
                          source="auto", client_order_id=ident_a.client_order_id,
                          opened_at=agora)
        session.add(trade)
        await session.commit()
    # 1ª consulta: terminal com qty 1 (positiva); posição UNKNOWN.
    ORDENS[ident_a.client_order_id] = cancelada(1.0)
    POSICOES.clear()
    POSICOES["stale"] = True              # leitura de posição UNKNOWN
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    linhas = await incidentes_de(ident_a.client_order_id)
    proof, _ = proof_de(linhas, ident_a.client_order_id)
    check("primeira_consulta_persiste_positiva",
          proof is not None and proof.get("state") == "POSITIVE", str(proof))
    check("lower_bound_gravado", any(linha[3] == 1.0 for linha in linhas), str(linhas))

    # 2ª consulta: MESMA ordem, agora terminal com qty 0 — CONTRADIÇÃO.
    ORDENS[ident_a.client_order_id] = cancelada(0.0)
    POSICOES.clear()
    POSICOES["positions"] = []            # fresh-flat
    await adiantar_retries()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    linhas = await incidentes_de(ident_a.client_order_id)
    proof, payload = proof_de(linhas, ident_a.client_order_id)
    check("positiva_nao_e_substituida_por_zero",
          proof is not None and proof.get("state") == "POSITIVE", str(proof))
    conflito = payload.get("entry_proof_conflict")
    check("conflito_persistido_com_as_duas_observacoes",
          isinstance(conflito, dict)
          and conflito.get("client_order_id") == ident_a.client_order_id
          and conflito.get("observed_state") == "TERMINAL_ZERO"
          and conflito.get("kept_state") == "POSITIVE", str(conflito))
    check("incidente_do_conflito_nao_resolve",
          any(linha[2] is None for linha in linhas), str(linhas))
    check("incidente_do_conflito_e_manual",
          any(linha[1] == ers.State.MANUAL_REQUIRED and linha[4] for linha in linhas),
          str(linhas))
    estado_a = await estado(ident_a.intent_key)
    check("conflito_nao_liquida_a_intencao",
          estado_a[0] == "UNKNOWN" and estado_a[1] is None, str(estado_a))
    check("conflito_mantem_slot_e_reserva",
          await pendentes() >= 1 and await reservado() >= 0.0,
          f"{await pendentes()} / {await reservado()}")
    check("conflito_mantem_pausa", await pausado(), "quarentena caiu com conflito")
    check("conflito_nao_cancela_protecao", MUTACOES == [], str(MUTACOES))

    # Repetição/restart: idempotente, sem histórico sem limite nem reversão.
    await db._engine.dispose()
    for _ in range(3):
        await adiantar_retries()
        await ers.reconcile_due()
        await ers.recover_entry_intents()
    linhas_depois = await incidentes_de(ident_a.client_order_id)
    proof_depois, payload_depois = proof_de(linhas_depois, ident_a.client_order_id)
    check("restart_preserva_positiva_e_conflito",
          proof_depois.get("state") == "POSITIVE"
          and isinstance(payload_depois.get("entry_proof_conflict"), dict),
          str(payload_depois)[:200])
    check("repeticao_nao_multiplica_incidentes",
          len(linhas_depois) == len(linhas), f"{len(linhas)} → {len(linhas_depois)}")
    check("intencao_continua_pendente_apos_restart",
          (await estado(ident_a.intent_key))[0] == "UNKNOWN",
          str(await estado(ident_a.intent_key)))

    # ═══════════════════════════════════════════════════════════════════════
    #  A2. ZERO → POSITIVA na MESMA ordem, SEM RealTrade
    # ═══════════════════════════════════════════════════════════════════════
    ident_b = await intencao_unknown(1_760_000_200_000, entry=50.0, stop=45.0, qty=2.0)
    # Incidente do EXECUTOR com condicional exata (SL): o zero terminal é
    # provado e o desfecho passa pelo cleanup (grace), então a observação fica
    # persistida ANTES de qualquer liquidação — é aí que a ordem inversa cabe.
    await ers.record_incident(**ers.assemble_entry_incident(
        {"client_order_id": ident_b.client_order_id, "was_maker": False,
         "safety_state": "ENTRY_SUBMISSION_UNKNOWN", "submitted_qty": 2.0,
         "sl_order_id": f"{ident_b.client_order_id}-sl",
         "entry_order_terminal": True},
        {"symbol": SIMBOLO, "direction": "long", "stop_loss": 45.0, "qty": 2.0},
        local_client_order_id=ident_b.client_order_id))
    ORDENS[ident_b.client_order_id] = cancelada(0.0)
    POSICOES.clear()
    POSICOES["positions"] = []
    await adiantar_retries()
    await ers.recover_entry_intents()
    await ers.reconcile_due()
    linhas_b = await incidentes_de(ident_b.client_order_id)
    proof_b, _ = proof_de(linhas_b, ident_b.client_order_id)
    check("zero_terminal_persistido_primeiro",
          proof_b is not None and proof_b.get("state") == "TERMINAL_ZERO", str(proof_b))
    # A ordem inversa: a MESMA ordem agora aparece com fill.
    ORDENS[ident_b.client_order_id] = cancelada(2.0)
    await adiantar_retries()
    await ers.reconcile_due()
    await ers.recover_entry_intents()
    linhas_b = await incidentes_de(ident_b.client_order_id)
    proof_b, payload_b = proof_de(linhas_b, ident_b.client_order_id)
    conflito_b = payload_b.get("entry_proof_conflict")
    check("inversa_registra_conflito",
          isinstance(conflito_b, dict)
          and {conflito_b.get("kept_state"), conflito_b.get("observed_state")}
          == {"POSITIVE", "TERMINAL_ZERO"}, str(conflito_b))
    estado_b = await estado(ident_b.intent_key)
    check("inversa_nao_encerra_sem_execucao",
          estado_b[2] != "RECONCILED_NO_EXECUTION" and estado_b[0] != "TERMINAL",
          str(estado_b))
    check("inversa_sem_realtrade_nao_confirma",
          estado_b[1] is None, str(estado_b))

    # ═══════════════════════════════════════════════════════════════════════
    #  A3. Primária ZERO + filha POSITIVA (ordens DIFERENTES) reconcilia
    # ═══════════════════════════════════════════════════════════════════════
    coid_c = identidade(1_760_000_300_000).client_order_id
    filha_c = f"{coid_c[:32]}-mfb"[:36]
    ident_c = await intencao_unknown(1_760_000_300_000, ids=[coid_c, filha_c],
                                     entry=200.0, stop=190.0, qty=2.0)
    async with db.get_session() as session:
        trade = RealTrade(symbol=SIMBOLO, exchange="binance", side="long", qty=2.0,
                          entry_price=200.0, planned_stop=190.0, status="open",
                          source="auto", client_order_id=filha_c, opened_at=agora)
        session.add(trade)
        await session.commit()
        trade_c = trade.id
    ORDENS[coid_c] = {"ok": True, "status": "REJECTED", "executed_qty": 0.0,
                      "raw": {"executedQty": "0"}}
    ORDENS[filha_c] = {"ok": True, "status": "FILLED", "executed_qty": 2.0,
                       "orig_qty": 2.0, "avg_price": 200.0,
                       "raw": {"executedQty": "2", "avgPrice": "200"}}
    POSICOES.clear()
    POSICOES["positions"] = [{"symbol": "ALFAUSDT", "side": "buy", "size": 2.0}]
    ALGOS["orders"] = [{"algo_id": "SL-C", "symbol": "ALFAUSDT", "side": "SELL",
                        "type": "STOP_MARKET", "status": "NEW", "close_position": True,
                        "trigger_price": 190.0, "quantity": 2.0}]
    for _ in range(3):
        await adiantar_retries()
        await ers.recover_entry_intents()
        await ers.reconcile_due()
    estado_c = await estado(ident_c.intent_key)
    check("primaria_zero_com_filha_positiva_reconcilia",
          estado_c[0] == "CONFIRMED" and estado_c[1] == trade_c, str(estado_c))
    POSICOES["positions"] = []
    ALGOS["orders"] = []

    # ═══════════════════════════════════════════════════════════════════════
    #  A4. Casos já verdes continuam verdes
    # ═══════════════════════════════════════════════════════════════════════
    ident_d = await intencao_unknown(1_760_000_400_000)
    ORDENS[ident_d.client_order_id] = {"ok": True, "status": "REJECTED",
                                       "executed_qty": 0.0,
                                       "raw": {"executedQty": "0"}}
    for _ in range(2):
        await adiantar_retries()
        await ers.recover_entry_intents()
        await ers.reconcile_due()
    check("zero_terminal_sem_conflito_encerra",
          (await estado(ident_d.intent_key))[0] == "TERMINAL",
          str(await estado(ident_d.intent_key)))
    ident_e = await intencao_unknown(1_760_000_500_000)
    ORDENS[ident_e.client_order_id] = {"ok": False, "error": "timeout"}
    for _ in range(2):
        await adiantar_retries()
        await ers.recover_entry_intents()
        await ers.reconcile_due()
    check("consulta_indisponivel_segue_incerta",
          (await estado(ident_e.intent_key))[0] == "UNKNOWN",
          str(await estado(ident_e.intent_key)))
    check("zero_mutacao_na_exchange", MUTACOES == [], str(MUTACOES))

    await db._engine.dispose()
    print(f"P03_CONFLICT_PG_OK: {len(CHECKS)} verificações — conflito terminal "
          "persistido e bloqueado pelo fluxo oficial")


if __name__ == "__main__":
    asyncio.run(run())
