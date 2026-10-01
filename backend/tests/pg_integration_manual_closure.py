"""Fechamento manual/BOT — T1–T8 e integração, em PostgreSQL real.

`MANUALCLOSURE_TEST_SOCKET` aponta para /tmp/cw-mclose-sock.* criado pelo runner.
Driver async real, socket Unix, TCP/DNS bloqueados, cluster descartável UTF-8.
Serviços, SQL, locks e transporte são os REAIS; só a borda externa (HTTP da
exchange) é falsa.

Defeitos reproduzidos na baseline `20580139` (ver
`docs/review_probes/review_20580139_probes.py`):

- T1 reserva devolvia token mas gravava NULL, e o guard liberava;
- T2 época da conta mudava e o guard continuava comparando só a própria linha;
- T3 readmissão sem aumento devolvia o token ANTIGO;
- T4 inversão de locks entre reserva e resolução ⇒ `deadlock detected`;
- T5 INVALIDATED conservava prova e autorizava entrada em outro símbolo;
- T6 escritor atrasado recarimbava a prova de um INVALIDATED;
- T8 observação anterior encerrava reconhecimento criado depois dela.
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

test_socket = os.environ.get("MANUALCLOSURE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-mclose-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://mclose@/mclosedb?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no fechamento manual/bot")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no fechamento manual/bot")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no fechamento manual/bot")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "d" * 64


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


async def run():
    from sqlalchemy import event, func, select, text
    import db
    from models.account_margin_epoch import AccountMarginEpoch as Epoch
    from models.entry_intent import EntryIntent
    from models.execution_incident import ExecutionIncident
    from models.manual_position_ack import ManualPositionAcknowledgement as Ack
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from models.risk_state import RiskState
    from services import binance_signed_service as bss
    from services import entry_intent_service as intents
    from services import execution_reconciliation_service as ers
    from services import manual_position_service as mps
    from services import shadow_trade_service as sts

    # ══════════════════════════════════════════════════════════════════════
    #  0. Migração: upgrade vindo de 20580139 + schema novo + repetição
    # ══════════════════════════════════════════════════════════════════════
    async with db._engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE manual_position_acks (
                id SERIAL PRIMARY KEY,
                account_scope VARCHAR(64) NOT NULL, exchange VARCHAR(20) NOT NULL,
                market VARCHAR(20) NOT NULL, symbol VARCHAR(50) NOT NULL,
                quote VARCHAR(20) NOT NULL, side VARCHAR(8) NOT NULL,
                position_side VARCHAR(10) NOT NULL, qty NUMERIC(38,18) NOT NULL,
                entry_price NUMERIC(38,18) NOT NULL,
                exchange_update_time_ms BIGINT NOT NULL,
                fingerprint VARCHAR(64) NOT NULL,
                contract_version VARCHAR(32) NOT NULL, state VARCHAR(16) NOT NULL,
                reason VARCHAR(200), identity_note VARCHAR(120),
                incident_key VARCHAR(200), evidence JSONB,
                revision INTEGER DEFAULT 0, validated_at_ms BIGINT,
                validation_scope VARCHAR(16), validation_account VARCHAR(64),
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                ended_at TIMESTAMPTZ, ended_reason VARCHAR(120))"""))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_manual_ack_open ON manual_position_acks "
            "(account_scope, exchange, market, symbol) "
            "WHERE state IN ('ACTIVE','INVALIDATED','WAITING_ORDERS')"))
        await conn.execute(text("""
            CREATE TABLE account_margin_epochs (
                id SERIAL PRIMARY KEY, account_scope VARCHAR(64) NOT NULL,
                exchange VARCHAR(20) NOT NULL, market VARCHAR(20) NOT NULL,
                generation BIGINT DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL)"""))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_margin_epoch_identity ON "
            "account_margin_epochs (account_scope, exchange, market)"))
        await conn.execute(text(
            "INSERT INTO manual_position_acks (account_scope, exchange, market, "
            "symbol, quote, side, position_side, qty, entry_price, "
            "exchange_update_time_ms, fingerprint, contract_version, state, "
            "revision, validated_at_ms, created_at, updated_at) VALUES "
            "('legado','binance','usdm_futures','OMEGA/USDT:USDT','USDT','buy',"
            "'BOTH',1,100,1770000000000,'f'||repeat('0',63),'MANUAL_ACK_V1',"
            "'ACTIVE',4,1770000000000, now(), now())"))
        await conn.execute(text(
            "INSERT INTO account_margin_epochs (account_scope, exchange, market, "
            "generation, updated_at) VALUES "
            "('legado','binance','usdm_futures', 9, now())"))
    tabelas = [RecommendationSnapshot.__table__, RealTrade.__table__,
               EntryIntent.__table__, ExecutionIncident.__table__,
               RiskState.__table__, Ack.__table__, Epoch.__table__]
    for _ in range(2):
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tabelas)
    await db.init_db()
    await db.init_db()
    async with db.get_session() as session:
        colunas_ack = {l[0] for l in (await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='manual_position_acks'"))).all()}
        colunas_ep = {l[0] for l in (await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='account_margin_epochs'"))).all()}
        legado = (await session.execute(text(
            "SELECT state, revision, validated_revision, validated_generation "
            "FROM manual_position_acks WHERE account_scope='legado'"))).one()
        ep_legado = (await session.execute(text(
            "SELECT generation, manual_validation_generation, "
            "manual_validation_blocked FROM account_margin_epochs "
            "WHERE account_scope='legado'"))).one()
    check("migracao_adiciona_as_quatro_colunas",
          {"validated_revision", "validated_generation"} <= colunas_ack
          and {"manual_validation_generation", "manual_validation_blocked"} <= colunas_ep,
          f"{sorted(colunas_ack)[:6]} {sorted(colunas_ep)}")
    check("upgrade_preserva_linhas_existentes",
          legado[0] == "ACTIVE" and legado[1] == 4 and legado[2] is None
          and legado[3] is None and ep_legado[0] == 9, str(legado) + str(ep_legado))
    check("legado_nasce_sem_validacao_de_conta",
          ep_legado[1] == 0 and ep_legado[2] is True, str(ep_legado))

    # ── Fakes de borda: exchange e carteira ───────────────────────────────
    EXCHANGE = {"positions": [], "algos": [], "common": [],
                "quality": "ok", "algos_ok": True, "common_ok": True}
    ENVIADOS: list = []

    def posicao(symbol, *, side="Buy", size="2.5", entry="100",
                position_side="BOTH", update_time=1_770_000_000_000):
        return {"symbol": symbol, "side": side, "size": float(size),
                "entry_price": float(entry), "mark_price": 101.0,
                "unrealized_pnl": 0.0, "leverage": 5.0, "position_value": 250.0,
                "take_profit": None, "stop_loss": None,
                "position_side": position_side, "update_time_ms": update_time}

    async def fake_get_positions(symbol=None, force=False, **kwargs):
        if EXCHANGE["quality"] == "stale":
            return {"ok": True, "stale": True, "positions": EXCHANGE["positions"]}
        if EXCHANGE["quality"] == "erro":
            return {"ok": False, "error": "indisponível"}
        linhas = EXCHANGE["positions"]
        if symbol:
            alvo = mps.symbol_key(symbol)
            linhas = [p for p in linhas if mps.symbol_key(p.get("symbol")) == alvo]
        return {"ok": True, "positions": linhas, "count": len(linhas)}

    async def fake_algo_orders(symbol=None, **kwargs):
        if not EXCHANGE["algos_ok"]:
            return {"ok": False, "error": "indisponível"}
        return {"ok": True, "orders": list(EXCHANGE["algos"])}

    async def fake_open_orders(symbol=None, **kwargs):
        if not EXCHANGE["common_ok"]:
            return {"ok": False, "error": "indisponível"}
        return {"ok": True, "orders": list(EXCHANGE["common"])}

    async def fake_request(method, url):
        ENVIADOS.append((method, url.split("?")[0]))
        return SimpleNamespace(
            status_code=200, headers={},
            json=lambda: {"orderId": 11, "status": "FILLED", "executedQty": "1",
                          "avgPrice": "100", "clientOrderId": "cw-final"})

    bss._API_KEY, bss._API_SECRET = "sintetica", "sintetica"
    patches = [patch.object(bss, "accounting_scope", lambda: ESCOPO),
               patch.object(bss, "get_positions", fake_get_positions),
               patch.object(bss, "get_open_algo_orders", fake_algo_orders),
               patch.object(bss, "get_open_orders", fake_open_orders),
               patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)),
               patch.object(bss, "_get_symbol_filters", AsyncMock(return_value={
                   "step": 0.001, "min_qty": 0.001, "max_qty": 1e9,
                   "min_notional": 5.0, "market_step": 0.001,
                   "market_min_qty": 0.001, "market_max_qty": 1e9,
                   "tick": 0.01})),
               patch.object(bss, "_floor_to_step",
                            lambda valor, passo: float(valor)),
               patch.object(bss, "_round_price",
                            AsyncMock(side_effect=lambda _s, p: p)),
               patch.object(bss, "_build_signed_url",
                            side_effect=lambda path, params=None:
                            "https://sintetico.invalido" + path),
               patch.object(bss, "_get_client",
                            return_value=SimpleNamespace(request=fake_request))]
    for item in patches:
        item.start()
    ers.set_repo(None)
    ers._boot_scan_safe = True

    def identidade(symbol_guardado, trigger, *, conta=ESCOPO):
        return intents.EntryIdentity(
            account_ref=conta, exchange="binance", symbol=symbol_guardado,
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)

    async def geracao():
        async with db.get_session() as session:
            valor = await intents.current_margin_generation(
                session, account_ref=ESCOPO, exchange="binance",
                market="usdm_futures")
            await session.commit()
            return valor

    async def carteira(requerido, *, disponivel=1_000.0, generation=None):
        g = generation if generation is not None else await geracao()
        agora = ms()
        return intents.MarginGate(
            available_usd=disponivel, required_usd=requerido, as_of_ms=agora,
            observed_start_ms=agora - 5, observed_end_ms=agora, quality="live",
            complete=True, account_ref=ESCOPO, exchange="binance",
            market="usdm_futures", generation=g)

    async def linha_intencao(chave):
        return await intents.get_intent(db.get_session, chave)

    async def desbloquear_conta():
        """Conta validada por uma observação ACCOUNT completa (ciclo real)."""
        contexto = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
        observacao = await mps.observe_positions()
        return await mps.revalidate_active(observation=observacao, context=contexto)

    # ══════════════════════════════════════════════════════════════════════
    #  T1 — token persistido; NULL/ausente negam o envio
    # ══════════════════════════════════════════════════════════════════════
    await desbloquear_conta()
    gate_a = await carteira(60.0)
    a = await intents.reserve(db.get_session, identidade("ALFA-USDT-USDT", 1000),
                              {"entry": 100.0, "stop_loss": 95.0},
                              owner=sts._INTENT_OWNER, margin=gate_a)
    check("t1_reserva_concedida", a.granted, str(a))
    linha_a = await linha_intencao(a.intent_key)
    check("t1_token_persistido_na_linha",
          linha_a.margin_generation is not None
          and int(linha_a.margin_generation) == int(a.generation),
          f"retornado={a.generation} persistido={linha_a.margin_generation}")
    assert await intents.mark_sending(db.get_session, a.intent_key,
                                      owner=sts._INTENT_OWNER)
    assert await intents.register_dispatch(db.get_session, a.intent_key,
                                           owner=sts._INTENT_OWNER,
                                           dispatch_id=a.client_order_id)
    ok_token = await intents.authorize_dispatch(
        db.get_session, a.intent_key, owner=sts._INTENT_OWNER,
        expected_token=a.generation, dispatch_id=a.client_order_id)
    check("t1_token_correto_autoriza", ok_token["ok"], str(ok_token)[:200])
    for nome, token in (("nulo", None), ("bool", True), ("texto", "2"),
                        ("divergente", int(a.generation) + 5)):
        veredito = await intents.authorize_dispatch(
            db.get_session, a.intent_key, owner=sts._INTENT_OWNER,
            expected_token=token, dispatch_id=a.client_order_id)
        check(f"t1_token_{nome}_nega", veredito["ok"] is False, str(veredito)[:160])
    ausente = await intents.authorize_dispatch(
        db.get_session, "intencao-inexistente", owner=sts._INTENT_OWNER,
        expected_token=0, dispatch_id="x")
    check("t1_linha_ausente_nega", ausente["ok"] is False, str(ausente)[:160])
    conta_errada = await intents.authorize_dispatch(
        db.get_session, a.intent_key, owner="outro-dono",
        expected_token=a.generation, dispatch_id=a.client_order_id)
    check("t1_owner_errado_nega", conta_errada["ok"] is False, str(conta_errada)[:160])
    dispatch_errado = await intents.authorize_dispatch(
        db.get_session, a.intent_key, owner=sts._INTENT_OWNER,
        expected_token=a.generation, dispatch_id="dispatch-que-nao-existe")
    check("t1_dispatch_divergente_nega", dispatch_errado["ok"] is False,
          str(dispatch_errado)[:160])

    # ══════════════════════════════════════════════════════════════════════
    #  T2 — mudança ALHEIA de época é detectada na fronteira
    # ══════════════════════════════════════════════════════════════════════
    gate_b = await carteira(20.0)
    b = await intents.reserve(db.get_session, identidade("BETA-USDT-USDT", 2000),
                              {"entry": 100.0, "stop_loss": 95.0},
                              owner="outro", margin=gate_b)
    check("t2_reserva_alheia_avanca_a_epoca",
          b.granted and int(b.generation) != int(a.generation),
          f"a={a.generation} b={b.generation}")
    apos_alheio = await intents.authorize_dispatch(
        db.get_session, a.intent_key, owner=sts._INTENT_OWNER,
        expected_token=a.generation, dispatch_id=a.client_order_id)
    check("t2_token_de_a_fica_superado",
          apos_alheio["ok"] is False
          and apos_alheio["reason_code"] == intents.MARGIN_SUPERSEDED,
          str(apos_alheio)[:200])

    # ══════════════════════════════════════════════════════════════════════
    #  T3 — readmissão SEM aumento persiste e devolve a época atual
    # ══════════════════════════════════════════════════════════════════════
    atual = await geracao()
    readmitida = await intents.admit_final_risk(
        db.get_session, a.intent_key, owner=sts._INTENT_OWNER, risk_usd=0.0,
        margin=await carteira(60.0, generation=atual))
    check("t3_readmissao_sem_aumento_aprova", readmitida.granted, str(readmitida))
    check("t3_readmissao_devolve_a_epoca_atual",
          int(readmitida.generation) == int(atual),
          f"atual={atual} devolvido={readmitida.generation}")
    linha_a = await linha_intencao(a.intent_key)
    check("t3_token_atual_persistido",
          int(linha_a.margin_generation) == int(atual), str(linha_a.margin_generation))
    depois = await intents.authorize_dispatch(
        db.get_session, a.intent_key, owner=sts._INTENT_OWNER,
        expected_token=readmitida.generation, dispatch_id=a.client_order_id)
    check("t3_apos_readmitir_o_envio_e_autorizado", depois["ok"], str(depois)[:200])
    # Aumento PRÓPRIO gera token NOVO.
    antes_aumento = await geracao()
    com_aumento = await intents.admit_final_risk(
        db.get_session, a.intent_key, owner=sts._INTENT_OWNER, risk_usd=0.0,
        margin=await carteira(90.0, generation=antes_aumento))
    check("t3_aumento_proprio_gera_token_novo",
          com_aumento.granted and int(com_aumento.generation) > int(antes_aumento),
          f"{antes_aumento} → {com_aumento.generation}")
    # Mero refresh/idempotência NÃO faz bump.
    antes_refresh = await geracao()
    await intents.mark_sending(db.get_session, a.intent_key, owner=sts._INTENT_OWNER)
    await intents.register_dispatch(db.get_session, a.intent_key,
                                    owner=sts._INTENT_OWNER,
                                    dispatch_id=a.client_order_id)
    await intents.recover_stale(db.get_session)
    check("t3_repeticao_nao_incrementa_epoca",
          await geracao() == antes_refresh, str(antes_refresh))

    # ══════════════════════════════════════════════════════════════════════
    #  T4 — ordem única de locks: três corridas, duas conexões, sem deadlock
    # ══════════════════════════════════════════════════════════════════════
    erros_db: list = []

    def anotar_erro(ctx):
        erros_db.append(str(ctx.original_exception))

    event.listen(db._engine.sync_engine, "handle_error", anotar_erro)

    async def corrida(nome_leitor, leitor, nome_escritor, escritor):
        """Barreira no método REAL: o leitor pausa depois de ler a época."""
        epoca_lida, escritor_pronto = asyncio.Event(), asyncio.Event()
        original = intents.current_margin_generation

        async def pausa_apos_epoca(*args, **kwargs):
            valor = await original(*args, **kwargs)
            if asyncio.current_task().get_name() == nome_leitor:
                epoca_lida.set()
                await asyncio.wait_for(escritor_pronto.wait(), timeout=8)
            return valor

        with patch.object(intents, "current_margin_generation", pausa_apos_epoca):
            tarefa_leitor = asyncio.create_task(leitor(), name=nome_leitor)
            await asyncio.wait_for(epoca_lida.wait(), timeout=8)
            tarefa_escritor = asyncio.create_task(
                _escritor_sinalizando(escritor, escritor_pronto), name=nome_escritor)
            return await asyncio.wait_for(
                asyncio.gather(tarefa_leitor, tarefa_escritor), timeout=20)

    async def _escritor_sinalizando(escritor, pronto):
        pronto.set()
        await asyncio.sleep(0.05)
        return await escritor()

    # (a) reserva × resolução
    gate_c = await carteira(10.0)
    res_c, fim_c = await corrida(
        "fc-reserve",
        lambda: intents.reserve(db.get_session, identidade("GAMA-USDT-USDT", 3000),
                                {"entry": 100.0, "stop_loss": 95.0},
                                owner=sts._INTENT_OWNER, margin=gate_c),
        "fc-resolve",
        lambda: intents.mark_terminal(db.get_session, b.intent_key, owner="outro"))
    check("t4_reserva_x_resolucao_sem_deadlock",
          not any("deadlock detected" in e for e in erros_db),
          str(erros_db)[:200])
    check("t4_reserva_x_resolucao_conclui",
          res_c.decision != intents.UNAVAILABLE and fim_c is True,
          f"{res_c.decision} {fim_c}")

    # (b) readmissão × confirmação
    erros_db.clear()
    async with db.get_session() as session:
        trade = RealTrade(symbol="GAMA/USDT:USDT", side="long", source="auto",
                          exchange="binance", qty=1.0, entry_price=100.0,
                          status="open", opened_at=datetime.now(timezone.utc),
                          planned_stop=95.0)
        session.add(trade)
        await session.commit()
        trade_id = trade.id
    gate_d = await carteira(15.0)
    res_d, fim_d = await corrida(
        "fc-readmit",
        lambda: intents.admit_final_risk(db.get_session, res_c.intent_key,
                                         owner=sts._INTENT_OWNER, risk_usd=0.0,
                                         margin=gate_d),
        "fc-confirm",
        lambda: intents.mark_confirmed(db.get_session, a.intent_key,
                                       owner=sts._INTENT_OWNER,
                                       real_trade_id=trade_id))
    check("t4_readmissao_x_confirmacao_sem_deadlock",
          not any("deadlock detected" in e for e in erros_db), str(erros_db)[:200])
    check("t4_readmissao_x_confirmacao_conclui", fim_d is True,
          f"{res_d.decision} {fim_d}")

    # (c) liberação × retomada
    erros_db.clear()
    gate_e = await carteira(5.0)
    livre = await intents.reserve(db.get_session, identidade("DELTA-USDT-USDT", 4000),
                                  {"entry": 100.0, "stop_loss": 95.0},
                                  owner="solto", margin=gate_e)
    check("t4_reserva_para_liberacao", livre.granted, str(livre))
    gate_f = await carteira(5.0)
    res_f, fim_f = await corrida(
        "fc-retomada",
        lambda: intents.reserve(db.get_session, identidade("EPS-USDT-USDT", 5000),
                                {"entry": 100.0, "stop_loss": 95.0},
                                owner=sts._INTENT_OWNER, margin=gate_f),
        "fc-liberacao",
        lambda: intents.release_reserved(db.get_session, livre.intent_key,
                                         owner="solto"))
    check("t4_liberacao_x_retomada_sem_deadlock",
          not any("deadlock detected" in e for e in erros_db), str(erros_db)[:200])
    check("t4_liberacao_x_retomada_conclui",
          res_f.decision != intents.UNAVAILABLE and fim_f is True,
          f"{res_f.decision} {fim_f}")
    event.remove(db._engine.sync_engine, "handle_error", anotar_erro)
    async with db.get_session() as session:
        perdidas = int((await session.execute(
            select(func.count(EntryIntent.intent_key))
            .where(EntryIntent.state.in_(("RESERVED", "SENDING", "UNKNOWN")),
                   EntryIntent.reserved_margin_usd.is_(None)))).scalar() or 0)
    check("t4_nenhuma_reserva_perdida", perdidas == 0, str(perdidas))

    # ══════════════════════════════════════════════════════════════════════
    #  T5/T6 — prova revogada no mesmo commit; escritor atrasado perde CAS
    # ══════════════════════════════════════════════════════════════════════
    async def novo_ack(symbol, *, estado="ACTIVE", revisao=0, qty="2.5"):
        """Reconhecimento com o fingerprint REAL da posição sintética."""
        agora = datetime.now(timezone.utc)
        impressao = mps.position_fingerprint(
            account_scope=ESCOPO, exchange="binance", market="usdm_futures",
            symbol=symbol, side="Buy", position_side="BOTH", qty=qty,
            entry_price="100", update_time_ms=1_770_000_000_000,
            contract_version=mps._contract_version())
        assert impressao, "fingerprint sintético inválido"
        async with db.get_session() as session:
            linha = Ack(account_scope=ESCOPO, exchange="binance",
                        market="usdm_futures", symbol=symbol, quote="USDT",
                        side="buy", position_side="BOTH", qty=qty,
                        entry_price=100,
                        exchange_update_time_ms=1_770_000_000_000,
                        fingerprint=impressao, contract_version="MANUAL_ACK_V1",
                        state=estado, revision=revisao, created_at=agora,
                        updated_at=agora)
            session.add(linha)
            await session.commit()
            return linha.id

    async with db.get_session() as session:
        await session.execute(text("DELETE FROM manual_position_acks "
                                   "WHERE account_scope = :s"), {"s": ESCOPO})
        await session.commit()
    EXCHANGE["positions"] = [posicao("GAMAUSDT")]
    id_gama = await novo_ack("GAMA/USDT:USDT")
    validado = await desbloquear_conta()
    check("t5_prova_publicada_para_ativo",
          validado["ok"] and id_gama in validado["valid"], str(validado)[:220])
    async with db.get_session() as session:
        prova = (await session.execute(text(
            "SELECT state, revision, validated_revision, validated_generation, "
            "validated_at_ms FROM manual_position_acks WHERE id = :i"),
            {"i": id_gama})).one()
    check("t5_prova_grava_revisao_e_epoca",
          prova[2] is not None and int(prova[2]) == int(prova[1])
          and prova[3] is not None and prova[4] is not None, str(prova))
    guarda_outro = await mps.ownership_guard("ZETA/USDT:USDT", action="entry",
                                             require_fresh_proof=True)
    check("t5_com_prova_valida_outro_simbolo_opera", guarda_outro["allowed"],
          str(guarda_outro))

    # Identidade muda ⇒ INVALIDATED E prova revogada no MESMO commit.
    EXCHANGE["positions"] = [posicao("GAMAUSDT", size="9.0")]
    contexto_inv = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    obs_inv = await mps.observe_positions()
    await mps.revalidate_active(observation=obs_inv, context=contexto_inv)
    async with db.get_session() as session:
        depois_inv = (await session.execute(text(
            "SELECT state, validated_at_ms, validated_revision, "
            "validated_generation, validation_scope, validation_account "
            "FROM manual_position_acks WHERE id = :i"), {"i": id_gama})).one()
    check("t5_invalidado_revoga_a_prova_no_mesmo_commit",
          depois_inv[0] == "INVALIDATED" and all(v is None for v in depois_inv[1:]),
          str(depois_inv))
    guarda_pos_inv = await mps.ownership_guard("ZETA/USDT:USDT", action="entry",
                                               require_fresh_proof=True)
    check("t5_entrada_alheia_bloqueada_apos_invalidar",
          guarda_pos_inv["allowed"] is False, str(guarda_pos_inv))

    # T6 — escritor ATRASADO (revisão antiga) não recarimba o invalidado.
    contexto_velho = {**contexto_inv, "acks": [
        {**linha, "state": "ACTIVE"} for linha in contexto_inv["acks"]]}
    recarimbo = await mps.publish_validation_proof(
        context=contexto_velho, observation=obs_inv,
        validated_ids=[id_gama], validated_at_ms=ms())
    async with db.get_session() as session:
        pos_recarimbo = (await session.execute(text(
            "SELECT state, validated_at_ms FROM manual_position_acks "
            "WHERE id = :i"), {"i": id_gama})).one()
    check("t6_escritor_atrasado_perde_cas",
          recarimbo.get("ok") is False or id_gama not in (recarimbo.get("valid") or []),
          str(recarimbo)[:200])
    check("t6_invalidado_nao_recebe_prova",
          pos_recarimbo[0] == "INVALIDATED" and pos_recarimbo[1] is None,
          str(pos_recarimbo))
    # Fingerprint voltar a coincidir NÃO reativa automaticamente.
    EXCHANGE["positions"] = [posicao("GAMAUSDT")]
    contexto_volta = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    obs_volta = await mps.observe_positions()
    volta = await mps.revalidate_active(observation=obs_volta, context=contexto_volta)
    async with db.get_session() as session:
        estado_volta = (await session.execute(text(
            "SELECT state, validated_at_ms FROM manual_position_acks "
            "WHERE id = :i"), {"i": id_gama})).one()
    check("t6_fingerprint_coincidente_nao_reativa",
          estado_volta[0] == "INVALIDATED" and estado_volta[1] is None
          and id_gama not in (volta.get("valid") or []), str(estado_volta))

    # ══════════════════════════════════════════════════════════════════════
    #  T8 — observação anterior não encerra reconhecimento mais novo
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM manual_position_acks "
                                   "WHERE account_scope = :s"), {"s": ESCOPO})
        await session.commit()
    EXCHANGE["positions"] = []
    contexto_antigo = await mps.capture_validation_context(
        scope=mps.SCOPE_SYMBOL, symbol="EPSILON/USDT:USDT")
    observacao_antiga = await mps.observe_positions("EPSILON/USDT:USDT")
    id_novo = await novo_ack("EPSILON/USDT:USDT")        # nasce DEPOIS do GET
    veredito_antigo = await mps.revalidate_active(observation=observacao_antiga,
                                                  context=contexto_antigo)
    async with db.get_session() as session:
        estado_novo = (await session.execute(text(
            "SELECT state FROM manual_position_acks WHERE id = :i"),
            {"i": id_novo})).scalar()
    check("t8_observacao_antiga_nao_encerra_ack_novo",
          estado_novo == "ACTIVE", str(estado_novo))
    check("t8_contexto_vencido_e_reportado",
          veredito_antigo.get("reason_code") == mps.STALE_CONTEXT
          or id_novo not in (veredito_antigo.get("closed") or []),
          str(veredito_antigo)[:200])
    guarda_novo = await mps.ownership_guard("EPSILON/USDT:USDT", action="entry")
    check("t8_simbolo_do_ack_novo_continua_bloqueado",
          guarda_novo["allowed"] is False, str(guarda_novo))
    check("t8_stale_context_nao_arma_pausa_global",
          (await mps.account_validation_state())["blocked"] is False
          or veredito_antigo.get("reason_code") == mps.STALE_CONTEXT,
          str(veredito_antigo)[:160])

    # Mudança DURANTE a consulta de ordens também perde CAS.
    EXCHANGE["positions"] = [posicao("EPSILONUSDT")]
    await desbloquear_conta()
    EXCHANGE["positions"] = []
    contexto_ordens = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_ordens = await mps.observe_positions()
    original_ordens = mps.symbol_has_live_orders

    async def ordens_com_mudanca(symbol):
        async with db.get_session() as session:
            await session.execute(text(
                "UPDATE manual_position_acks SET revision = revision + 1, "
                "updated_at = now() WHERE id = :i"), {"i": id_novo})
            await session.commit()
        return await original_ordens(symbol)

    with patch.object(mps, "symbol_has_live_orders", ordens_com_mudanca):
        durante = await mps.revalidate_active(observation=observacao_ordens,
                                              context=contexto_ordens)
    async with db.get_session() as session:
        estado_durante = (await session.execute(text(
            "SELECT state FROM manual_position_acks WHERE id = :i"),
            {"i": id_novo})).scalar()
    check("t8_mudanca_durante_a_consulta_de_ordens_perde_cas",
          estado_durante != "CLOSED", f"{estado_durante} {str(durante)[:160]}")

    # ══════════════════════════════════════════════════════════════════════
    #  §7.1 — falha conhecida bloqueia IMEDIATAMENTE e sobrevive a restart
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM manual_position_acks "
                                   "WHERE account_scope = :s"), {"s": ESCOPO})
        await session.execute(text("DELETE FROM execution_incidents"))
        await session.commit()
    EXCHANGE["positions"] = [posicao("GAMAUSDT")]
    id_falha = await novo_ack("GAMA/USDT:USDT")
    await desbloquear_conta()
    estado_ok = await mps.account_validation_state()
    check("f1_conta_desbloqueada_apos_ciclo_completo",
          estado_ok["blocked"] is False, str(estado_ok))
    guarda_ok = await mps.ownership_guard("ZETA/USDT:USDT", action="entry",
                                          require_fresh_proof=True)
    check("f1_entrada_permitida_com_conta_validada", guarda_ok["allowed"],
          str(guarda_ok))
    epoca_manual_antes = estado_ok["generation"]

    # Leitura EXTERNA stale: bloqueia na hora, mesmo com TTL ainda válido.
    EXCHANGE["quality"] = "stale"
    contexto_falha = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_falha = await mps.observe_positions()
    veredito_falha = await mps.revalidate_active(observation=observacao_falha,
                                                 context=contexto_falha)
    estado_falha = await mps.account_validation_state()
    check("f1_leitura_stale_bloqueia_na_hora",
          veredito_falha["ok"] is False and estado_falha["blocked"] is True
          and estado_falha["generation"] > epoca_manual_antes,
          f"{veredito_falha.get('reason_code')} {estado_falha}")
    async with db.get_session() as session:
        prova_revogada = (await session.execute(text(
            "SELECT validated_at_ms, validated_revision, validated_generation "
            "FROM manual_position_acks WHERE id = :i"), {"i": id_falha})).one()
    check("f1_falha_revoga_a_prova_alcancada",
          all(v is None for v in prova_revogada), str(prova_revogada))
    guarda_bloqueado = await mps.ownership_guard("ZETA/USDT:USDT", action="entry",
                                                 require_fresh_proof=True)
    check("f1_entrada_bloqueada_imediatamente",
          guarda_bloqueado["allowed"] is False
          and guarda_bloqueado["reason_code"] == mps.GUARD_ACCOUNT_BLOCKED,
          str(guarda_bloqueado))
    gate_bloq = await carteira(10.0)
    reserva_bloqueada = await intents.reserve(
        db.get_session, identidade("ZETA-USDT-USDT", 6000),
        {"entry": 100.0, "stop_loss": 95.0}, owner=sts._INTENT_OWNER,
        margin=gate_bloq)
    check("f1_admissao_tambem_recusa_com_conta_bloqueada",
          reserva_bloqueada.granted is False, str(reserva_bloqueada))

    # Leitor que começou ANTES da falha não repara a autorização antiga.
    anterior = await mps.revalidate_active(observation=observacao_ordens,
                                           context=contexto_falha)
    check("f1_leitor_anterior_nao_restaura",
          anterior["ok"] is False
          and (await mps.account_validation_state())["blocked"] is True,
          str(anterior)[:200])

    # Restart continua fechado; TTL antigo não recupera a permissão.
    await db._engine.dispose()
    mps.reset_local_validation_state()           # simula processo NOVO
    estado_restart = await mps.account_validation_state()
    check("f1_restart_continua_fechado", estado_restart["blocked"] is True,
          str(estado_restart))
    guarda_restart = await mps.ownership_guard("ZETA/USDT:USDT", action="entry",
                                               require_fresh_proof=True)
    check("f1_restart_mantem_entrada_bloqueada",
          guarda_restart["allowed"] is False, str(guarda_restart))

    # Zero incidentes NÃO remove a causa manual pendente.
    ers._p03_latch_armed = True
    ers._boot_scan_safe = True
    liberou = await ers._maybe_release_quarantine()
    check("f1_zero_incidentes_nao_libera_com_causa_manual",
          liberou is False, str(liberou))

    # Falha de COMMIT: fence local segura mesmo sem época durável avançada.
    EXCHANGE["quality"] = "ok"
    await desbloquear_conta()
    check("f2_conta_recuperada_para_o_proximo_caso",
          (await mps.account_validation_state())["blocked"] is False)
    epoca_antes_commit = (await mps.account_validation_state())["generation"]
    EXCHANGE["quality"] = "erro"
    contexto_c = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_c = await mps.observe_positions()
    original_persistencia = mps._persist_validation_failure

    async def commit_quebrado(*args, **kwargs):
        return {"ok": False, "reason_code": "DB_UNAVAILABLE"}

    with patch.object(mps, "_persist_validation_failure", commit_quebrado):
        falha_commit = await mps.revalidate_active(observation=observacao_c,
                                                   context=contexto_c)
    estado_commit = await mps.account_validation_state()
    check("f2_falha_de_commit_retorna_unknown", falha_commit["ok"] is False,
          str(falha_commit)[:200])
    check("f2_fence_local_bloqueia_sem_commit",
          estado_commit["blocked"] is True
          and estado_commit.get("pending_failure") is True,
          str(estado_commit))
    check("f2_epoca_duravel_pode_nao_ter_avancado",
          estado_commit["generation"] == epoca_antes_commit
          or estado_commit["generation"] > epoca_antes_commit,
          str(estado_commit))
    # Um GET ANTERIOR à falha não publica nem recupera.
    EXCHANGE["quality"] = "ok"
    nao_recupera = await mps.revalidate_active(observation=observacao_c,
                                               context=contexto_c)
    check("f2_get_anterior_nao_recupera",
          nao_recupera["ok"] is False
          and (await mps.account_validation_state())["blocked"] is True,
          str(nao_recupera)[:200])
    # Banco de volta: PRIMEIRO persiste a falha pendente, depois um GET NOVO.
    persistiu = await mps.flush_pending_validation_failure()
    estado_pos_flush = await mps.account_validation_state()
    check("f2_falha_pendente_e_persistida_primeiro",
          persistiu.get("ok") is True
          and estado_pos_flush["generation"] > epoca_antes_commit
          and estado_pos_flush["blocked"] is True, str(estado_pos_flush))
    recuperado = await desbloquear_conta()
    estado_final = await mps.account_validation_state()
    check("f2_so_um_get_novo_recupera",
          recuperado["ok"] and estado_final["blocked"] is False,
          f"{str(recuperado)[:160]} {estado_final}")

    # ══════════════════════════════════════════════════════════════════════
    #  §7.2 — recuperação só da causa própria; SYMBOL não libera ACCOUNT
    # ══════════════════════════════════════════════════════════════════════
    EXCHANGE["quality"] = "stale"
    contexto_s = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    await mps.revalidate_active(observation=await mps.observe_positions(),
                                context=contexto_s)
    check("r1_conta_bloqueada_de_novo",
          (await mps.account_validation_state())["blocked"] is True)
    EXCHANGE["quality"] = "ok"
    contexto_sym = await mps.capture_validation_context(
        scope=mps.SCOPE_SYMBOL, symbol="GAMA/USDT:USDT")
    obs_sym = await mps.observe_positions("GAMA/USDT:USDT")
    sym = await mps.revalidate_active(observation=obs_sym, context=contexto_sym)
    check("r1_symbol_nao_limpa_bloqueio_de_conta",
          (await mps.account_validation_state())["blocked"] is True,
          str(sym)[:200])
    conta_nova = await desbloquear_conta()
    check("r1_account_completo_recupera",
          conta_nova["ok"] and (await mps.account_validation_state())["blocked"] is False,
          str(conta_nova)[:200])
    # Pausas/owners alheios preservados.
    async with db.get_session() as session:
        session.add(ExecutionIncident(
            incident_key="binance:UNTRACKED_POSITION:OUTRO/USDT:USDT:x",
            exchange="binance", symbol="OUTRO/USDT:USDT",
            kind="UNTRACKED_POSITION", state="MANUAL_REQUIRED",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)))
        await session.commit()
    ers._p03_latch_armed = True
    liberou_outro = await ers._maybe_release_quarantine()
    async with db.get_session() as session:
        sobreviveu = int((await session.execute(
            select(func.count(ExecutionIncident.id))
            .where(ExecutionIncident.resolved_at.is_(None)))).scalar() or 0)
    check("r1_recuperacao_preserva_incidente_alheio",
          liberou_outro is False and sobreviveu == 1,
          f"{liberou_outro} {sobreviveu}")
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM execution_incidents"))
        await session.commit()

    # ══════════════════════════════════════════════════════════════════════
    #  Caller → transporte REAL → cliente HTTP falso
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM entry_intents"))
        await session.execute(text("DELETE FROM manual_position_acks "
                                   "WHERE account_scope = :s"), {"s": ESCOPO})
        await session.commit()
    EXCHANGE["positions"] = []
    await desbloquear_conta()

    def rec_sintetica(symbol="OMEGA/USDT:USDT"):
        return {"symbol": symbol, "timeframe": "4h", "playbook": "CHAMPION_LEGACY",
                "leverage": 5, "signal": {}, "score_provenance": {},
                "data_freshness": {"candle": {"close_time_ms": 1_770_000_600_000}}}

    async def caminho_completo(*, mudar_epoca_no_throttle=False,
                               mudar_epoca_no_preflight=False,
                               symbol="OMEGA/USDT:USDT"):
        """Reserva → SENDING/dispatch → preflight/readmissão → guard final."""
        ENVIADOS.clear()
        with patch.object(bss, "accounting_scope", lambda: ESCOPO), \
                patch.object(sts, "_open_risk_usd", AsyncMock(return_value=0.0)), \
                patch.object(sts, "_free_margin_snapshot",
                             AsyncMock(return_value={
                                 "available_usd": 5_000.0, "as_of_ms": ms(),
                                 "observed_start_ms": ms(), "observed_end_ms": ms(),
                                 "quality": "live", "source": "live"})):
            intent = await sts._reserve_entry_intent(
                rec_sintetica(symbol), side="long", entry=100.0, stop=95.0,
                tp1=105.0, tp2=110.0, qty=1.0, equity_usd=10_000.0)
            if not intent.get("granted"):
                return intent, None
            dispatch_id = intent["client_order_id"]

            async def preflight(qty, regras):
                # Readmissão REAL com carteira nova (renova o token).
                recusa = await sts._admit_final_entry_risk(
                    {"ok": True, "budget": None}, intent, {},
                    final_entry=100.0, final_qty=float(qty))
                if recusa is not None:
                    return recusa
                if mudar_epoca_no_preflight:
                    # Mudança ALHEIA DEPOIS da readmissão: a autorização final
                    # tem de detectar e negar.
                    await intents.reserve(
                        db.get_session, identidade("RUIDO-USDT-USDT", 9100),
                        {"entry": 100.0, "stop_loss": 95.0}, owner="ruido",
                        margin=await carteira(1.0))
                # Preços do P04B que o transporte confere no MIN_NOTIONAL.
                return {"ok": True, "quality": "OK", "approved_qty": float(qty),
                        "checks": {"best_executable_price": 100.0,
                                   "vwap_price": 100.0, "worst_price": 100.0}}

            # ROTA REAL: preparação + dispatch + preflight/readmissão dentro do
            # `_intent_guarded_preflight`, e AUTORIZAÇÃO FINAL interna composta
            # no `_signed_request` depois do throttle e do ownership.
            async def enviar():
                return await bss.place_order(
                    symbol, "BUY", 1.0, order_type="Market",
                    client_order_id=dispatch_id,
                    final_authorization=sts._intent_final_authorization(
                        intent, dispatch_id_fn=lambda: dispatch_id),
                    entry_preflight=sts._intent_guarded_preflight(
                        intent, preflight, dispatch_id_fn=lambda: dispatch_id))

            if mudar_epoca_no_throttle:
                original_sleep = asyncio.sleep

                async def dorme_e_muda(delay, *a, **k):
                    await intents.reserve(
                        db.get_session, identidade("RUIDO2-USDT-USDT", 9200),
                        {"entry": 100.0, "stop_loss": 95.0}, owner="ruido2",
                        margin=await carteira(1.0))
                    bss._throttle_until_ms = 0
                    return await original_sleep(0)

                with patch.object(bss, "_throttle_until_ms", ms() + 1_000), \
                        patch.object(bss.asyncio, "sleep", dorme_e_muda):
                    envio = await enviar()
            else:
                envio = await enviar()
        return intent, envio

    intent_pos, envio_pos = await caminho_completo()
    check("int_caminho_positivo_reserva_concedida",
          intent_pos.get("granted") is True, str(intent_pos)[:200])
    check("int_caminho_positivo_envia_uma_ordem",
          len(ENVIADOS) == 1 and ENVIADOS[0][0] == "POST", str(ENVIADOS))
    check("int_caminho_positivo_nao_foi_bloqueado",
          envio_pos is not None and envio_pos.get("manual_ownership_blocked") is not True,
          str(envio_pos)[:200])
    async with db.get_session() as session:
        token_final = (await session.execute(
            select(EntryIntent.margin_generation)
            .where(EntryIntent.intent_key == intent_pos["intent_key"]))).scalar()
    check("int_token_final_persistido", token_final is not None, str(token_final))

    # Token SUPERADO antes do preflight: a PREPARAÇÃO deixa renovar, a
    # readmissão real passa e o envio acontece com o token ATUAL (§9).
    intent_t, envio_t = await caminho_completo(mudar_epoca_no_throttle=True,
                                               symbol="OMEGA2/USDT:USDT")
    check("int_token_superado_antes_do_preflight_e_renovado",
          len(ENVIADOS) == 1 and envio_t is not None and envio_t.get("ok") is True,
          f"{ENVIADOS} {str(envio_t)[:160]}")
    async with db.get_session() as session:
        token_renovado, epoca_atual = (await session.execute(text(
            "SELECT i.margin_generation, e.generation FROM entry_intents i "
            "JOIN account_margin_epochs e ON e.account_scope = i.account_ref "
            "WHERE i.intent_key = :k"), {"k": intent_t["intent_key"]})).one()
    check("int_token_renovado_igual_a_epoca_vigente",
          int(token_renovado) == int(epoca_atual),
          f"{token_renovado} vs {epoca_atual}")

    # Mudança ALHEIA DEPOIS da readmissão: a autorização final nega, zero POST.
    intent_p, envio_p = await caminho_completo(mudar_epoca_no_preflight=True,
                                               symbol="OMEGA3/USDT:USDT")
    check("int_mudanca_apos_readmissao_zera_envios", ENVIADOS == [], str(ENVIADOS))
    check("int_mudanca_apos_readmissao_nega_com_motivo",
          envio_p is not None and envio_p.get("ok") is not True
          and intents.MARGIN_SUPERSEDED in str(envio_p),
          str(envio_p)[:250])

    # Reconhecimento criado DURANTE o throttle: zero requisição mutante.
    ENVIADOS.clear()
    EXCHANGE["positions"] = [posicao("OMEGA4USDT")]
    id_omega = await novo_ack("OMEGA4/USDT:USDT")
    intent_m, envio_m = await caminho_completo(symbol="OMEGA4/USDT:USDT")
    check("int_simbolo_reconhecido_nao_reserva_nem_envia",
          intent_m.get("granted") is not True and ENVIADOS == [],
          f"{str(intent_m)[:160]} {ENVIADOS}")

    # T7 pelo caller REAL de fechamento BOT em outro símbolo.
    async with db.get_session() as session:
        await session.execute(text(
            "UPDATE manual_position_acks SET validated_at_ms = :v "
            "WHERE id = :i"), {"v": ms() - int((mps.VALIDATION_MAX_AGE_S + 10) * 1000),
                               "i": id_omega})
        await session.commit()
    ENVIADOS.clear()
    reducao = await bss.place_order("THETA/USDT:USDT", "SELL", 1.0,
                                    order_type="Market", reduce_only=True,
                                    leverage=None, client_order_id="cw-fecha")
    check("t7_reducao_bot_em_outro_simbolo_passa",
          len(ENVIADOS) == 1 and reducao.get("reason_code") != mps.GUARD_PROOF_STALE,
          f"{ENVIADOS} {str(reducao)[:160]}")
    ENVIADOS.clear()
    entrada_bloqueada = await bss.place_order(
        "THETA/USDT:USDT", "BUY", 1.0, order_type="Market", entry_preflight=None,
        leverage=None, client_order_id="cw-entrada-bloq")
    check("t7_entrada_nova_continua_bloqueada",
          entrada_bloqueada.get("ok") is False and ENVIADOS == [],
          f"{str(entrada_bloqueada)[:160]} {ENVIADOS}")
    ENVIADOS.clear()
    manual_dois_lados = []
    for lado, reduz in (("SELL", True), ("BUY", False)):
        res = await bss.place_order("OMEGA4/USDT:USDT", lado, 1.0,
                                    order_type="Market", reduce_only=reduz,
                                    leverage=None, client_order_id="cw-man")
        manual_dois_lados.append(res.get("reason_code"))
    check("t7_simbolo_manual_bloqueia_ambos_os_lados",
          ENVIADOS == []
          and all(c == mps.GUARD_MANUAL_SYMBOL for c in manual_dois_lados),
          f"{manual_dois_lados} {ENVIADOS}")

    for item in patches:
        item.stop()
    await db._engine.dispose()
    print(f"MANUAL_CLOSURE_PG_OK: {len(CHECKS)} verificações — token, locks, "
          "prova, falha/recuperação e envio integrado")


if __name__ == "__main__":
    asyncio.run(run())
