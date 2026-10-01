"""T5/T7 e a matriz de concorrência da convivência manual/bot, em PostgreSQL.

`MANUALMARGIN_TEST_SOCKET` aponta para /tmp/cw-mmargin-sock.* criado pelo runner.
Driver async real, socket Unix, TCP/DNS bloqueados, cluster descartável UTF-8.
A exchange é falsa; repositório, locks, transações e serviços são os reais.

Defeitos reproduzidos na baseline `69fd090a`:

- **T5** carteira de 100 livres autorizava 80 para A; depois do fill de A (que
  tira a reserva da soma de pendentes), a MESMA observação autorizava mais 80.
- **T7** `reserve` criava intenção para um símbolo já reconhecido como manual,
  porque a admissão não consultava o reconhecimento sob a lock decisória.
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

test_socket = os.environ.get("MANUALMARGIN_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-mmargin-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://mmargin@/mmargindb?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste de margem manual/bot")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste de margem manual/bot")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste de margem manual/bot")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "d" * 64


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


async def run():
    from sqlalchemy import func, select, text
    import db
    from models.entry_intent import EntryIntent
    from models.execution_incident import ExecutionIncident
    from models.manual_position_ack import ManualPositionAcknowledgement as Ack
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot   # FK
    from models.risk_state import RiskState
    from services import entry_intent_service as intents
    from services import manual_position_service as mps

    # ── Migração vinda de 69fd090a: a tabela EXISTE no formato antigo ─────
    # (sem revisão/prova de validação e com o índice único só de ACTIVE).
    async with db._engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE manual_position_acks (
                id SERIAL PRIMARY KEY,
                account_scope VARCHAR(64) NOT NULL,
                exchange VARCHAR(20) NOT NULL,
                market VARCHAR(20) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                quote VARCHAR(20) NOT NULL,
                side VARCHAR(8) NOT NULL,
                position_side VARCHAR(10) NOT NULL,
                qty NUMERIC(38,18) NOT NULL,
                entry_price NUMERIC(38,18) NOT NULL,
                exchange_update_time_ms BIGINT NOT NULL,
                fingerprint VARCHAR(64) NOT NULL,
                contract_version VARCHAR(32) NOT NULL,
                state VARCHAR(16) NOT NULL,
                reason VARCHAR(200),
                identity_note VARCHAR(120),
                incident_key VARCHAR(200),
                evidence JSONB,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                ended_at TIMESTAMPTZ,
                ended_reason VARCHAR(120))"""))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_manual_ack_active "
            "ON manual_position_acks (account_scope, exchange, market, symbol) "
            "WHERE state = 'ACTIVE'"))
        await conn.execute(text(
            "INSERT INTO manual_position_acks (account_scope, exchange, market, "
            "symbol, quote, side, position_side, qty, entry_price, "
            "exchange_update_time_ms, fingerprint, contract_version, state, "
            "created_at, updated_at) VALUES ('legado', 'binance', "
            "'usdm_futures', 'OMEGA/USDT:USDT', 'USDT', 'buy', 'BOTH', 1, 100, "
            "1770000000000, 'f'||repeat('0',63), 'MANUAL_ACK_V1', 'ACTIVE', "
            "now(), now())"))
    tabelas = [RecommendationSnapshot.__table__, RealTrade.__table__,
               EntryIntent.__table__, ExecutionIncident.__table__,
               RiskState.__table__, Ack.__table__]
    for _ in range(2):
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all, tables=tabelas)
    await db.init_db()
    await db.init_db()
    async with db.get_session() as session:
        legado = (await session.execute(text(
            "SELECT state, revision, validated_at_ms FROM manual_position_acks "
            "WHERE account_scope = 'legado'"))).one_or_none()
        indices_legado = {linha[0] for linha in (await session.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'manual_position_acks'"))).all()}
    check("migracao_de_69fd090a_preserva_a_linha_existente",
          legado is not None and legado[0] == "ACTIVE" and legado[1] == 0
          and legado[2] is None, str(legado))
    check("migracao_troca_o_indice_sem_destruir_historico",
          "uq_manual_ack_open" in indices_legado
          and "uq_manual_ack_active" not in indices_legado,
          str(sorted(indices_legado)))

    async def epoca_manual():
        """Época da validação MANUAL vigente (prova sintética coerente)."""
        async with db.get_session() as session:
            valor = await intents.current_manual_generation(
                session, account_scope=ESCOPO, exchange="binance",
                market="usdm_futures")
            await session.commit()
            return valor

    async def liberar_conta():
        """Ciclo ACCOUNT completo: a conta nasce BLOQUEADA por contrato."""
        from services import binance_signed_service as _bss
        from services import manual_position_service as _mps
        from unittest.mock import patch as _patch, AsyncMock as _AM
        with _patch.object(_bss, "accounting_scope", lambda: ESCOPO), \
                _patch.object(_bss, "is_configured", return_value=True), \
                _patch.object(_bss, "get_positions",
                              _AM(return_value={"ok": True, "positions": []})):
            contexto = await _mps.capture_validation_context(
                scope=_mps.SCOPE_ACCOUNT)
            observacao = await _mps.observe_positions()
            return await _mps.revalidate_active(observation=observacao,
                                                context=contexto)

    def identidade(symbol_guardado, trigger):
        return intents.EntryIdentity(
            account_ref=ESCOPO, exchange="binance", symbol=symbol_guardado,
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)

    def agora_ms():
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    async def geracao_atual():
        async with db.get_session() as session:
            return await intents.current_margin_generation(
                session, account_ref=ESCOPO, exchange="binance",
                market="usdm_futures")

    async def carteira(disponivel, requerido, *, generation=None,
                       as_of_ms=None, quality="live"):
        g = generation if generation is not None else await geracao_atual()
        instante = as_of_ms if as_of_ms is not None else agora_ms()
        return intents.MarginGate(
            available_usd=disponivel, required_usd=requerido,
            as_of_ms=instante, observed_start_ms=instante - 5,
            observed_end_ms=instante, quality=quality, complete=True,
            account_ref=ESCOPO, exchange="binance", market="usdm_futures",
            generation=g)

    # ══════════════════════════════════════════════════════════════════════
    #  Migração aditiva e idempotente
    # ══════════════════════════════════════════════════════════════════════
    async with db.get_session() as session:
        colunas_ack = {linha[0] for linha in (await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'manual_position_acks'"))).all()}
        tabelas_novas = {linha[0] for linha in (await session.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'account_margin_epochs'"))).all()}
        indices = {linha[0] for linha in (await session.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'manual_position_acks'"))).all()}
    check("migracao_cria_epoca_de_margem", "account_margin_epochs" in tabelas_novas,
          str(tabelas_novas))
    check("migracao_adiciona_prova_de_validacao",
          {"validated_at_ms", "revision"} <= colunas_ack,
          str(sorted(colunas_ack))[:200])
    check("indice_unico_cobre_estados_bloqueantes",
          "uq_manual_ack_open" in indices, str(sorted(indices)))

    # ══════════════════════════════════════════════════════════════════════
    #  T5 — a carteira anterior ao fill não autoriza a próxima proposta
    # ══════════════════════════════════════════════════════════════════════
    liberou = await liberar_conta()
    check("conta_liberada_por_ciclo_completo",
          liberou["ok"] and liberou.get("account_unblocked") is True,
          str(liberou)[:200])
    observada = await carteira(100.0, 80.0)
    primeira = await intents.reserve(
        db.get_session, identidade("ALFA-USDT-USDT", 1000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t5", margin=observada)
    check("t5_primeira_reserva_concedida", primeira.granted, str(primeira))
    check("t5_reserva_devolve_geracao", primeira.generation is not None,
          str(primeira))
    assert await intents.mark_sending(db.get_session, primeira.intent_key,
                                      owner="t5")
    async with db.get_session() as session:
        trade = RealTrade(symbol="ALFA/USDT:USDT", side="long", source="auto",
                          exchange="binance", qty=4.0, entry_price=100.0,
                          status="open", opened_at=datetime.now(timezone.utc),
                          planned_stop=95.0, sl_order_id="sl-sintetico")
        session.add(trade)
        await session.commit()
        trade_id = trade.id
    geracao_antes = await geracao_atual()
    assert await intents.mark_confirmed(db.get_session, primeira.intent_key,
                                        owner="t5", real_trade_id=trade_id)
    geracao_depois = await geracao_atual()
    check("t5_confirmacao_invalida_carteiras_anteriores",
          geracao_depois > geracao_antes, f"{geracao_antes} → {geracao_depois}")

    segunda = await intents.reserve(
        db.get_session, identidade("BETA-USDT-USDT", 2000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t5", margin=observada)
    check("t5_carteira_anterior_ao_fill_nao_autoriza",
          segunda.granted is False
          and segunda.reason == "MARGIN_OBSERVATION_SUPERSEDED", str(segunda))

    # Uma carteira NOVA de 20 admite proposta que caiba, nunca a de 80.
    nova_pequena = await carteira(20.0, 80.0)
    grande = await intents.reserve(
        db.get_session, identidade("BETA-USDT-USDT", 2000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t5", margin=nova_pequena)
    check("t5_carteira_nova_nao_admite_proposta_grande",
          grande.granted is False
          and grande.reason == "INSUFFICIENT_FREE_MARGIN", str(grande))
    nova_cabe = await carteira(20.0, 15.0)
    pequena = await intents.reserve(
        db.get_session, identidade("BETA-USDT-USDT", 2000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t5", margin=nova_cabe)
    check("t5_carteira_nova_admite_proposta_que_cabe", pequena.granted,
          str(pequena))

    # `admit_final_risk` também recusa observação superada.
    velha = await carteira(20.0, 15.0, generation=0)
    final_velho = await intents.admit_final_risk(
        db.get_session, pequena.intent_key, owner="t5", risk_usd=1.0,
        margin=velha)
    check("t5_readmissao_recusa_observacao_superada",
          final_velho.granted is False
          and final_velho.reason == "MARGIN_OBSERVATION_SUPERSEDED",
          str(final_velho))
    atual = await carteira(20.0, 18.0)
    final_ok = await intents.admit_final_risk(
        db.get_session, pequena.intent_key, owner="t5", risk_usd=1.0,
        margin=atual)
    check("t5_readmissao_com_observacao_atual_passa", final_ok.granted,
          str(final_ok))
    check("t5_readmissao_devolve_a_propria_geracao",
          final_ok.generation is not None
          and final_ok.generation >= atual.generation, str(final_ok))
    async with db.get_session() as session:
        reservada = float((await session.execute(
            select(EntryIntent.reserved_margin_usd)
            .where(EntryIntent.intent_key == pequena.intent_key))).scalar() or 0)
    check("t5_reserva_final_substitui_a_menor", abs(reservada - 18.0) < 1e-6,
          str(reservada))

    # Enquanto o envio está incerto, a reserva permanece.
    assert await intents.mark_sending(db.get_session, pequena.intent_key, owner="t5")
    assert await intents.mark_unknown(db.get_session, pequena.intent_key,
                                      owner="t5", reason="DISPATCH_UNKNOWN")
    async with db.get_session() as session:
        ainda = float((await session.execute(
            select(func.coalesce(func.sum(EntryIntent.reserved_margin_usd), 0.0))
            .where(EntryIntent.state == "UNKNOWN"))).scalar() or 0)
    check("t5_envio_incerto_mantem_reserva", abs(ainda - 18.0) < 1e-6, str(ainda))

    # ══════════════════════════════════════════════════════════════════════
    #  T7 — reserva em símbolo reconhecido é negada sob a lock decisória
    # ══════════════════════════════════════════════════════════════════════
    agora = datetime.now(timezone.utc)
    async with db.get_session() as session:
        session.add(Ack(account_scope=ESCOPO, exchange="binance",
                        market="usdm_futures", symbol="GAMA/USDT:USDT",
                        quote="USDT", side="buy", position_side="BOTH",
                        qty=2, entry_price=100, exchange_update_time_ms=1_770_000_000_000,
                        fingerprint="a" * 64, contract_version="MANUAL_ACK_V1",
                        state="ACTIVE", created_at=agora, updated_at=agora,
                        validated_at_ms=agora_ms(), revision=1,
                        validated_revision=1,
                        validated_generation=await epoca_manual(),
                        validation_scope="ACCOUNT", validation_account=ESCOPO))
        await session.commit()
    nova = await carteira(1_000.0, 10.0)
    gama = await intents.reserve(
        db.get_session, identidade("GAMA-USDT-USDT", 3000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t7", margin=nova)
    check("t7_reserva_em_simbolo_reconhecido_e_negada",
          gama.granted is False and gama.reason == intents.OWNERSHIP_BLOCKED,
          str(gama))
    async with db.get_session() as session:
        linhas = int((await session.execute(
            select(func.count(EntryIntent.intent_key))
            .where(EntryIntent.symbol == "GAMA-USDT-USDT"))).scalar() or 0)
    check("t7_nenhuma_intencao_persistida_no_simbolo_manual", linhas == 0,
          str(linhas))

    # Retomada da MESMA intenção também é barrada (não só o ramo de criação).
    livre = await intents.reserve(
        db.get_session, identidade("DELTA-USDT-USDT", 4000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t7", margin=nova)
    check("t7_simbolo_livre_continua_reservando", livre.granted, str(livre))
    agora2 = datetime.now(timezone.utc)
    async with db.get_session() as session:
        session.add(Ack(account_scope=ESCOPO, exchange="binance",
                        market="usdm_futures", symbol="DELTA/USDT:USDT",
                        quote="USDT", side="buy", position_side="BOTH",
                        qty=2, entry_price=100, exchange_update_time_ms=1_770_000_000_000,
                        fingerprint="b" * 64, contract_version="MANUAL_ACK_V1",
                        state="ACTIVE", created_at=agora2, updated_at=agora2,
                        validated_at_ms=agora_ms(), revision=1,
                        validated_revision=1,
                        validated_generation=await epoca_manual(),
                        validation_scope="ACCOUNT", validation_account=ESCOPO))
        await session.commit()
    retomada = await intents.reserve(
        db.get_session, identidade("DELTA-USDT-USDT", 4000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t7", margin=nova)
    check("t7_retomada_tambem_e_barrada",
          retomada.granted is False and retomada.reason == intents.OWNERSHIP_BLOCKED,
          str(retomada))
    readmissao = await intents.admit_final_risk(
        db.get_session, livre.intent_key, owner="t7", risk_usd=1.0, margin=nova)
    check("t7_readmissao_tambem_e_barrada",
          readmissao.granted is False
          and readmissao.reason == intents.OWNERSHIP_BLOCKED, str(readmissao))
    # Estado bloqueante que NÃO é ACTIVE também barra.
    async with db.get_session() as session:
        await session.execute(text(
            "UPDATE manual_position_acks SET state = 'WAITING_ORDERS' "
            "WHERE symbol = 'GAMA/USDT:USDT'"))
        await session.commit()
    esperando = await intents.reserve(
        db.get_session, identidade("GAMA-USDT-USDT", 3001),
        {"entry": 100.0, "stop_loss": 95.0}, owner="t7", margin=nova)
    check("t7_estado_de_espera_tambem_barra_a_reserva",
          esperando.granted is False
          and esperando.reason == intents.OWNERSHIP_BLOCKED, str(esperando))
    # Conta/símbolo/quote diferentes NÃO colapsam.
    outra_conta = intents.EntryIdentity(
        account_ref="e" * 64, exchange="binance", symbol="GAMA-USDT-USDT",
        quote="USDT", side="long", position_side="BOTH", timeframe="4h",
        playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
        purpose="ENTRY", trigger_candle_ms=3002)
    # Conta NOVA nasce sem validação (contrato): primeiro ela é negada pelo seu
    # PRÓPRIO estado, não por herdar o bloqueio de símbolo da outra conta.
    sem_validacao = await intents.reserve(
        db.get_session, outra_conta, {"entry": 100.0, "stop_loss": 95.0},
        owner="t7")
    check("t7_conta_nova_nasce_sem_validacao",
          sem_validacao.granted is False, str(sem_validacao))
    outro_escopo = "e" * 64
    from services import binance_signed_service as _bss2
    from services import manual_position_service as _mps2
    from unittest.mock import patch as _p2, AsyncMock as _AM2
    with _p2.object(_bss2, "accounting_scope", lambda: outro_escopo), \
            _p2.object(_bss2, "is_configured", return_value=True), \
            _p2.object(_bss2, "get_positions",
                       _AM2(return_value={"ok": True, "positions": []})):
        ctx_outra = await _mps2.capture_validation_context(
            scope=_mps2.SCOPE_ACCOUNT)
        obs_outra = await _mps2.observe_positions()
        lib_outra = await _mps2.revalidate_active(observation=obs_outra,
                                                 context=ctx_outra)
    check("t7_conta_nova_valida_por_ciclo_proprio",
          lib_outra["ok"] and lib_outra.get("account_unblocked") is True,
          str(lib_outra)[:200])
    sem_gate = await intents.reserve(
        db.get_session, outra_conta, {"entry": 100.0, "stop_loss": 95.0},
        owner="t7")
    check("t7_conta_diferente_nao_colapsa", sem_gate.granted, str(sem_gate))
    # QUOTE diferente não colapsa no mesmo símbolo: `GAMA/USDC` não é bloqueada
    # pelo reconhecimento de `GAMA/USDT`. (A admissão completa continua exigindo
    # a validação de conta, já coberta acima — aqui o alvo é o SÍMBOLO.)
    async with db.get_session() as session:
        usdt = await mps.check_ownership_in_session(
            session, account_scope=ESCOPO, exchange="binance",
            market="usdm_futures", symbol="GAMA/USDT:USDT", action="reserve",
            require_fresh_proof=False)
        usdc = await mps.check_ownership_in_session(
            session, account_scope=ESCOPO, exchange="binance",
            market="usdm_futures", symbol="GAMA/USDC:USDC", action="reserve",
            require_fresh_proof=False)
        await session.commit()
    check("t7_quote_diferente_nao_colapsa",
          usdt["allowed"] is False and usdc["allowed"] is True,
          f"{usdt.get('reason_code')} / {usdc.get('reason_code')}")


    # ══════════════════════════════════════════════════════════════════════
    #  Concorrência REAL: duas conexões, barreiras nos pontos reais
    # ══════════════════════════════════════════════════════════════════════
    # Cenário de margem puro: sem reconhecimento manual e com a conta validada
    # por um ciclo ACCOUNT completo.
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM manual_position_acks "
                                   "WHERE account_scope = :s"), {"s": ESCOPO})
        await session.commit()
    conta_limpa = await liberar_conta()
    check("conc_conta_validada_sem_reconhecimento",
          conta_limpa["ok"] and conta_limpa.get("account_unblocked") is True,
          str(conta_limpa)[:200])
    LOCK = intents.RISK_LOCK_KEY

    async def esperando_a_lock() -> bool:
        """Espera REAL: alguém bloqueado no advisory lock, visto em `pg_locks`."""
        async with db.get_session() as session:
            total = int((await session.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND NOT granted AND objid = :k"), {"k": LOCK})).scalar() or 0)
        return total > 0

    async def segurar_lock(*, na_fila: asyncio.Event, liberar: asyncio.Event,
                           durante=None):
        """Segura a advisory lock até o outro participante entrar na fila."""
        async with db.get_session() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                      {"k": LOCK})
                for _ in range(400):
                    if await esperando_a_lock():
                        na_fila.set()
                        break
                    await asyncio.sleep(0.01)
                if durante is not None:
                    await durante()
                await liberar.wait()

    # (a) A carteira de B foi lida ANTES do fill de A. B fica ESPERANDO a lock
    #     enquanto a intenção de A vira RealTrade (a margem sai da soma de
    #     pendentes). Ao adquirir a lock, B é recusado por GERAÇÃO.
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM entry_intents"))
        await session.commit()
    ident_a = identidade("ETA-USDT-USDT", 5000)
    gate_a = await carteira(100.0, 60.0)
    reserva_a = await intents.reserve(
        db.get_session, ident_a, {"entry": 100.0, "stop_loss": 95.0},
        owner="conc", margin=gate_a)
    check("conc_reserva_a_concedida", reserva_a.granted, str(reserva_a))
    assert await intents.mark_sending(db.get_session, reserva_a.intent_key,
                                      owner="conc")
    gate_b_antigo = await carteira(100.0, 60.0)     # lida ANTES do fill de A
    na_fila, liberar = asyncio.Event(), asyncio.Event()

    async def confirmar_a():
        async with db.get_session() as session:
            trade = RealTrade(symbol="ETA/USDT:USDT", side="long", source="auto",
                              exchange="binance", qty=1.0, entry_price=100.0,
                              status="open", opened_at=datetime.now(timezone.utc),
                              planned_stop=95.0)
            session.add(trade)
            await session.commit()
            novo_id = trade.id
        assert await intents.mark_confirmed(db.get_session, reserva_a.intent_key,
                                            owner="conc", real_trade_id=novo_id)

    # Barreira NO MÉTODO REAL: o escritor (confirmação de A) já está DENTRO da
    # sua transação com a lock `917283` e pausa logo depois de incrementar a
    # época; B entra na FILA da mesma lock. Nenhum participante chama outro
    # escritor que precise da lock — isso travaria contra a própria operação.
    original_bump = intents._bump_margin_generation

    async def bump_e_espera(*args, **kwargs):
        valor = await original_bump(*args, **kwargs)
        if asyncio.current_task().get_name() == "conc-confirma":
            for _ in range(600):
                if await esperando_a_lock():
                    na_fila.set()
                    break
                await asyncio.sleep(0.01)
            await asyncio.wait_for(liberar.wait(), timeout=15)
        return valor

    with patch.object(intents, "_bump_margin_generation", bump_e_espera):
        tarefa_confirma = asyncio.create_task(confirmar_a(), name="conc-confirma")
        await asyncio.sleep(0.05)
        tarefa_b = asyncio.create_task(intents.reserve(
            db.get_session, identidade("TETA-USDT-USDT", 6000),
            {"entry": 100.0, "stop_loss": 95.0}, owner="conc",
            margin=gate_b_antigo), name="conc-b")
        await asyncio.wait_for(na_fila.wait(), timeout=20)
        check("conc_b_esperou_de_fato_pela_lock", True)
        liberar.set()
        await asyncio.wait_for(tarefa_confirma, timeout=20)
        reserva_b = await asyncio.wait_for(tarefa_b, timeout=20)
    check("conc_carteira_anterior_ao_fill_e_recusada_apos_a_espera",
          reserva_b.granted is False
          and reserva_b.reason == "MARGIN_OBSERVATION_SUPERSEDED", str(reserva_b))
    async with db.get_session() as session:
        teta = int((await session.execute(
            select(func.count(EntryIntent.intent_key))
            .where(EntryIntent.symbol == "TETA-USDT-USDT"))).scalar() or 0)
    check("conc_reserva_recusada_nao_consome_capacidade", teta == 0, str(teta))

    # (b) A espera REAL pela lock envelhece a carteira: o carimbo anterior à
    #     espera não passa (idade medida com o relógio POSTERIOR à lock).
    na_fila2, liberar2 = asyncio.Event(), asyncio.Event()

    async def demorar():
        await asyncio.sleep(1.2)

    gate_curto = intents.MarginGate(
        available_usd=1_000.0, required_usd=10.0, as_of_ms=agora_ms(),
        max_age_s=1.0, complete=True, account_ref=ESCOPO, exchange="binance",
        market="usdm_futures", generation=await geracao_atual(), quality="live")
    segurador2 = asyncio.create_task(
        segurar_lock(na_fila=na_fila2, liberar=liberar2, durante=demorar))
    await asyncio.sleep(0.02)
    tarefa_c = asyncio.create_task(intents.reserve(
        db.get_session, identidade("IOTA-USDT-USDT", 7000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="conc", margin=gate_curto))
    await asyncio.wait_for(na_fila2.wait(), timeout=10)
    liberar2.set()
    await segurador2
    reserva_c = await tarefa_c
    check("conc_espera_pela_lock_envelhece_a_carteira",
          reserva_c.granted is False and reserva_c.reason == "FREE_MARGIN_STALE",
          str(reserva_c))

    # (c) Reconhecimento × reserva nos DOIS sentidos, com barreira real.
    agora3 = datetime.now(timezone.utc)
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM manual_position_acks"))
        await session.commit()
    # (c1) A RESERVA ganha a lock primeiro: o ack posterior encontra intenção
    #      pendente e é NEGADO (exercitado no harness de convivência).
    gate_d = await carteira(1_000.0, 10.0)
    reserva_d = await intents.reserve(
        db.get_session, identidade("KAPA-USDT-USDT", 8000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="conc", margin=gate_d)
    check("conc_reserva_antes_do_ack_e_concedida", reserva_d.granted, str(reserva_d))
    # (c2) O ACK ganha a lock primeiro: a reserva seguinte é NEGADA por
    #      ownership DENTRO da transação (não por guard externo).
    na_fila3, liberar3 = asyncio.Event(), asyncio.Event()

    async def gravar_ack():
        async with db.get_session() as session:
            session.add(Ack(account_scope=ESCOPO, exchange="binance",
                            market="usdm_futures", symbol="LAMBDA/USDT:USDT",
                            quote="USDT", side="buy", position_side="BOTH",
                            qty=2, entry_price=100,
                            exchange_update_time_ms=1_770_000_000_000,
                            fingerprint="c" * 64, contract_version="MANUAL_ACK_V1",
                            state="ACTIVE", created_at=agora3, updated_at=agora3,
                            validated_at_ms=agora_ms(), revision=1,
                            validated_revision=1,
                            validated_generation=await epoca_manual(),
                            validation_scope="ACCOUNT",
                            validation_account=ESCOPO))
            await session.commit()

    gate_e = await carteira(1_000.0, 10.0)
    segurador3 = asyncio.create_task(
        segurar_lock(na_fila=na_fila3, liberar=liberar3, durante=gravar_ack))
    await asyncio.sleep(0.02)
    tarefa_e = asyncio.create_task(intents.reserve(
        db.get_session, identidade("LAMBDA-USDT-USDT", 9000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="conc", margin=gate_e))
    await asyncio.wait_for(na_fila3.wait(), timeout=10)
    liberar3.set()
    await segurador3
    reserva_e = await tarefa_e
    check("conc_ack_primeiro_nega_a_reserva_sob_a_lock",
          reserva_e.granted is False and reserva_e.reason == intents.OWNERSHIP_BLOCKED,
          str(reserva_e))
    async with db.get_session() as session:
        lam = int((await session.execute(
            select(func.count(EntryIntent.intent_key))
            .where(EntryIntent.symbol == "LAMBDA-USDT-USDT"))).scalar() or 0)
    check("conc_nenhuma_intencao_no_simbolo_reconhecido", lam == 0, str(lam))

    # (d) A geração sobrevive a restart e é a MESMA em outra conexão.
    antes_restart = await geracao_atual()
    await db._engine.dispose()
    depois_restart = await geracao_atual()
    check("geracao_sobrevive_restart", depois_restart == antes_restart,
          f"{antes_restart} → {depois_restart}")
    async with db.get_session() as s1, db.get_session() as s2:
        g1 = await intents.current_margin_generation(
            s1, account_ref=ESCOPO, exchange="binance", market="usdm_futures")
        await s1.commit()
        g2 = await intents.current_margin_generation(
            s2, account_ref=ESCOPO, exchange="binance", market="usdm_futures")
        await s2.commit()
    check("duas_conexoes_leem_a_mesma_geracao", g1 == g2 == depois_restart,
          f"{g1} {g2}")
    # Operação idempotente NÃO faz a geração crescer sem fim.
    estavel = await geracao_atual()
    await intents.mark_sending(db.get_session, "inexistente", owner="conc")
    await intents.recover_stale(db.get_session)
    check("operacao_sem_efeito_economico_nao_incrementa",
          await geracao_atual() == estavel, str(estavel))

    # ══════════════════════════════════════════════════════════════════════
    #  Admissão POSITIVA (token/dispatch/readmissão)
    # ══════════════════════════════════════════════════════════════════════
    #  O envio ponta a ponta — caller → preflight/readmissão reais → guard final
    #  → `_signed_request` real → cliente HTTP falso — é exercitado em
    #  `tests/pg_integration_manual_closure.py` (seção "Caller → transporte
    #  REAL"). Aqui ficam só as garantias de ADMISSÃO, sem simular um envio
    #  desligado do token.
    ident_pos = identidade("MI-USDT-USDT", 12000)
    gate_pos = await carteira(1_000.0, 25.0)
    reserva_pos = await intents.reserve(
        db.get_session, ident_pos, {"entry": 100.0, "stop_loss": 95.0},
        owner="pos", margin=gate_pos)
    check("positivo_reserva_concedida", reserva_pos.granted, str(reserva_pos))
    assert await intents.mark_sending(db.get_session, reserva_pos.intent_key,
                                      owner="pos")
    check("positivo_dispatch_registrado",
          await intents.register_dispatch(db.get_session, reserva_pos.intent_key,
                                          owner="pos",
                                          dispatch_id=reserva_pos.client_order_id))
    gate_final = await carteira(1_000.0, 30.0)
    readmissao_pos = await intents.admit_final_risk(
        db.get_session, reserva_pos.intent_key, owner="pos", risk_usd=5.0,
        margin=gate_final)
    check("positivo_readmissao_final_concedida", readmissao_pos.granted,
          str(readmissao_pos))
    check("positivo_readmissao_devolve_token",
          readmissao_pos.generation is not None
          and readmissao_pos.generation >= gate_final.generation,
          str(readmissao_pos))
    # A AUTORIZAÇÃO FINAL aprova com o token resultante da readmissão.
    autorizado = await intents.authorize_dispatch(
        db.get_session, reserva_pos.intent_key, owner="pos",
        expected_token=readmissao_pos.generation,
        dispatch_id=reserva_pos.client_order_id)
    check("positivo_autorizacao_final_aprova", autorizado["ok"],
          str(autorizado)[:200])

    # Carteira nova INSUFICIENTE: nem reserva.
    gate_curto2 = await carteira(5.0, 40.0)
    insuficiente = await intents.reserve(
        db.get_session, identidade("NI-USDT-USDT", 13000),
        {"entry": 100.0, "stop_loss": 95.0}, owner="pos", margin=gate_curto2)
    check("positivo_carteira_insuficiente_nao_reserva",
          insuficiente.granted is False
          and insuficiente.reason == "INSUFFICIENT_FREE_MARGIN", str(insuficiente))
    sem_autorizacao = await intents.authorize_dispatch(
        db.get_session, reserva_pos.intent_key, owner="pos",
        expected_token=int(readmissao_pos.generation) - 1,
        dispatch_id=reserva_pos.client_order_id)
    check("positivo_token_anterior_nao_autoriza",
          sem_autorizacao["ok"] is False, str(sem_autorizacao)[:200])

    await db._engine.dispose()
    print(f"MANUAL_MARGIN_PG_OK: {len(CHECKS)} verificações — geração da margem "
          "e ownership dentro da admissão")


if __name__ == "__main__":
    asyncio.run(run())
