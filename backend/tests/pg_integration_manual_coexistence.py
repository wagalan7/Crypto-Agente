"""Convivência manual/bot na MESMA conta — aceites pelos fluxos persistidos.

`MANUALBOT_TEST_SOCKET` aponta para /tmp/cw-mbot-sock.* criado pelo runner.
PostgreSQL 16 descartável UTF-8, driver async real, socket Unix, TCP/DNS
bloqueados. A EXCHANGE é falsa (só responde consultas e registra mutações); a
lógica exercitada é a REAL: `manual_position_service`, `entry_intent_service`,
`execution_reconciliation_service` e o transporte `binance_signed_service`.

Defeitos reproduzidos ANTES da correção (ver `docs/MANUAL_BOT_COEXISTENCE.md`):

1. `get_positions` descartava `positionSide`/`updateTime` — sem eles não existe
   identidade verificável de posição, então nenhum reconhecimento poderia ser
   revalidado depois.
2. O trade manager escolhia a PRIMEIRA posição do símbolo, sem conferir lado —
   uma posição manual (ou a perna oposta) seria administrada como se fosse do bot.
3. Não havia gate de margem REAL: duas admissões concorrentes podiam reservar o
   MESMO saldo livre da conta compartilhada.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("MANUALBOT_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-mbot-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://mbot@/mbotdb?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste manual/bot")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste manual/bot")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste manual/bot")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ALFA = "ALFA/USDT:USDT"          # posição MANUAL do operador
BETA = "BETA/USDT:USDT"          # símbolo livre para o bot
ALFA_EX = "ALFAUSDT"
BETA_EX = "BETAUSDT"


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def posicao(symbol=ALFA_EX, *, side="Buy", size="2.5", entry="100",
            position_side="BOTH", update_time=1_770_000_000_000, mark="101"):
    """Linha CRUA no formato que `get_positions` normaliza."""
    return {"symbol": symbol, "side": side, "size": float(size),
            "entry_price": float(entry), "mark_price": float(mark),
            "unrealized_pnl": 0.0, "leverage": 5.0, "position_value": 250.0,
            "take_profit": None, "stop_loss": None,
            "position_side": position_side, "update_time_ms": update_time}


async def run():
    from unittest.mock import patch
    from sqlalchemy import func, select, text, update as sql_update
    import db
    from models.entry_intent import EntryIntent
    from models.execution_incident import ExecutionIncident
    from models.manual_position_ack import (STATE_ACTIVE, STATE_CLOSED,
                                            STATE_INVALIDATED)
    from models.manual_position_ack import ManualPositionAcknowledgement as Ack
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from models.risk_state import RiskState
    from services import binance_signed_service as bss
    from services import entry_intent_service as intents
    from services import execution_reconciliation_service as ers
    from services import manual_position_service as mps

    tabelas = [RecommendationSnapshot.__table__, RealTrade.__table__,
               EntryIntent.__table__, ExecutionIncident.__table__,
               RiskState.__table__, Ack.__table__]
    for _ in range(2):
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tabelas)
    # Migração ADITIVA e IDEMPOTENTE pelo caminho oficial, duas vezes.
    await db.init_db()
    await db.init_db()
    async with db.get_session() as session:
        colunas = {linha[0] for linha in (await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'entry_intents'"))).all()}
        indices = {linha[0] for linha in (await session.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'manual_position_acks'"))).all()}
    check("migracao_idempotente_adiciona_margem_reservada",
          "reserved_margin_usd" in colunas, str(sorted(colunas))[:160])
    check("indice_unico_cobre_os_estados_bloqueantes",
          "uq_manual_ack_open" in indices
          and "uq_manual_ack_active" not in indices, str(sorted(indices)))

    # ── Exchange FALSA ────────────────────────────────────────────────────
    EXCHANGE = {"positions": [posicao()], "quality": "ok", "algos": [],
                "algos_ok": True, "common": [], "common_ok": True}
    MUTACOES: list = []

    async def fake_get_positions(symbol=None, force=False, **kwargs):
        if EXCHANGE["quality"] == "stale":
            return {"ok": True, "stale": True, "positions": EXCHANGE["positions"]}
        if EXCHANGE["quality"] == "rate_limited":
            return {"ok": True, "rate_limited": True, "positions": []}
        if EXCHANGE["quality"] == "erro":
            return {"ok": False, "error": "indisponível"}
        linhas = EXCHANGE["positions"]
        if symbol:
            alvo = mps.symbol_key(symbol)
            linhas = [p for p in linhas if mps.symbol_key(p.get("symbol")) == alvo]
        return {"ok": True, "positions": linhas, "count": len(linhas)}

    async def fake_algo_orders(symbol=None, **kwargs):
        if not EXCHANGE["algos_ok"]:
            return {"ok": False, "error": "listagem indisponível"}
        linhas = EXCHANGE["algos"]
        if symbol:
            alvo = mps.symbol_key(symbol)
            linhas = [o for o in linhas if mps.symbol_key(o.get("symbol")) == alvo]
        return {"ok": True, "orders": linhas, "count": len(linhas)}

    async def fake_open_orders(symbol=None, **kwargs):
        """Ordens COMUNS — segunda fonte obrigatória da prova de ausência."""
        if not EXCHANGE.get("common_ok", True):
            return {"ok": False, "error": "openOrders indisponível"}
        linhas = EXCHANGE.get("common", [])
        if symbol:
            alvo = mps.symbol_key(symbol)
            linhas = [o for o in linhas if mps.symbol_key(o.get("symbol")) == alvo]
        return {"ok": True, "orders": linhas, "count": len(linhas)}

    async def fake_signed_request(method, path, params=None, **kwargs):
        MUTACOES.append((method, path, dict(params or {})))
        return {"ok": True, "result": {}}

    async def fake_round_qty(sym, qty):
        return float(qty)

    async def fake_filters(sym):
        return {"step": 0.001, "min_qty": 0.001, "max_qty": 1e9, "min_notional": 5.0}

    from services import exchange_service as exs
    bss._API_KEY, bss._API_SECRET = "sintetica-key", "sintetica-secret"
    patches = [patch.object(exs, "get_positions", fake_get_positions),
               patch.object(bss, "get_positions", fake_get_positions),
               patch.object(bss, "get_open_algo_orders", fake_algo_orders),
               patch.object(bss, "get_open_orders", fake_open_orders),
               patch.object(bss, "_signed_request", fake_signed_request),
               patch.object(bss, "_round_qty", fake_round_qty),
               patch.object(bss, "_get_symbol_filters", fake_filters)]
    for item in patches:
        item.start()

    ers.set_repo(None)
    ers._boot_scan_safe = True
    escopo = bss.accounting_scope()
    check("conta_opaca_disponivel", isinstance(escopo, str) and len(escopo) == 64,
          str(escopo)[:20])

    async def acks(state=None):
        async with db.get_session() as session:
            stmt = select(Ack).order_by(Ack.id)
            if state:
                stmt = stmt.where(Ack.state == state)
            return (await session.execute(stmt)).scalars().all()

    async def incidentes(symbol=None):
        async with db.get_session() as session:
            stmt = select(ExecutionIncident).order_by(ExecutionIncident.id)
            if symbol:
                stmt = stmt.where(ExecutionIncident.symbol == symbol)
            return (await session.execute(stmt)).scalars().all()

    async def pausado():
        async with db.get_session() as session:
            linha = (await session.execute(select(RiskState))).scalars().first()
            return bool(linha and linha.trading_paused)

    async def candidato_de(symbol=ALFA):
        lista = await mps.list_candidates()
        for item in lista.get("candidates") or []:
            if mps.symbol_key(item.get("symbol")) == mps.symbol_key(symbol):
                return item
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  1. Identidade observável — o RED e a correção
    # ══════════════════════════════════════════════════════════════════════
    leitura = await mps.observe_positions()
    check("leitura_fresca_traz_perna_e_versao_temporal",
          leitura["ok"] and leitura["positions"][0]["position_side"] == "BOTH"
          and leitura["positions"][0]["update_time_ms"] == 1_770_000_000_000,
          str(leitura)[:200])
    candidato = await candidato_de()
    check("candidato_expoe_fingerprint_sem_segredo",
          candidato and candidato["eligible"] and candidato["fingerprint"]
          and escopo not in str(candidato), str(candidato)[:200])
    impressao = candidato["fingerprint"]

    # Campo ausente NÃO vira BOTH nem relógio local: identidade incompleta.
    EXCHANGE["positions"] = [posicao(position_side=None, update_time=None)]
    incompleto = await candidato_de()
    check("identidade_incompleta_nao_e_elegivel",
          incompleto and incompleto["eligible"] is False
          and incompleto["fingerprint"] is None, str(incompleto)[:200])
    EXCHANGE["positions"] = [posicao()]

    # Mark price sozinho NÃO muda a identidade.
    outra_marca = mps.position_fingerprint(
        account_scope=escopo, exchange="binance", market="usdm_futures",
        symbol=ALFA, side="Buy", position_side="BOTH", qty=Decimal("2.5"),
        entry_price=Decimal("100"), update_time_ms=1_770_000_000_000,
        contract_version=mps._contract_version())
    check("mark_price_nao_entra_na_identidade", outra_marca == impressao,
          f"{outra_marca} != {impressao}")
    # Decimais canônicos: 2.50 e 2.5 são a MESMA posição.
    canonico = mps.position_fingerprint(
        account_scope=escopo, exchange="binance", market="usdm_futures",
        symbol="ALFAUSDT", side="buy", position_side="BOTH", qty="2.50",
        entry_price="100.0", update_time_ms=1_770_000_000_000,
        contract_version=mps._contract_version())
    check("decimais_canonicos_nao_mudam_identidade", canonico == impressao)
    for nome, kwargs in (("bool", {"qty": True}), ("nan", {"qty": float("nan")}),
                         ("infinito", {"entry_price": float("inf")}),
                         ("qty_zero", {"qty": 0}),
                         ("lado_ausente", {"side": None})):
        base = dict(account_scope=escopo, exchange="binance", market="usdm_futures",
                    symbol=ALFA, side="Buy", position_side="BOTH", qty="2.5",
                    entry_price="100", update_time_ms=1_770_000_000_000,
                    contract_version=mps._contract_version())
        base.update(kwargs)
        check(f"geometria_invalida_recusada_{nome}",
              mps.position_fingerprint(**base) is None, nome)

    # ══════════════════════════════════════════════════════════════════════
    #  2. Sem reconhecimento: manual aparente continua UNTRACKED e bloqueia
    # ══════════════════════════════════════════════════════════════════════
    scan = await ers._detect_untracked_positions()
    check("sem_reconhecimento_a_manual_vira_untracked",
          scan["status"] == "UNTRACKED" and scan["count"] == 1, str(scan))
    linhas = await incidentes(ALFA)
    check("incidente_untracked_persistido",
          len(linhas) == 1 and linhas[0].kind == "UNTRACKED_POSITION"
          and linhas[0].resolved_at is None, str([(l.kind, l.state) for l in linhas]))
    check("pausa_p03_persistida_com_a_causa", await pausado())
    chave_untracked = linhas[0].incident_key

    guard_alfa = await mps.ownership_guard(ALFA, action="entry")
    guard_beta = await mps.ownership_guard(BETA, action="entry")
    check("sem_reconhecimento_guard_nao_bloqueia_ninguem",
          guard_alfa["allowed"] and guard_beta["allowed"],
          f"{guard_alfa} {guard_beta}")

    # ══════════════════════════════════════════════════════════════════════
    #  3. Confirmação: recusas antes de qualquer gravação
    # ══════════════════════════════════════════════════════════════════════
    nao_literal = []
    for valor in (True,):
        pass
    for valor in ("true", 1, "1", "on", None):
        res = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao,
                                    confirm=valor)
        nao_literal.append(res.get("reason_code"))
    check("confirmacao_precisa_ser_booleano_literal",
          set(nao_literal) == {mps.ACK_CONFIRM_NOT_LITERAL}, str(nao_literal))
    velho = await mps.acknowledge(symbol=ALFA, expected_fingerprint="0" * 64,
                                  confirm=True)
    check("fingerprint_obsoleto_recusa",
          velho["reason_code"] == mps.ACK_FINGERPRINT_MISMATCH
          and velho.get("current_fingerprint") == impressao, str(velho)[:200])
    for qualidade, codigo in (("stale", mps.ACK_POSITION_UNKNOWN),
                              ("rate_limited", mps.ACK_POSITION_UNKNOWN),
                              ("erro", mps.ACK_POSITION_UNKNOWN)):
        EXCHANGE["quality"] = qualidade
        res = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao,
                                    confirm=True)
        check(f"leitura_{qualidade}_nao_autoriza", res["reason_code"] == codigo,
              str(res)[:160])
    EXCHANGE["quality"] = "ok"
    EXCHANGE["algos_ok"] = False
    res = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao, confirm=True)
    check("listagem_de_ordens_indisponivel_bloqueia",
          res["reason_code"] == mps.ACK_ORDERS_UNKNOWN, str(res)[:160])
    EXCHANGE["algos_ok"] = True
    check("nenhuma_gravacao_nas_recusas", len(await acks()) == 0)

    # Rastro do BOT no símbolo impede reconhecer.
    ident_alfa = intents.EntryIdentity(
        account_ref=escopo, exchange="binance", symbol="ALFA-USDT-USDT", quote="USDT",
        side="long", position_side="BOTH", timeframe="4h", playbook="CHAMPION_LEGACY",
        playbook_version="SCORE_V2", purpose="ENTRY", trigger_candle_ms=1_770_000_000_000)
    reserva = await intents.reserve(db.get_session, ident_alfa,
                                    {"entry": 100.0, "stop_loss": 95.0, "leverage": 5},
                                    owner="w1")
    check("intencao_pendente_criada", reserva.granted, str(reserva))
    res = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao, confirm=True)
    check("intencao_pendente_bloqueia_reconhecimento",
          res["reason_code"] == mps.ACK_INTENT_PENDING, str(res)[:200])
    await intents.mark_terminal(db.get_session, ident_alfa.intent_key, owner="w1",
                                reason="TESTE")

    async with db.get_session() as session:
        session.add(RealTrade(symbol=ALFA, side="long", entry_price=100.0, qty=1.0,
                              status="open", source="auto", exchange="binance",
                              opened_at=datetime.now(timezone.utc),
                              client_order_id="cw-bot-1"))
        await session.commit()
    res = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao, confirm=True)
    check("real_trade_do_bot_bloqueia_reconhecimento",
          res["reason_code"] == mps.ACK_BOT_TRADE_PRESENT, str(res)[:200])
    async with db.get_session() as session:
        await session.execute(sql_update(RealTrade).values(status="closed"))
        await session.commit()

    EXCHANGE["algos"] = [{"symbol": ALFA_EX, "algo_id": "A9",
                          "client_algo_id": "cw-bot-1-sl"}]
    res = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao, confirm=True)
    check("ordem_condicional_do_bot_bloqueia",
          res["reason_code"] == mps.ACK_BOT_ORDERS_PRESENT, str(res)[:200])
    EXCHANGE["algos"] = [{"symbol": ALFA_EX, "algo_id": "M1",
                          "client_algo_id": "operador-sl"}]
    check("ainda_sem_gravacao", len(await acks()) == 0)

    # ══════════════════════════════════════════════════════════════════════
    #  4. Reconhecimento aceito, idempotente e com vínculo atômico
    # ══════════════════════════════════════════════════════════════════════
    aceito = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao,
                                   confirm=True, reason="posição minha, anterior ao bot",
                                   identity_note="admin-1")
    check("reconhecimento_aceito",
          aceito["ok"] and aceito["acknowledged"]
          and aceito["acknowledgement"]["state"] == STATE_ACTIVE, str(aceito)[:220])
    check("resposta_explica_que_nao_e_autorizacao",
          "não é autorização" in aceito["note"].lower()
          or "nao e autorizacao" in aceito["note"].lower(), aceito["note"])
    check("vinculo_atomico_com_a_causa_untracked",
          aceito["acknowledgement"]["incident_key"] == chave_untracked,
          str(aceito["acknowledgement"])[:220])
    registro = (await acks())[0]
    check("registro_guarda_identidade_decimal",
          Decimal(str(registro.qty)) == Decimal("2.5")
          and int(registro.exchange_update_time_ms) == 1_770_000_000_000,
          f"{registro.qty} {registro.exchange_update_time_ms}")
    repetido = await mps.acknowledge(symbol=ALFA, expected_fingerprint=impressao,
                                     confirm=True)
    check("confirmacao_repetida_e_idempotente",
          repetido["ok"] and repetido["idempotent"]
          and len(await acks(STATE_ACTIVE)) == 1, str(repetido)[:200])

    # Índice parcial: duas linhas ACTIVE para a MESMA posição são impossíveis.
    duplicou = False
    try:
        async with db.get_session() as session:
            session.add(Ack(account_scope=escopo, exchange="binance",
                            market="usdm_futures", symbol=ALFA, quote="USDT",
                            side="buy", position_side="BOTH", qty=Decimal("2.5"),
                            entry_price=Decimal("100"),
                            exchange_update_time_ms=1_770_000_000_001,
                            fingerprint="x" * 64, contract_version="MANUAL_ACK_V1",
                            state=STATE_ACTIVE, created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc)))
            await session.commit()
        duplicou = True
    except Exception:
        duplicou = False
    check("banco_impede_dois_reconhecimentos_ativos", duplicou is False)

    # ══════════════════════════════════════════════════════════════════════
    #  5. Guard de propriedade em TODOS os caminhos mutantes
    # ══════════════════════════════════════════════════════════════════════
    bloq_alfa = await mps.ownership_guard(ALFA, action="entry")
    bloq_alfa_venda = await mps.ownership_guard(ALFA, action="entry_short")
    livre_beta = await mps.ownership_guard(BETA, action="entry")
    check("alfa_bloqueado_nos_dois_lados",
          bloq_alfa["allowed"] is False and bloq_alfa_venda["allowed"] is False
          and bloq_alfa["reason_code"] == mps.GUARD_MANUAL_SYMBOL, str(bloq_alfa))
    check("beta_continua_liberado", livre_beta["allowed"], str(livre_beta))
    sem_simbolo = await mps.ownership_guard(None, action="cancel_algo_order")
    check("prova_inconclusiva_bloqueia",
          sem_simbolo["allowed"] is False
          and sem_simbolo["reason_code"] == mps.GUARD_SYMBOL_UNKNOWN, str(sem_simbolo))

    MUTACOES.clear()
    chamadas = {
        "set_leverage": bss.set_leverage(ALFA_EX, 10),
        "place_order": bss.place_order(ALFA, "BUY", 1.0, order_type="Market",
                                       entry_preflight=None, leverage=10),
        "place_protection_orders": bss.place_protection_orders(
            ALFA, "Buy", 1.0, stop_loss=90.0, tp1=110.0, tp2=120.0),
        "cancel_order": bss.cancel_order(ALFA, order_id="1"),
        "cancel_algo_order": bss.cancel_algo_order("M1", symbol=ALFA),
    }
    if hasattr(bss, "place_maker_entry_then_protect"):
        chamadas["maker"] = bss.place_maker_entry_then_protect(
            ALFA, "BUY", 1.0, limit_price=99.0, stop_loss=90.0, tp1=110.0,
            leverage=10)
    resultados = {}
    for nome, corrotina in chamadas.items():
        resultados[nome] = await corrotina
    for nome, res in resultados.items():
        check(f"alfa_sem_mutacao_em_{nome}",
              res.get("ok") is False and res.get("manual_ownership_blocked") is True,
              f"{nome}: {str(res)[:160]}")
    check("zero_requisicoes_assinadas_em_alfa", MUTACOES == [], str(MUTACOES)[:200])

    # BETA continua operando: proteção legítima do bot não é desativada.
    MUTACOES.clear()
    lev_beta = await bss.set_leverage(BETA_EX, 5)
    cancel_beta = await bss.cancel_order(BETA, order_id="9")
    check("beta_mantem_alavancagem_e_cancel", lev_beta.get("ok") and cancel_beta.get("ok"),
          f"{lev_beta} {cancel_beta}")
    check("beta_chegou_no_transporte", len(MUTACOES) == 2, str(MUTACOES)[:160])

    # Registro ILEGÍVEL bloqueia (não vira lista vazia).
    async def registro_quebrado():
        raise RuntimeError("registro fora do ar")

    with patch.object(mps, "active_acknowledgements", registro_quebrado):
        MUTACOES.clear()
        quebrado = await bss.set_leverage(BETA_EX, 5)
    check("registro_ilegivel_bloqueia_mutacao",
          quebrado.get("ok") is False and MUTACOES == [], str(quebrado)[:160])

    # ══════════════════════════════════════════════════════════════════════
    #  6. Reconciliador: MANUAL_ACKNOWLEDGED encerra SÓ o UNTRACKED da manual
    # ══════════════════════════════════════════════════════════════════════
    outro = await ers.record_incident(kind=ers.Kind.ENTRY_SUBMISSION_UNKNOWN,
                                      symbol=BETA, side="buy",
                                      client_order_id="cw-beta-1")
    check("outro_incidente_criado", outro.get("persisted"), str(outro)[:160])
    # Ciclo OFICIAL completo: o UNTRACKED escala para MANUAL_REQUIRED e só
    # então a re-checagem avalia a causa.
    await ers.reconcile_due()
    ers._untracked_recheck_at.clear()
    recheck = await ers.recheck_untracked_manual()
    linha = [l for l in await incidentes(ALFA)][0]
    check("untracked_da_manual_encerra_como_reconhecida",
          linha.state == ers.State.MANUAL_ACKNOWLEDGED and linha.resolved_at is not None
          and recheck["resolved"] == 0,       # o ciclo oficial já encerrou
          f"{recheck} {linha.state}")
    check("posicao_aberta_nunca_vira_flat_nem_protected",
          linha.state not in (ers.State.FLAT, ers.State.PROTECTED), linha.state)
    beta_inc = [l for l in await incidentes(BETA)]
    check("outros_incidentes_preservados",
          len(beta_inc) == 1 and beta_inc[0].resolved_at is None,
          str([(l.kind, l.state) for l in beta_inc]))
    check("pausa_continua_com_outra_causa_aberta", await pausado())

    # `MANUAL_ACKNOWLEDGED` não prova nada sobre ordens do bot.
    check("reconhecida_nao_entra_nas_provas_de_entrada",
          ers.State.MANUAL_ACKNOWLEDGED not in ers._TERMINAL_SAFE
          and ers.State.MANUAL_ACKNOWLEDGED in ers._TERMINAL_CLOSED)

    # Resolvido o incidente de BETA, a liberação vem pelo fluxo P03 oficial.
    async with db.get_session() as session:
        await session.execute(sql_update(ExecutionIncident)
                              .where(ExecutionIncident.symbol == BETA)
                              .values(state=ers.State.FLAT,
                                      resolved_at=datetime.now(timezone.utc)))
        await session.commit()
    ers._p03_latch_armed = True
    ers._boot_scan_safe = True
    liberou = await ers._maybe_release_quarantine()
    check("liberacao_pelo_fluxo_oficial_p03", liberou is True and not await pausado(),
          f"liberou={liberou} pausado={await pausado()}")

    # ══════════════════════════════════════════════════════════════════════
    #  7. Orçamento BOT separado; margem REAL continua obrigatória
    # ══════════════════════════════════════════════════════════════════════
    async def reservar(symbol_guardado, *, trigger, risco, margem, disponivel,
                       max_risk=10.0, idade_ms=0):
        ident = intents.EntryIdentity(
            account_ref=escopo, exchange="binance", symbol=symbol_guardado,
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)
        agora_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Observação de carteira no contrato novo: identidade + geração vigente.
        async with db.get_session() as session:
            geracao = await intents.current_margin_generation(
                session, account_ref=ident.account_ref, exchange=ident.exchange,
                market="usdm_futures")
            await session.commit()
        reserva = await intents.reserve(
            db.get_session, ident,
            {"entry": 100.0, "stop_loss": 95.0, "leverage": 5}, owner="w1",
            capacity=intents.Capacity(risk_usd=risco, max_open_positions=20,
                                      max_open_risk_usd=max_risk),
            margin=intents.MarginGate(available_usd=disponivel, required_usd=margem,
                                      as_of_ms=agora_ms - idade_ms, complete=True,
                                      account_ref=ident.account_ref,
                                      exchange=ident.exchange,
                                      market="usdm_futures", generation=geracao,
                                      quality="live"))
        return ident, reserva

    # A posição manual tem risco nominal MAIOR que o teto do bot e mesmo assim
    # não consome o orçamento: ela não é RealTrade e não entra nas coortes.
    _, livre = await reservar("BETA-USDT-USDT", trigger=1, risco=4.0, margem=50.0,
                              disponivel=1_000.0)
    check("manual_nao_consome_orcamento_nominal_do_bot", livre.granted, str(livre))
    # Operações BOT + intenções continuam consumindo.
    _, segunda = await reservar("GAMA-USDT-USDT", trigger=2, risco=9.0, margem=50.0,
                                disponivel=1_000.0)
    check("intencoes_bot_continuam_consumindo_o_orcamento",
          segunda.granted is False and segunda.reason == "MAX_OPEN_RISK", str(segunda))
    # Margem livre insuficiente bloqueia mesmo com orçamento BOT sobrando.
    _, sem_margem = await reservar("DELTA-USDT-USDT", trigger=3, risco=1.0,
                                   margem=900.0, disponivel=100.0)
    check("margem_livre_insuficiente_bloqueia_beta",
          sem_margem.granted is False
          and sem_margem.reason == "INSUFFICIENT_FREE_MARGIN", str(sem_margem))
    # Parcela desconhecida NÃO vira zero.
    ident_x = intents.EntryIdentity(
        account_ref=escopo, exchange="binance", symbol="EPS-USDT-USDT", quote="USDT",
        side="long", position_side="BOTH", timeframe="4h", playbook="CHAMPION_LEGACY",
        playbook_version="SCORE_V2", purpose="ENTRY", trigger_candle_ms=4)
    async with db.get_session() as session:
        g_x = await intents.current_margin_generation(
            session, account_ref=ident_x.account_ref, exchange=ident_x.exchange,
            market="usdm_futures")
        await session.commit()
    desconhecida = await intents.reserve(
        db.get_session, ident_x, {"entry": 100.0, "stop_loss": 95.0, "leverage": 5},
        owner="w1", margin=intents.MarginGate(
            available_usd=None, required_usd=10.0,
            as_of_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            complete=False, account_ref=ident_x.account_ref,
            exchange=ident_x.exchange, market="usdm_futures", generation=g_x,
            quality="live"))
    check("margem_desconhecida_nao_vira_zero",
          desconhecida.granted is False
          and desconhecida.reason == "FREE_MARGIN_UNKNOWN", str(desconhecida))
    _, velha = await reservar("ZETA-USDT-USDT", trigger=5, risco=1.0, margem=10.0,
                              disponivel=1_000.0, idade_ms=600_000)
    check("carteira_velha_nao_vale_como_atual",
          velha.granted is False and velha.reason == "FREE_MARGIN_STALE", str(velha))

    # ── Coortes BOT × manual: a manual não é RealTrade e não entra em NADA ─
    from services import kill_switch_service as kss
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM real_trades"))
        agora = datetime.now(timezone.utc)
        session.add(RealTrade(symbol=BETA, side="long", entry_price=100.0, qty=1.0,
                              status="open", source="auto", exchange="binance",
                              opened_at=agora, client_order_id="cw-beta-open"))
        # `auto` ENCERRADA MANUALMENTE continua automática: não vira manual e
        # não some da contabilidade do bot.
        session.add(RealTrade(symbol="GAMA/USDT:USDT", side="long", entry_price=100.0,
                              qty=1.0, status="closed_manual", source="auto",
                              exchange="binance", opened_at=agora, closed_at=agora,
                              pnl_usd=-3.0, client_order_id="cw-gama-closed"))
        session.add(RealTrade(symbol="DELTA/USDT:USDT", side="long", entry_price=100.0,
                              qty=1.0, status="open", source="managed",
                              exchange="binance", opened_at=agora,
                              client_order_id="cw-delta-managed"))
        await session.commit()
    abertos_kill = await kss._count_open()
    abertos_hoje = await kss._daily_opens()
    async with db.get_session() as session:
        abertos_auto = await intents._open_positions(session)
        fontes = sorted({linha[0] for linha in (await session.execute(
            select(RealTrade.source))).all()})
    check("contagem_bot_ignora_a_posicao_manual",
          abertos_kill == 2 and abertos_auto == 1, f"{abertos_kill} {abertos_auto}")
    check("auto_encerrada_manualmente_continua_automatica",
          "auto" in fontes and "manual" not in fontes and abertos_hoje == 3,
          f"{fontes} {abertos_hoje}")
    check("tratamento_de_managed_preservado", "managed" in fontes, str(fontes))
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM real_trades"))
        await session.commit()

    # ══════════════════════════════════════════════════════════════════════
    #  8. Duas admissões disputando a MESMA margem livre
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM entry_intents"))
        await session.commit()
    barreira = asyncio.Event()

    async def disputa(trigger, atrasar):
        if atrasar:
            await barreira.wait()
        return await reservar("OMEGA-USDT-USDT" if trigger == 1 else "SIGMA-USDT-USDT",
                              trigger=trigger, risco=0.5, margem=80.0,
                              disponivel=100.0, max_risk=1_000.0)

    tarefa_a = asyncio.create_task(disputa(1, False))
    await asyncio.sleep(0.05)
    tarefa_b = asyncio.create_task(disputa(2, True))
    barreira.set()
    (_, r_a), (_, r_b) = await asyncio.gather(tarefa_a, tarefa_b)
    concedidas = [r for r in (r_a, r_b) if r.granted]
    check("no_maximo_a_capacidade_real_e_reservada",
          len(concedidas) == 1, f"{r_a.decision}/{r_a.reason} {r_b.decision}/{r_b.reason}")
    recusada = [r for r in (r_a, r_b) if not r.granted][0]
    check("a_segunda_proposta_e_recusada_por_margem",
          recusada.reason == "INSUFFICIENT_FREE_MARGIN", str(recusada))
    async with db.get_session() as session:
        soma = float((await session.execute(select(
            func.coalesce(func.sum(EntryIntent.reserved_margin_usd), 0.0)))).scalar() or 0)
    check("margem_reservada_persistida_uma_vez", abs(soma - 80.0) < 1e-6, str(soma))

    # Dispatch FINAL maior que a reserva original é readmitido — e bloqueia se
    # não couber (nada de intervalo sem reserva na transição).
    vencedora = [r for r in (r_a, r_b) if r.granted][0]
    async def gate_para(intent_key, disponivel, requerido):
        async with db.get_session() as session:
            linha = await intents.get_intent(db.get_session, intent_key)
            g = await intents.current_margin_generation(
                session, account_ref=linha.account_ref, exchange=linha.exchange,
                market="usdm_futures")
            await session.commit()
        return intents.MarginGate(
            available_usd=disponivel, required_usd=requerido,
            as_of_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            complete=True, account_ref=linha.account_ref, exchange=linha.exchange,
            market="usdm_futures", generation=g, quality="live")

    maior = await intents.admit_final_risk(
        db.get_session, vencedora.intent_key, owner="w1", risk_usd=0.5,
        margin=await gate_para(vencedora.intent_key, 100.0, 95.0))
    check("dispatch_final_maior_que_a_reserva_e_readmitido", maior.granted, str(maior))
    async with db.get_session() as session:
        atual = float((await session.execute(select(EntryIntent.reserved_margin_usd)
                                             .where(EntryIntent.intent_key ==
                                                    vencedora.intent_key))).scalar() or 0)
    check("reserva_final_substitui_a_menor", abs(atual - 95.0) < 1e-6, str(atual))
    estourou = await intents.admit_final_risk(
        db.get_session, vencedora.intent_key, owner="w1", risk_usd=0.5,
        margin=await gate_para(vencedora.intent_key, 100.0, 150.0))
    check("dispatch_que_nao_cabe_e_bloqueado",
          estourou.granted is False
          and estourou.reason == "INSUFFICIENT_FREE_MARGIN", str(estourou))

    # ══════════════════════════════════════════════════════════════════════
    #  9. Reconhecimento × reserva concorrente (dois sentidos, com barreira)
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM entry_intents"))
        await session.commit()
    # (a) A RESERVA chega primeiro: a confirmação encontra intenção pendente e
    #     recusa — nunca aceita com base numa leitura anterior à reserva.
    EXCHANGE["positions"] = [posicao(symbol="THETAUSDT")]
    cand_theta = await candidato_de("THETA/USDT:USDT")
    fp_theta = cand_theta["fingerprint"]
    porta = asyncio.Event()
    original_bot_state = mps._bot_state_for_symbol

    async def bot_state_atrasado(session, chave, *, observed_at_ms):
        if chave == "THETA/USDT":
            await porta.wait()
        return await original_bot_state(session, chave, observed_at_ms=observed_at_ms)

    async def confirmar_theta():
        with patch.object(mps, "_bot_state_for_symbol", bot_state_atrasado):
            return await mps.acknowledge(symbol="THETA/USDT:USDT",
                                         expected_fingerprint=fp_theta, confirm=True)

    tarefa_ack = asyncio.create_task(confirmar_theta())
    await asyncio.sleep(0.05)
    _, reserva_theta = await reservar("THETA-USDT-USDT", trigger=11, risco=0.5,
                                      margem=10.0, disponivel=1_000.0, max_risk=1_000.0)
    porta.set()
    ack_theta = await tarefa_ack
    check("reserva_primeiro_impede_a_confirmacao",
          reserva_theta.granted and ack_theta["ok"] is False
          and ack_theta["reason_code"] == mps.ACK_INTENT_PENDING,
          f"{reserva_theta.decision} {ack_theta.get('reason_code')}")

    # (b) O RECONHECIMENTO chega primeiro: a entrada naquele símbolo é recusada
    #     pelo guard — antes de qualquer mutação de conta.
    await intents.mark_terminal(db.get_session, reserva_theta.intent_key, owner="w1",
                                reason="TESTE")
    ack_theta2 = await mps.acknowledge(symbol="THETA/USDT:USDT",
                                       expected_fingerprint=fp_theta, confirm=True)
    check("reconhecimento_apos_encerrar_a_intencao", ack_theta2["ok"], str(ack_theta2)[:200])
    MUTACOES.clear()
    bloqueio_theta = await bss.set_leverage("THETAUSDT", 7)
    check("apos_reconhecer_a_entrada_e_recusada_antes_da_alavancagem",
          bloqueio_theta.get("ok") is False and MUTACOES == [], str(bloqueio_theta)[:160])

    # (c) Duas confirmações concorrentes: só UMA linha ativa.
    EXCHANGE["positions"] = [posicao(symbol="IOTAUSDT")]
    cand_iota = await candidato_de("IOTA/USDT:USDT")
    fp_iota = cand_iota["fingerprint"]
    duplas = await asyncio.gather(
        mps.acknowledge(symbol="IOTA/USDT:USDT", expected_fingerprint=fp_iota, confirm=True),
        mps.acknowledge(symbol="IOTA/USDT:USDT", expected_fingerprint=fp_iota, confirm=True))
    async with db.get_session() as session:
        ativos_iota = int((await session.execute(
            select(func.count(Ack.id)).where(Ack.symbol == "IOTA/USDT:USDT",
                                             Ack.state == STATE_ACTIVE))).scalar() or 0)
    check("duas_confirmacoes_concorrentes_deixam_um_registro",
          ativos_iota == 1 and all(r.get("ok") for r in duplas),
          f"{ativos_iota} {[r.get('reason_code') for r in duplas]}")

    # ══════════════════════════════════════════════════════════════════════
    #  10. Invalidação por identidade — e mark price NÃO invalida
    # ══════════════════════════════════════════════════════════════════════
    EXCHANGE["positions"] = [posicao(), posicao(symbol="THETAUSDT"),
                             posicao(symbol="IOTAUSDT")]
    # Só o mark price muda: continua válido.
    EXCHANGE["positions"][0] = posicao(mark="130")
    revalida = await mps.revalidate_active()
    check("mark_price_alterado_nao_invalida",
          revalida["ok"] and registro.id in revalida["valid"], str(revalida))
    for nome, alterado in (("qty_parcial", posicao(size="1.0")),
                           ("lado_invertido", posicao(side="Sell")),
                           ("reabertura", posicao(update_time=1_770_000_999_999))):
        EXCHANGE["positions"][0] = alterado
        veredito = await mps.revalidate_active()
        atual = [a for a in await acks() if a.id == registro.id][0]
        check(f"identidade_divergente_invalida_{nome}",
              registro.id in veredito["invalidated"]
              and atual.state == STATE_INVALIDATED and atual.ended_at is None,
              f"{nome}: {atual.state}")
        # INVALIDATED continua BLOQUEANDO: "não está ACTIVE" não significa
        # "símbolo livre". É preciso nova confirmação ou encerramento provado.
        ainda_bloqueado = await mps.ownership_guard(ALFA, action="entry")
        check(f"apos_invalidar_o_simbolo_continua_bloqueado_{nome}",
              ainda_bloqueado["allowed"] is False
              and ainda_bloqueado["reason_code"] == mps.GUARD_MANUAL_SYMBOL,
              str(ainda_bloqueado))
        scan_novo = await ers._detect_untracked_positions()
        check(f"contencao_reaberta_{nome}", scan_novo["status"] == "UNTRACKED",
              str(scan_novo))
        # Restaura para o próximo caso.
        async with db.get_session() as session:
            await session.execute(sql_update(Ack).where(Ack.id == registro.id)
                                  .values(state=STATE_ACTIVE, ended_at=None,
                                          ended_reason=None,
                                          validated_at_ms=int(
                                              datetime.now(timezone.utc).timestamp() * 1000)))
            await session.execute(text(
                "UPDATE execution_incidents SET resolved_at = now(), "
                "state = 'MANUAL_ACKNOWLEDGED' WHERE symbol = :s"), {"s": ALFA})
            await session.commit()
        EXCHANGE["positions"][0] = posicao()

    # Conta/credencial diferente derruba TODAS as autorizações.
    with patch.object(bss, "_API_KEY", "outra-credencial"):
        troca = await mps.revalidate_active()
    check("conta_diferente_invalida_tudo",
          # Credencial nova ⇒ OUTRA conta: nenhuma autorização anterior vale.
          (troca["ok"] is False and troca["reason_code"] == mps.ACK_NO_ACCOUNT)
          or (troca.get("valid") == [] and troca.get("invalidated")),
          str(troca)[:200])
    async with db.get_session() as session:
        sobraram = int((await session.execute(
            select(func.count(Ack.id)).where(Ack.state == STATE_ACTIVE))).scalar() or 0)
    check("nenhum_reconhecimento_sobrevive_a_troca_de_conta", sobraram == 0, str(sobraram))
    # Reconhece de novo para os passos seguintes.
    EXCHANGE["positions"] = [posicao()]
    cand2 = await candidato_de()
    ack2 = await mps.acknowledge(symbol=ALFA, expected_fingerprint=cand2["fingerprint"],
                                 confirm=True, reason="reconhecimento renovado")
    check("novo_reconhecimento_apos_invalidacao", ack2["ok"], str(ack2)[:200])
    id_ack2 = ack2["acknowledgement"]["id"]

    # ══════════════════════════════════════════════════════════════════════
    #  11. Múltiplas pernas e agregação BOT/manual ficam BLOQUEADAS
    # ══════════════════════════════════════════════════════════════════════
    EXCHANGE["positions"] = [posicao(symbol="KAPPAUSDT", position_side="LONG"),
                             posicao(symbol="KAPPAUSDT", side="Sell",
                                     position_side="SHORT")]
    ambiguo = await candidato_de("KAPPA/USDT:USDT")
    check("pernas_ambiguas_nao_sao_condensadas",
          ambiguo and ambiguo.get("eligible") is False
          and ambiguo.get("reason_code") == mps.ACK_AMBIGUOUS_LEGS, str(ambiguo))
    recusa_ambigua = await mps.acknowledge(symbol="KAPPA/USDT:USDT",
                                           expected_fingerprint="x" * 64, confirm=True)
    check("confirmacao_de_pernas_ambiguas_recusa",
          recusa_ambigua["reason_code"] == mps.ACK_AMBIGUOUS_LEGS,
          str(recusa_ambigua)[:160])
    # O manager também não escolhe a primeira perna: leitura fica INCERTA.
    from services import trade_manager_service as tms
    qty, _entrada = await tms._fetch_exchange_position("KAPPA/USDT:USDT")
    qty_lado, _e2 = await tms._fetch_exchange_position("KAPPA/USDT:USDT", side="long")
    check("manager_nao_escolhe_a_primeira_posicao_do_simbolo", qty is None, str(qty))
    check("manager_identifica_a_perna_pelo_lado", qty_lado == 2.5, str(qty_lado))
    EXCHANGE["positions"] = [posicao()]

    # ══════════════════════════════════════════════════════════════════════
    #  12. Fechamento: ordens manuais residuais mantêm o símbolo indisponível
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM execution_incidents"))
        await session.commit()
    scan_flat = await ers._detect_untracked_positions()
    linha_alfa = [l for l in await incidentes(ALFA)]
    ers._untracked_recheck_at.clear()
    await ers.recheck_untracked_manual()
    EXCHANGE["positions"] = []                      # operador fechou a posição
    EXCHANGE["algos"] = [{"symbol": ALFA_EX, "algo_id": "M1",
                          "client_algo_id": "operador-sl"}]
    async with db.get_session() as session:
        await session.execute(text(
            "UPDATE execution_incidents SET resolved_at = NULL, state = 'MANUAL_REQUIRED' "
            "WHERE symbol = :s"), {"s": ALFA})
        await session.commit()
    ers._untracked_recheck_at.clear()
    com_ordens = await ers.recheck_untracked_manual()
    check("ordens_manuais_vivas_impedem_reutilizar_o_simbolo",
          com_ordens["resolved"] == 0
          and com_ordens["kept"].get(linha_alfa[0].incident_key) == "MANUAL_ORDERS_LIVE",
          str(com_ordens))
    check("nenhuma_ordem_manual_foi_cancelada",
          all(p[1] != "/fapi/v1/algoOrder" for p in MUTACOES), str(MUTACOES)[:200])
    EXCHANGE["algos"] = []
    ers._untracked_recheck_at.clear()
    sem_ordens = await ers.recheck_untracked_manual()
    check("ausencia_fresca_encerra_o_incidente", sem_ordens["resolved"] == 1,
          str(sem_ordens))
    revalida_fim = await mps.revalidate_active()
    atual2 = [a for a in await acks() if a.id == id_ack2][0]
    check("fechamento_encerra_o_registro_preservando_historico",
          id_ack2 in revalida_fim["closed"] and atual2.state == STATE_CLOSED
          and atual2.created_at is not None and atual2.ended_reason,
          f"{atual2.state} {atual2.ended_reason}")
    check("historico_preservado_sem_apagar_linhas", len(await acks()) >= 3,
          str(len(await acks())))

    # ══════════════════════════════════════════════════════════════════════
    #  13. Restart: reconhecimento sobrevive, revalida antes de liberar
    # ══════════════════════════════════════════════════════════════════════
    EXCHANGE["positions"] = [posicao()]
    cand3 = await candidato_de()
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM execution_incidents"))
        await session.commit()
    ack3 = await mps.acknowledge(symbol=ALFA, expected_fingerprint=cand3["fingerprint"],
                                 confirm=True, reason="antes do restart")
    check("reconhecimento_antes_do_restart", ack3["ok"], str(ack3)[:160])
    await db._engine.dispose()                      # processo novo (pool derrubado)
    apos = await mps.active_acknowledgements()
    check("restart_preserva_o_reconhecimento",
          apos["ok"] and len(apos["acks"]) == 1
          and apos["acks"][0]["fingerprint"] == cand3["fingerprint"], str(apos)[:200])
    async with db.get_session() as session:
        total_ativos = int((await session.execute(
            select(func.count(Ack.id)).where(Ack.state == STATE_ACTIVE))).scalar() or 0)
    check("restart_nao_duplica_registro", total_ativos == 1, str(total_ativos))
    # Identidade divergente descoberta no boot mantém o bloqueio.
    EXCHANGE["positions"] = [posicao(size="9.0")]
    scan_pos_restart = await ers._detect_untracked_positions()
    check("boot_com_identidade_divergente_reabre_contencao",
          scan_pos_restart["status"] in ("UNTRACKED", "UNKNOWN") and await pausado(),
          str(scan_pos_restart))
    EXCHANGE["quality"] = "stale"
    ers._boot_scan_safe = True
    scan_incerto = await ers._detect_untracked_positions()
    check("leitura_incerta_no_boot_mantem_bloqueio",
          scan_incerto["status"] == "UNKNOWN" and ers._boot_scan_safe is False,
          str(scan_incerto))
    EXCHANGE["quality"] = "ok"

    # ══════════════════════════════════════════════════════════════════════
    #  14. Conflito com adoção `managed` é recusado explicitamente
    # ══════════════════════════════════════════════════════════════════════
    EXCHANGE["positions"] = [posicao(symbol="LAMBDAUSDT")]
    cand_l = await candidato_de("LAMBDA/USDT:USDT")
    async with db.get_session() as session:
        session.add(RealTrade(symbol="LAMBDA/USDT:USDT", side="long", entry_price=100.0,
                              qty=1.0, status="open", source="managed",
                              exchange="binance", opened_at=datetime.now(timezone.utc),
                              client_order_id="cw-managed-1"))
        await session.commit()
    conflito = await mps.acknowledge(symbol="LAMBDA/USDT:USDT",
                                     expected_fingerprint=cand_l["fingerprint"],
                                     confirm=True)
    check("adocao_managed_recusa_reconhecimento_manual",
          conflito["ok"] is False
          and conflito["reason_code"] == mps.ACK_BOT_TRADE_PRESENT, str(conflito)[:200])
    async with db.get_session() as session:
        ainda_managed = (await session.execute(
            select(RealTrade.source).where(RealTrade.client_order_id == "cw-managed-1"))
        ).scalar()
    check("managed_nao_vira_manual_em_segundo_plano", ainda_managed == "managed",
          str(ainda_managed))

    # ══════════════════════════════════════════════════════════════════════
    #  15. Falha de persistência não autoriza nada
    # ══════════════════════════════════════════════════════════════════════
    EXCHANGE["positions"] = [posicao(symbol="MIUUSDT")]
    cand_m = await candidato_de("MIU/USDT:USDT")

    async def persistencia_quebrada(*args, **kwargs):
        raise RuntimeError("banco fora")

    with patch.object(mps, "_persist_acknowledgement", persistencia_quebrada):
        falhou = False
        try:
            await mps.acknowledge(symbol="MIU/USDT:USDT",
                                  expected_fingerprint=cand_m["fingerprint"],
                                  confirm=True)
        except RuntimeError:
            falhou = True
    async with db.get_session() as session:
        miu = int((await session.execute(
            select(func.count(Ack.id)).where(Ack.symbol == "MIU/USDT:USDT"))).scalar() or 0)
    check("escrita_malsucedida_nao_libera_nem_grava",
          falhou and miu == 0,
          f"falhou={falhou} linhas={miu}")
    guard_miu = await mps.ownership_guard("MIU/USDT:USDT", action="entry")
    check("simbolo_sem_registro_nao_fica_isento", guard_miu["allowed"] is True,
          str(guard_miu))


    # ══════════════════════════════════════════════════════════════════════
    #  16. Escopo da observação, estados bloqueantes e substituição explícita
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM manual_position_acks"))
        await session.execute(text("DELETE FROM execution_incidents"))
        await session.commit()
    EXCHANGE["quality"] = "ok"
    EXCHANGE["algos"], EXCHANGE["common"] = [], []
    EXCHANGE["algos_ok"] = EXCHANGE["common_ok"] = True
    EXCHANGE["positions"] = [posicao(), posicao(symbol="BETAUSDT")]
    cand_alfa = await candidato_de(ALFA)
    cand_beta = await candidato_de(BETA)
    ack_alfa = await mps.acknowledge(symbol=ALFA,
                                     expected_fingerprint=cand_alfa["fingerprint"],
                                     confirm=True, reason="manual alfa")
    ack_beta = await mps.acknowledge(symbol=BETA,
                                     expected_fingerprint=cand_beta["fingerprint"],
                                     confirm=True, reason="manual beta")
    check("duas_posicoes_manuais_reconhecidas",
          ack_alfa["ok"] and ack_beta["ok"], f"{ack_alfa.get('reason_code')} "
          f"{ack_beta.get('reason_code')}")
    id_beta = ack_beta["acknowledgement"]["id"]

    # T1 no fluxo persistido: consulta só ALFA não altera BETA.
    EXCHANGE["positions"] = [posicao(), posicao(symbol="BETAUSDT")]
    observacao_alfa = await mps.observe_positions(ALFA)
    check("observacao_declara_escopo_e_completude",
          observacao_alfa["scope"] == mps.SCOPE_SYMBOL
          and observacao_alfa["complete"] is True
          and observacao_alfa["symbol_key"] == mps.symbol_key(ALFA),
          str({k: observacao_alfa[k] for k in ("scope", "complete", "symbol_key")}))
    await mps.revalidate_active(observation=observacao_alfa)
    async with db.get_session() as session:
        estado_beta = (await session.execute(
            select(Ack.state).where(Ack.id == id_beta))).scalar()
    check("consulta_de_alfa_nao_encerra_beta", estado_beta == STATE_ACTIVE,
          str(estado_beta))
    guarda_beta = await mps.ownership_guard(BETA, action="entry")
    check("beta_continua_bloqueada_apos_consulta_de_alfa",
          guarda_beta["allowed"] is False, str(guarda_beta))

    # Observação de CONTA incompleta (linha malformada) não prova ausência.
    EXCHANGE["positions"] = [posicao(), "linha-invalida"]
    incompleta = await mps.observe_positions()
    check("linha_malformada_torna_a_observacao_incompleta",
          incompleta["ok"] is False and incompleta["complete"] is False,
          str(incompleta)[:160])
    veredito_incompleto = await mps.revalidate_active(observation=incompleta)
    check("observacao_incompleta_nao_muda_nada",
          veredito_incompleto["ok"] is False, str(veredito_incompleto)[:160])
    async with db.get_session() as session:
        ainda_ativos = int((await session.execute(
            select(func.count(Ack.id)).where(Ack.state == STATE_ACTIVE))).scalar() or 0)
    check("nenhum_reconhecimento_encerrado_por_leitura_incompleta",
          ainda_ativos == 2, str(ainda_ativos))

    # Flat + ordem COMUM do operador: não encerra; símbolo segue bloqueado.
    EXCHANGE["positions"] = [posicao(symbol="BETAUSDT")]     # ALFA fechou
    EXCHANGE["common"] = [{"symbol": ALFA_EX, "order_id": "77",
                           "client_order_id": "operador-limit"}]
    obs_conta = await mps.observe_positions()
    await mps.revalidate_active(observation=obs_conta)
    async with db.get_session() as session:
        estado_alfa = (await session.execute(
            select(Ack.state).where(Ack.symbol == ALFA,
                                    Ack.state.in_(list(mps.BLOCKING_STATES)))
        )).scalar()
    check("flat_com_limit_manual_nao_encerra",
          estado_alfa == "WAITING_ORDERS", str(estado_alfa))
    guarda_alfa = await mps.ownership_guard(ALFA, action="entry")
    check("waiting_orders_continua_bloqueando",
          guarda_alfa["allowed"] is False
          and guarda_alfa["reason_code"] == mps.GUARD_MANUAL_SYMBOL,
          str(guarda_alfa))
    check("nenhuma_ordem_do_operador_foi_cancelada",
          all(p[1] != "/fapi/v1/order" and p[1] != "/fapi/v1/algoOrder"
              for p in MUTACOES), str(MUTACOES)[:200])

    # Falha numa das listagens também mantém o bloqueio.
    EXCHANGE["common"], EXCHANGE["common_ok"] = [], False
    obs_conta = await mps.observe_positions()
    await mps.revalidate_active(observation=obs_conta)
    async with db.get_session() as session:
        estado_alfa = (await session.execute(
            select(Ack.state).where(Ack.symbol == ALFA,
                                    Ack.state.in_(list(mps.BLOCKING_STATES)))
        )).scalar()
    check("falha_de_listagem_nao_encerra", estado_alfa == "WAITING_ORDERS",
          str(estado_alfa))
    EXCHANGE["common_ok"] = True

    # Duas listagens completas e vazias: agora encerra.
    obs_conta = await mps.observe_positions()
    await mps.revalidate_active(observation=obs_conta)
    async with db.get_session() as session:
        linha_alfa_final = (await session.execute(
            select(Ack).where(Ack.symbol == ALFA).order_by(Ack.id.desc())
        )).scalars().first()
    check("duas_listagens_vazias_encerram",
          linha_alfa_final.state == STATE_CLOSED
          and linha_alfa_final.ended_at is not None, str(linha_alfa_final.state))
    guarda_livre = await mps.ownership_guard(ALFA, action="entry")
    check("simbolo_liberado_so_com_ausencia_comprovada",
          guarda_livre["allowed"] is True, str(guarda_livre))

    # Nova confirmação explícita substitui INVALIDATED atomicamente.
    EXCHANGE["positions"] = [posicao(symbol="BETAUSDT", size="9.0")]
    obs_conta = await mps.observe_positions()
    await mps.revalidate_active(observation=obs_conta)
    async with db.get_session() as session:
        beta_estado = (await session.execute(
            select(Ack.state).where(Ack.id == id_beta))).scalar()
    check("identidade_divergente_invalida_sem_encerrar",
          beta_estado == STATE_INVALIDATED, str(beta_estado))
    cand_beta2 = await candidato_de(BETA)
    ack_beta2 = await mps.acknowledge(symbol=BETA,
                                      expected_fingerprint=cand_beta2["fingerprint"],
                                      confirm=True, reason="nova confirmação")
    check("nova_confirmacao_substitui_o_invalidado", ack_beta2["ok"],
          str(ack_beta2)[:200])
    async with db.get_session() as session:
        linhas_beta = (await session.execute(
            select(Ack.id, Ack.state).where(Ack.symbol == BETA)
            .order_by(Ack.id))).all()
    bloqueantes_beta = [l for l in linhas_beta if l[1] in mps.BLOCKING_STATES]
    check("substituicao_nao_cria_duas_linhas_bloqueantes",
          len(bloqueantes_beta) == 1 and len(linhas_beta) == 2,
          str(linhas_beta))
    check("historico_do_anterior_preservado_como_superseded",
          any(l[0] == id_beta and l[1] == "SUPERSEDED" for l in linhas_beta),
          str(linhas_beta))

    # Scan ATRASADO (revisão antiga) não sobrescreve o reconhecimento novo.
    novo_id = ack_beta2["acknowledgement"]["id"]
    async with db.get_session() as session:
        revisao_atual = (await session.execute(
            select(Ack.revision).where(Ack.id == novo_id))).scalar()
    atrasado = await mps._transition_acks([(novo_id, int(revisao_atual) - 1,
                                            STATE_CLOSED, "scan atrasado")])
    async with db.get_session() as session:
        estado_novo = (await session.execute(
            select(Ack.state).where(Ack.id == novo_id))).scalar()
    check("scan_atrasado_nao_sobrescreve_ack_novo",
          estado_novo == STATE_ACTIVE and novo_id in atrasado["stale"],
          f"{estado_novo} {atrasado}")

    # Falha de persistência propaga UNKNOWN (não vira lista vazia com ok=True).
    async def transicao_quebrada(*args, **kwargs):
        return {"ok": False, "changed": [], "by_state": [], "stale": [],
                "detail": "RuntimeError"}

    EXCHANGE["positions"] = [posicao(symbol="BETAUSDT", size="3.0")]
    obs_conta = await mps.observe_positions()
    with patch.object(mps, "_transition_acks", transicao_quebrada):
        falha = await mps.revalidate_active(observation=obs_conta)
    check("falha_de_persistencia_propaga_unknown",
          falha["ok"] is False
          and falha["reason_code"] == "MANUAL_ACK_PERSISTENCE_FAILED", str(falha))
    async with db.get_session() as session:
        beta_apos = (await session.execute(
            select(Ack.state).where(Ack.id == novo_id))).scalar()
    check("falha_de_persistencia_nao_libera_o_simbolo",
          beta_apos in mps.BLOCKING_STATES, str(beta_apos))

    # T3 persistido: ciclo oficial revalida mesmo com ZERO incidentes abertos.
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM execution_incidents"))
        await session.execute(text(
            "UPDATE manual_position_acks SET validated_at_ms = 1 "
            "WHERE state = ANY(:s)"), {"s": list(mps.BLOCKING_STATES)})
        await session.commit()
    # Identidade IGUAL à reconhecida em `ack_beta2` (size 9.0): o ciclo tem de
    # revalidar e RENOVAR a prova, sem depender de incidente aberto.
    EXCHANGE["positions"] = [posicao(symbol="BETAUSDT", size="9.0")]
    ers._boot_scan_safe = True
    ers._p03_latch_armed = False
    await ers.reconcile_due()
    async with db.get_session() as session:
        prova = (await session.execute(
            select(Ack.validated_at_ms).where(Ack.id == novo_id))).scalar()
    check("ciclo_revalida_sem_incidente_aberto",
          prova is not None and int(prova) > 1, str(prova))
    guarda_nova = await mps.ownership_guard("OUTRO/USDT:USDT", action="entry",
                                            require_fresh_proof=True)
    check("prova_fresca_autoriza_outro_simbolo", guarda_nova["allowed"] is True,
          str(guarda_nova))
    async with db.get_session() as session:
        await session.execute(text(
            "UPDATE manual_position_acks SET validated_at_ms = 1 "
            "WHERE state = ANY(:s)"), {"s": list(mps.BLOCKING_STATES)})
        await session.commit()
    guarda_velha = await mps.ownership_guard("OUTRO/USDT:USDT", action="entry",
                                             require_fresh_proof=True)
    check("prova_vencida_nao_autoriza_nova_exposicao",
          guarda_velha["allowed"] is False
          and guarda_velha["reason_code"] == mps.GUARD_PROOF_STALE,
          str(guarda_velha))
    protetiva = await mps.ownership_guard("OUTRO/USDT:USDT",
                                          action="place_protection_orders",
                                          require_fresh_proof=True)
    check("protecao_de_outro_simbolo_nao_depende_da_prova",
          protetiva["allowed"] is True, str(protetiva))

    await db._engine.dispose()
    print(f"MANUAL_BOT_PG_OK: {len(CHECKS)} verificações — reconhecimento explícito, "
          "guard de propriedade e margem real pelos fluxos persistidos")


if __name__ == "__main__":
    asyncio.run(run())
