"""Fechamento ÚNICO manual/BOT — F1–F7 e a matriz integrada, em PostgreSQL real.

`MANUALSINGLE_TEST_SOCKET` aponta para /tmp/cw-msingle-sock.* criado pelo runner.
Driver async real, socket Unix, TCP/DNS bloqueados, cluster descartável UTF-8.
Serviços, SQL, locks, épocas, propostas, transporte e assinatura são os REAIS;
só as bordas externas (HTTP da exchange, carteira, coleta, relógio e fontes
auxiliares) são falsas.

Defeitos reproduzidos na baseline `61920156` (ver `probe_baseline_red.py`):

- F1 `reduce_only="true"` abria MARKET sem `reduceOnly`, sem SL, com sl_ok=True;
- F2 falha local conhecida durante ordens/SQL/commit não impedia CLOSED;
- F3 observação de 21 s (limite 20 s) encerrava reconhecimento e liberava;
- F4 proposta aprovada envelhecia 1,7 s e o POST saía (TTL 1500 ms);
- F5 release/retomada decidiam a causa manual FORA da transação;
- F6 FLAT do incidente só conferia `.ok` — reconhecimento novo era encerrado;
- F7 TERMINAL→TERMINAL idêntico incrementava a geração financeira.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

test_socket = os.environ.get("MANUALSINGLE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-msingle-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://msingle@/msingledb?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no fechamento único manual/bot")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no fechamento único manual/bot")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no fechamento único manual/bot")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "e" * 64
ALFA = "ALFA5/USDT:USDT"
BETA = "BETA5/USDT:USDT"


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def agora() -> datetime:
    return datetime.now(timezone.utc)


async def run():
    from sqlalchemy import func, select, text
    import db
    from models.account_margin_epoch import AccountMarginEpoch as Epoch
    from models.entry_intent import EntryIntent
    from models.execution_incident import ExecutionIncident
    from models.manual_position_ack import ManualPositionAcknowledgement as Ack
    from models.recommendation_snapshot import RecommendationSnapshot  # noqa: F401
    from models.risk_state import RiskState
    from services import binance_signed_service as bss
    from services import entry_intent_service as intents
    from services import execution_reconciliation_service as ers
    from services import manual_position_service as mps
    from services import risk_service
    from services import shadow_trade_service as sts

    # Schema REAL, duas vezes (idempotência). Este pacote não muda contrato.
    await db.init_db()
    await db.init_db()
    async with db._engine.begin() as conn:
        colunas = {r[0] for r in (await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'account_margin_epochs'"))).all()}
        colunas_intent = {r[0] for r in (await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'entry_intents'"))).all()}
    check("f0_schema_sem_coluna_nova",
          {"generation", "manual_validation_generation",
           "manual_validation_blocked"} <= colunas
          and "decision_payload" in colunas_intent,
          f"{sorted(colunas)} {sorted(colunas_intent)}")

    # ── Bordas externas (exchange) ───────────────────────────────────────
    EXCHANGE = {"positions": [], "orders": [], "algo": [],
                "orders_ok": True, "algo_ok": True}
    ENVIADOS: list = []
    ASSINADOS: list = []
    #: Respostas programáveis da borda HTTP: {rota: fn(params, method) -> corpo}
    #: e {"status": fn(params, method) -> código}. Vazio = respostas padrão.
    RESPOSTAS: dict = {}

    async def get_positions(symbol=None, force=False):
        return {"ok": True, "positions": list(EXCHANGE["positions"])}

    async def get_open_orders(symbol=None):
        if not EXCHANGE["orders_ok"]:
            return {"ok": False, "error": "indisponível"}
        return {"ok": True, "orders": list(EXCHANGE["orders"])}

    async def get_open_algo_orders(symbol=None):
        if not EXCHANGE["algo_ok"]:
            return {"ok": False, "error": "indisponível"}
        return {"ok": True, "orders": list(EXCHANGE["algo"])}

    def assinar(path, params=None):
        ASSINADOS.append({"path": path, "params": dict(params or {})})
        return "https://sintetico.invalido" + path

    async def request(method, url):
        path = url.split("?")[0].replace("https://sintetico.invalido", "")
        params = {}
        for i, item in enumerate(ASSINADOS):
            if item["path"] == path and not item.get("usado"):
                params = item["params"]
                ASSINADOS[i]["usado"] = True
                break
        ENVIADOS.append({"method": method, "path": path, "params": params})
        corpo: object = {}
        if path == "/fapi/v1/order":
            corpo = {"orderId": 7, "status": "FILLED", "executedQty": "1",
                     "avgPrice": "100", "cumQuote": "100"}
        elif path == "/fapi/v1/algoOrder":
            corpo = {"algoId": "A7", "status": "NEW"}
        elif path == "/fapi/v2/positionRisk":
            corpo = [{"symbol": "X", "positionAmt": "0", "entryPrice": "0",
                      "markPrice": "0", "unRealizedProfit": "0",
                      "leverage": "5", "updateTime": 1}]
        status = 200
        if path in RESPOSTAS:
            substituto = RESPOSTAS[path](params, method)
            if substituto is not None:
                corpo = substituto
        if "status" in RESPOSTAS:
            status = RESPOSTAS["status"](params, method)
        return SimpleNamespace(status_code=status, headers={},
                               json=lambda: corpo)

    def posts_de_entrada():
        return [e for e in ENVIADOS
                if e["method"] == "POST" and e["path"] == "/fapi/v1/order"
                and not e["params"].get("reduceOnly")
                and not e["params"].get("closePosition")]

    def posts_de_protecao():
        return [e for e in ENVIADOS
                if e["method"] == "POST"
                and (e["path"] == "/fapi/v1/algoOrder"
                     or (e["path"] == "/fapi/v1/order"
                         and (e["params"].get("reduceOnly")
                              or e["params"].get("closePosition"))))]

    patches = [
        patch.object(bss, "is_configured", return_value=True),
        patch.object(bss, "get_positions", get_positions),
        patch.object(bss, "get_open_orders", get_open_orders),
        patch.object(bss, "get_open_algo_orders", get_open_algo_orders),
        patch.object(bss, "_build_signed_url", assinar),
        patch.object(bss, "_get_client", return_value=SimpleNamespace(request=request)),
        patch.object(bss, "_ban_until_ms", 0),
        patch.object(bss, "_throttle_until_ms", 0),
        patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda s, q: float(q))),
        patch.object(bss, "_round_price", AsyncMock(side_effect=lambda s, p: float(p))),
        patch.object(bss, "_get_symbol_filters", AsyncMock(return_value={
            "step": 0.001, "min_qty": 0.001, "max_qty": 10_000.0,
            "market_step": 0.001, "market_min_qty": 0.001,
            "market_max_qty": 10_000.0, "min_notional": 5.0, "tick": 0.01})),
        patch.object(mps, "current_account_scope", return_value=ESCOPO),
        patch.object(bss, "accounting_scope", lambda: ESCOPO),
    ]
    for item in patches:
        item.start()

    async def epoca_conta():
        async with db.get_session() as session:
            linha = (await session.execute(select(Epoch).where(
                Epoch.account_scope == ESCOPO))).scalar_one_or_none()
            if linha is None:
                return None
            return {"generation": int(linha.generation or 0),
                    "manual_generation": int(linha.manual_validation_generation or 0),
                    "blocked": bool(linha.manual_validation_blocked)}

    async def desbloquear_conta():
        async with db.get_session() as session:
            await session.execute(text(
                "INSERT INTO account_margin_epochs (account_scope, exchange, "
                "market, generation, manual_validation_generation, "
                "manual_validation_blocked, updated_at) VALUES "
                "(:s, 'binance', 'usdm_futures', 0, 0, false, now()) "
                "ON CONFLICT (account_scope, exchange, market) DO UPDATE SET "
                "manual_validation_blocked = false"), {"s": ESCOPO})
            await session.commit()
        mps.reset_local_validation_state()

    async def criar_ack(symbol, *, state="ACTIVE", revision=3, qty="1",
                        validated=True):
        """Reconhecimento REAL com prova completa quando pedido."""
        async with db.get_session() as session:
            epoca = await epoca_conta()
            geracao = (epoca or {}).get("manual_generation", 0)
            impressao = mps.position_fingerprint(
                account_scope=ESCOPO, exchange="binance", market="usdm_futures",
                symbol=mps.canonical_symbol(symbol), side="buy",
                position_side="long", qty=mps.canonical_decimal(qty),
                entry_price=mps.canonical_decimal("100"),
                update_time_ms=1_700_000_000_000,
                contract_version="MANUAL_ACK_V1")
            linha = Ack(
                account_scope=ESCOPO, exchange="binance", market="usdm_futures",
                symbol=mps.canonical_symbol(symbol), quote="USDT", side="buy",
                position_side="long", qty=qty, entry_price="100",
                exchange_update_time_ms=1_700_000_000_000,
                fingerprint=impressao, contract_version="MANUAL_ACK_V1",
                state=state, revision=revision,
                validated_at_ms=ms() if validated else None,
                validation_scope="ACCOUNT" if validated else None,
                validation_account=ESCOPO if validated else None,
                validated_revision=revision if validated else None,
                validated_generation=geracao if validated else None,
                created_at=agora(), updated_at=agora())
            session.add(linha)
            await session.commit()
            return int(linha.id)

    def posicao(symbol, *, size="1"):
        return {"symbol": mps.canonical_symbol(symbol), "side": "buy",
                "position_side": "long", "size": size, "entry_price": "100",
                "update_time_ms": 1_700_000_000_000}

    async def estado_ack(ack_id):
        async with db.get_session() as session:
            linha = (await session.execute(
                select(Ack).where(Ack.id == ack_id))).scalar_one()
            return {"state": linha.state, "revision": int(linha.revision or 0),
                    "validated_at_ms": linha.validated_at_ms,
                    "ended_reason": linha.ended_reason}

    async def limpar_acks():
        async with db.get_session() as session:
            await session.execute(text("DELETE FROM manual_position_acks"))
            await session.commit()

    # ══════════════════════════════════════════════════════════════════════
    #  F3 — carimbos ORIGINAIS e idade reconferida depois das esperas
    # ══════════════════════════════════════════════════════════════════════
    await desbloquear_conta()
    ack_f3 = await criar_ack(ALFA)
    EXCHANGE["positions"] = []
    EXCHANGE["orders"] = []
    EXCHANGE["algo"] = []

    relogio = {"offset_ms": 0}
    real_now_ms = mps._now_ms
    ordens_reais = mps.symbol_has_live_orders

    def now_ms_deslocado():
        return real_now_ms() + relogio["offset_ms"]

    contexto = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao = await mps.observe_positions()
    check("f3_observacao_fresca_e_completa",
          observacao["ok"] and observacao["complete"]
          and observacao["observed_start_ms"] <= observacao["observed_end_ms"],
          str(observacao)[:200])

    async def ordens_que_envelhecem(symbol):
        # A consulta de ordens leva 21 s REAIS de relógio (limite é 20 s).
        relogio["offset_ms"] += 21_000
        return await ordens_reais(symbol)

    with patch.object(mps, "_now_ms", now_ms_deslocado), \
            patch.object(mps, "symbol_has_live_orders", ordens_que_envelhecem):
        veredito = await mps.revalidate_active(observation=observacao,
                                               context=contexto)
    depois = await estado_ack(ack_f3)
    epoca_f3 = await epoca_conta()
    check("f3_observacao_velha_nao_encerra",
          veredito["ok"] is False
          and veredito["reason_code"] == mps.ACK_READ_TOO_OLD
          and veredito.get("stage") == "after_order_reads"
          and depois["state"] == "ACTIVE",
          f"{veredito} {depois}")
    check("f3_falha_externa_bloqueia_a_conta",
          epoca_f3["blocked"] is True and epoca_f3["manual_generation"] >= 1,
          str(epoca_f3))

    guarda_pos_reset = None
    mps.reset_local_validation_state()
    with patch.object(mps, "active_acknowledgements",
                      AsyncMock(return_value={"ok": True, "acks": [],
                                              "reason_code": "REGISTRY_READ"})):
        guarda_pos_reset = await mps.ownership_guard(ALFA, action="entry",
                                                     require_fresh_proof=True)
    check("f3_boot_zerado_nao_autoriza_por_estado_local",
          guarda_pos_reset["allowed"] is False
          and guarda_pos_reset["reason_code"] == mps.GUARD_ACCOUNT_BLOCKED,
          str(guarda_pos_reset))

    # Carimbo ilegítimo NUNCA vira `now`.
    await desbloquear_conta()
    for rotulo, mudanca in (("ausente", {"observed_end_ms": None}),
                            ("zero", {"observed_end_ms": 0}),
                            ("bool", {"observed_end_ms": True}),
                            ("futuro", {"observed_end_ms": ms() + 600_000}),
                            ("inicio_depois_do_fim",
                             {"observed_start_ms": ms() + 5_000})):
        contexto_i = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
        observacao_i = {**await mps.observe_positions(), **mudanca}
        veredito_i = await mps.revalidate_active(observation=observacao_i,
                                                 context=contexto_i)
        estado_i = await estado_ack(ack_f3)
        check(f"f3_carimbo_{rotulo}_e_recusado",
              veredito_i["ok"] is False
              and veredito_i["reason_code"] in (mps.ACK_READ_TIMESTAMP_INVALID,
                                                mps.ACK_READ_WINDOW_TOO_LONG)
              and estado_i["state"] == "ACTIVE",
              f"{veredito_i} {estado_i}")
        await desbloquear_conta()

    # Caminho POSITIVO: dentro da janela, encerra e publica com o carimbo
    # ORIGINAL (não o do commit).
    contexto_ok = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_ok = await mps.observe_positions()
    veredito_ok = await mps.revalidate_active(observation=observacao_ok,
                                              context=contexto_ok)
    estado_ok = await estado_ack(ack_f3)
    check("f3_janela_valida_encerra_de_verdade",
          veredito_ok["ok"] is True and ack_f3 in veredito_ok["closed"]
          and estado_ok["state"] == "CLOSED",
          f"{veredito_ok} {estado_ok}")
    check("f3_commit_carrega_a_janela_original",
          int(veredito_ok["observed_end_ms"]) == int(observacao_ok["observed_end_ms"])
          and veredito_ok["recorded_at_ms"] >= veredito_ok["observed_end_ms"],
          str(veredito_ok)[:200])

    # Prova publicada usa o carimbo ORIGINAL da observação.
    await limpar_acks()
    await desbloquear_conta()
    ack_prova = await criar_ack(ALFA, validated=False)
    EXCHANGE["positions"] = [posicao(ALFA)]
    contexto_p = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_p = await mps.observe_positions()
    veredito_p = await mps.revalidate_active(observation=observacao_p,
                                             context=contexto_p)
    estado_p = await estado_ack(ack_prova)
    check("f3_prova_usa_o_carimbo_da_observacao",
          veredito_p["ok"] is True and ack_prova in veredito_p["valid"]
          and int(estado_p["validated_at_ms"]) == int(observacao_p["observed_end_ms"]),
          f"{veredito_p} {estado_p}")

    # Envelhecer SÓ na espera pela advisory lock: a reconferência pós-lock
    # recusa, arma a causa e persiste DEPOIS do commit (sem abrir conexão nova
    # sob a `917283` — se abrisse, isto travaria).
    await limpar_acks()
    await desbloquear_conta()
    ack_lock = await criar_ack(ALFA)
    EXCHANGE["positions"] = []
    relogio["offset_ms"] = 0
    acquire_real_lock = intents.acquire_risk_lock

    async def lock_que_demora(session):
        resultado = await acquire_real_lock(session)
        relogio["offset_ms"] += 21_000      # a espera pela lock levou 21 s
        return resultado

    contexto_l = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_l = await mps.observe_positions()
    with patch.object(mps, "_now_ms", now_ms_deslocado), \
            patch.object(intents, "acquire_risk_lock", lock_que_demora):
        veredito_l = await asyncio.wait_for(
            mps.revalidate_active(observation=observacao_l, context=contexto_l),
            timeout=20)
    estado_l = await estado_ack(ack_lock)
    epoca_l = await epoca_conta()
    relogio["offset_ms"] = 0
    check("f3_idade_reconferida_depois_da_espera_pela_lock",
          veredito_l["ok"] is False
          and veredito_l.get("stage") == "after_lock_wait"
          and veredito_l.get("failure_persisted") is True
          and estado_l["state"] == "ACTIVE" and epoca_l["blocked"] is True,
          f"{veredito_l} {estado_l} {epoca_l}")

    # Ordens: evidência velha não fecha reconhecimento (vira WAITING_ORDERS).
    await limpar_acks()
    await desbloquear_conta()
    ack_prova = await criar_ack(ALFA, validated=False)
    EXCHANGE["positions"] = []

    async def ordens_com_evidencia_velha(symbol):
        saida = await ordens_reais(symbol)
        return {**saida, "observed_end_ms": saida["observed_end_ms"] - 25_000,
                "observed_start_ms": saida["observed_start_ms"] - 25_000}

    contexto_o = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_o = await mps.observe_positions()
    with patch.object(mps, "symbol_has_live_orders", ordens_com_evidencia_velha):
        veredito_o = await mps.revalidate_active(observation=observacao_o,
                                                 context=contexto_o)
    estado_o = await estado_ack(ack_prova)
    check("f3_ausencia_de_ordens_velha_nao_fecha",
          veredito_o["ok"] is True and ack_prova in veredito_o["waiting"]
          and estado_o["state"] == "WAITING_ORDERS"
          and mps.ACK_ORDERS_TOO_OLD in str(estado_o["ended_reason"]),
          f"{veredito_o} {estado_o}")

    # ══════════════════════════════════════════════════════════════════════
    #  F2 — fence local não se perde em ordens/SQL/commit
    # ══════════════════════════════════════════════════════════════════════
    await limpar_acks()
    await desbloquear_conta()
    ack_f2 = await criar_ack(ALFA)
    EXCHANGE["positions"] = []

    # (a) falha EXTERNA conhecida durante as consultas de ordens
    contexto_a = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_a = await mps.observe_positions()

    async def ordens_com_falha_concorrente(symbol):
        saida = await ordens_reais(symbol)
        # Outra corrotina registra (e PERSISTE) uma falha externa.
        await mps.register_validation_failure(reason="fonte caiu no meio",
                                              context=None)
        return saida

    with patch.object(mps, "symbol_has_live_orders", ordens_com_falha_concorrente):
        veredito_a = await mps.revalidate_active(observation=observacao_a,
                                                 context=contexto_a)
    estado_a = await estado_ack(ack_f2)
    epoca_a = await epoca_conta()
    check("f2_fence_que_avanca_nas_ordens_impede_o_closed",
          veredito_a["ok"] is False
          and veredito_a["reason_code"] == mps.STALE_CONTEXT
          and estado_a["state"] == "ACTIVE" and epoca_a["blocked"] is True,
          f"{veredito_a} {estado_a} {epoca_a}")

    # (b) falha que NÃO consegue persistir: timeout REAL de 500 ms na 917283
    await desbloquear_conta()
    segura = {"pronto": asyncio.Event(), "libera": asyncio.Event()}

    async def detentor_da_lock():
        async with db.get_session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(917283)"))
            segura["pronto"].set()
            await segura["libera"].wait()
            await session.rollback()

    tarefa = asyncio.create_task(detentor_da_lock())
    await segura["pronto"].wait()
    acquire_real = intents.acquire_risk_lock

    async def lock_com_timeout(session):
        await session.execute(text("SET LOCAL statement_timeout = 500"))
        return await acquire_real(session)

    with patch.object(intents, "acquire_risk_lock", lock_com_timeout):
        falha_b = await mps.register_validation_failure(
            reason="fonte caiu com banco travado", context=None)
    segura["libera"].set()
    await tarefa
    pendente_b = mps.pending_validation_failure()
    check("f2_falha_sem_commit_fica_pendente_localmente",
          falha_b["ok"] is False and pendente_b is not None
          and int(pendente_b["fence"]) == mps.local_validation_fence(),
          f"{falha_b} {pendente_b}")

    contexto_b = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_b = await mps.observe_positions()
    veredito_b = await mps.revalidate_active(observation=observacao_b,
                                             context=contexto_b)
    estado_b = await estado_ack(ack_f2)
    check("f2_pendencia_local_impede_publicacao",
          veredito_b["ok"] is False and estado_b["state"] == "ACTIVE",
          f"{veredito_b} {estado_b}")

    # O "restart" (estado local zerado) NÃO recupera: só a persistência da
    # falha pendente + novo ciclo completo recuperam.
    pendente_antes = mps.pending_validation_failure()
    flush = await mps.flush_pending_validation_failure()
    epoca_flush = await epoca_conta()
    check("f2_flush_persiste_a_causa_antes_de_recuperar",
          flush["ok"] is True and flush.get("cleared") is True
          and mps.pending_validation_failure() is None
          and epoca_flush["blocked"] is True
          and int(pendente_antes["fence"]) == mps.local_validation_fence(),
          f"{flush} {epoca_flush}")

    # Versão do pendente: um sucesso antigo não apaga causa NOVA.
    mps._advance_local_fence("causa antiga")
    versao_antiga = mps.local_validation_fence()
    mps._advance_local_fence("causa nova")
    check("f2_sucesso_antigo_nao_limpa_causa_nova",
          mps._clear_pending_if_version(versao_antiga) is False
          and mps.pending_validation_failure() is not None,
          str(mps.pending_validation_failure()))
    mps.reset_local_validation_state()

    # (c) fence avança DENTRO do commit ⇒ compensação desfaz só a própria
    #     escrita, contém o símbolo e devolve resultado inseguro.
    await desbloquear_conta()
    estado_pre = await estado_ack(ack_f2)
    contexto_c = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_c = await mps.observe_positions()
    revoke_real = mps._revoke_proof
    disparos = {"n": 0}

    def revoke_que_falha_no_meio(linha, momento):
        # Falha LOCAL conhecida aparecendo durante o commit (síncrona, como a
        # de `register_validation_failure` antes do primeiro await).
        if disparos["n"] == 0:
            disparos["n"] += 1
            mps._advance_local_fence("falha conhecida durante o commit")
        return revoke_real(linha, momento)

    with patch.object(mps, "_revoke_proof", revoke_que_falha_no_meio):
        veredito_c = await mps.revalidate_active(observation=observacao_c,
                                                 context=contexto_c)
    estado_c = await estado_ack(ack_f2)
    epoca_c = await epoca_conta()
    check("f2_compensacao_desfaz_o_closed_indevido",
          veredito_c["ok"] is False
          and veredito_c["reason_code"] == mps.PUBLICATION_FENCE_LOST
          and veredito_c.get("compensated") is True
          and ack_f2 in (veredito_c.get("undone") or [])
          and estado_c["state"] == estado_pre["state"]
          and estado_c["revision"] > estado_pre["revision"]
          and estado_c["validated_at_ms"] is None,
          f"{veredito_c} {estado_pre} → {estado_c}")
    check("f2_compensacao_bloqueia_a_conta_e_revoga_prova",
          epoca_c["blocked"] is True
          and epoca_c["manual_generation"] > epoca_a["manual_generation"],
          f"{epoca_a} → {epoca_c}")

    # (d) compensação que FALHA: contenção local permanece e o desfecho é UNKNOWN
    await desbloquear_conta()
    disparos["n"] = 0
    contexto_d = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_d = await mps.observe_positions()
    bump_real = intents.bump_manual_generation

    async def bump_que_explode(session, **kwargs):
        if disparos.get("compensando"):
            raise RuntimeError("banco caiu na compensação")
        return await bump_real(session, **kwargs)

    def revoke_que_falha_e_marca(linha, momento):
        if disparos["n"] == 0:
            disparos["n"] += 1
            mps._advance_local_fence("falha durante o commit (compensação ruim)")
            disparos["compensando"] = True
        return revoke_real(linha, momento)

    with patch.object(mps, "_revoke_proof", revoke_que_falha_e_marca), \
            patch.object(intents, "bump_manual_generation", bump_que_explode):
        veredito_d = await mps.revalidate_active(observation=observacao_d,
                                                 context=contexto_d)
    disparos["compensando"] = False
    contencao = mps.locally_blocked_symbols()
    guarda_contido = await mps.ownership_guard(ALFA, action="entry",
                                               require_fresh_proof=True)
    guarda_outro = None
    with patch.object(mps, "active_acknowledgements",
                      AsyncMock(return_value={"ok": True, "acks": [],
                                              "reason_code": "REGISTRY_READ"})), \
            patch.object(mps, "account_validation_state",
                         AsyncMock(return_value={"blocked": False,
                                                 "generation": 0,
                                                 "pending_failure": False,
                                                 "reason_code": "OK"})):
        guarda_outro = await mps.ownership_guard(BETA,
                                                 action="place_protection_orders")
    check("f2_compensacao_falha_vira_unknown_com_contencao",
          veredito_d["ok"] is False
          and veredito_d["reason_code"] == mps.COMPENSATION_UNKNOWN
          and veredito_d.get("manual_intervention_required") is True
          and mps.symbol_key(ALFA) in contencao
          and guarda_contido["allowed"] is False
          and guarda_contido["reason_code"] == mps.GUARD_LOCAL_CONTAINMENT,
          f"{veredito_d} {contencao} {guarda_contido}")
    check("f2_contencao_e_por_simbolo_e_nao_trava_protecao_alheia",
          guarda_outro["allowed"] is True, str(guarda_outro))
    # A contenção vale TAMBÉM na fronteira transacional (admissão/dispatch).
    async with db.get_session() as session:
        contido_na_transacao = await mps.check_ownership_in_session(
            session, account_scope=ESCOPO, exchange="binance",
            market="usdm_futures", symbol=ALFA, action="reserve")
        livre_na_transacao = await mps.check_ownership_in_session(
            session, account_scope=ESCOPO, exchange="binance",
            market="usdm_futures", symbol=BETA,
            action="place_protection_orders", require_fresh_proof=False)
    check("f2_contencao_vale_na_fronteira_transacional",
          contido_na_transacao["allowed"] is False
          and contido_na_transacao["reason_code"] == mps.GUARD_LOCAL_CONTAINMENT
          and livre_na_transacao["allowed"] is True,
          f"{contido_na_transacao} {livre_na_transacao}")
    mps.reset_local_validation_state()

    # ══════════════════════════════════════════════════════════════════════
    #  F7 — TERMINAL idêntico é no-op econômico
    # ══════════════════════════════════════════════════════════════════════
    await limpar_acks()
    await desbloquear_conta()

    def identidade(symbol_guardado, trigger, *, conta=ESCOPO):
        return intents.EntryIdentity(
            account_ref=conta, exchange="binance", symbol=symbol_guardado,
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=trigger)

    async def geracao_financeira():
        async with db.get_session() as session:
            return int((await session.execute(select(Epoch.generation).where(
                Epoch.account_scope == ESCOPO))).scalar() or 0)

    async def carteira(requerido, *, disponivel=10_000.0, generation=None):
        g = generation if generation is not None else await geracao_financeira()
        instante = ms()
        return intents.MarginGate(
            available_usd=disponivel, required_usd=requerido, as_of_ms=instante,
            observed_start_ms=instante - 5, observed_end_ms=instante,
            quality="live", complete=True, account_ref=ESCOPO,
            exchange="binance", market="usdm_futures", generation=g)

    ident_a = identidade("F7A-USDT-USDT", 1_770_000_000_000)
    reserva_a = await intents.reserve(
        db.get_session, ident_a, {"entry": 100.0, "stop_loss": 95.0},
        owner="dono-a", margin=await carteira(50.0))
    check("f7_reserva_concedida", reserva_a.granted is True, str(reserva_a))
    g_inicial = await geracao_financeira()
    await intents.mark_sending(db.get_session, ident_a.intent_key,
                               owner="dono-a")
    primeiro = await intents.mark_terminal(db.get_session, ident_a.intent_key,
                                           owner="dono-a", reason="NO_FILL")
    g_depois = await geracao_financeira()
    repeticao = await intents.mark_terminal(db.get_session, ident_a.intent_key,
                                            owner="dono-a", reason="NO_FILL")
    g_repeticao = await geracao_financeira()
    check("f7_primeiro_terminal_e_evento_economico",
          primeiro is True and g_depois == g_inicial + 1,
          f"{primeiro} {g_inicial} → {g_depois}")
    check("f7_repeticao_identica_e_no_op",
          repeticao is True and g_repeticao == g_depois,
          f"{repeticao} {g_depois} → {g_repeticao}")

    async with db.get_session() as session:
        linha_a = (await session.execute(select(EntryIntent).where(
            EntryIntent.intent_key == ident_a.intent_key))).scalar_one()
        resolvido_em = linha_a.resolved_at
        razao_a = linha_a.reason
    await intents.mark_terminal(db.get_session, ident_a.intent_key,
                                owner="dono-a", reason="OUTRO_MOTIVO")
    async with db.get_session() as session:
        linha_a2 = (await session.execute(select(EntryIntent).where(
            EntryIntent.intent_key == ident_a.intent_key))).scalar_one()
    check("f7_no_op_nao_recarimba_evidencia",
          linha_a2.resolved_at == resolvido_em and linha_a2.reason == razao_a
          and await geracao_financeira() == g_depois,
          f"{linha_a2.resolved_at} {linha_a2.reason}")

    # Dois reconciliadores simultâneos + "restart": UM incremento.
    ident_b = identidade("F7B-USDT-USDT", 1_770_000_100_000)
    await intents.reserve(db.get_session, ident_b,
                          {"entry": 100.0, "stop_loss": 95.0},
                          owner="dono-b", margin=await carteira(50.0))
    await intents.mark_sending(db.get_session, ident_b.intent_key,
                               owner="dono-b")
    g_antes_b = await geracao_financeira()
    resultados = await asyncio.gather(
        intents.mark_terminal(db.get_session, ident_b.intent_key,
                              owner="dono-b", reason="NO_FILL"),
        intents.mark_terminal(db.get_session, ident_b.intent_key,
                              owner="dono-b", reason="NO_FILL"),
    )
    pos_restart = await intents.mark_terminal(db.get_session,
                                              ident_b.intent_key,
                                              owner="dono-b", reason="NO_FILL")
    g_depois_b = await geracao_financeira()
    check("f7_concorrencia_e_restart_dao_um_incremento",
          all(resultados) and pos_restart is True
          and g_depois_b == g_antes_b + 1,
          f"{resultados} {pos_restart} {g_antes_b} → {g_depois_b}")

    # Token de OUTRA entrada atualizado depois do primeiro terminal continua
    # VALENDO depois da repetição — uso REAL do resultado.
    ident_c = identidade("F7C-USDT-USDT", 1_770_000_200_000)
    reserva_c = await intents.reserve(
        db.get_session, ident_c, {"entry": 100.0, "stop_loss": 95.0},
        owner="dono-c", margin=await carteira(50.0))
    await intents.mark_sending(db.get_session, ident_c.intent_key,
                               owner="dono-c")
    await intents.register_dispatch(db.get_session, ident_c.intent_key,
                                     owner="dono-c",
                                     dispatch_id=ident_c.client_order_id)
    with patch.object(mps, "check_ownership_in_session",
                      AsyncMock(return_value={"allowed": True})):
        autoriza_antes = await intents.authorize_dispatch(
            db.get_session, ident_c.intent_key, owner="dono-c",
            expected_token=reserva_c.generation,
            dispatch_id=ident_c.client_order_id)
        await intents.mark_terminal(db.get_session, ident_b.intent_key,
                                    owner="dono-b", reason="NO_FILL")
        autoriza_depois = await intents.authorize_dispatch(
            db.get_session, ident_c.intent_key, owner="dono-c",
            expected_token=reserva_c.generation,
            dispatch_id=ident_c.client_order_id)
    check("f7_token_alheio_sobrevive_a_repeticao",
          autoriza_antes.get("ok") is True and autoriza_depois.get("ok") is True,
          f"{autoriza_antes} {autoriza_depois}")

    # CONFIRMED não é sobrescrito nem rebaixado; TERMINAL não reabre.
    confirmou = await intents.mark_confirmed(db.get_session,
                                             ident_c.intent_key,
                                             reason="FILL_CONFIRMED")
    g_confirmado = await geracao_financeira()
    rebaixa = await intents.mark_unknown(db.get_session, ident_c.intent_key,
                                         reason="CALLBACK_ATRASADO")
    reabre = await intents.mark_unknown(db.get_session, ident_a.intent_key,
                                        reason="CALLBACK_ATRASADO")
    async with db.get_session() as session:
        estados = dict((await session.execute(select(
            EntryIntent.intent_key, EntryIntent.state).where(
            EntryIntent.intent_key.in_([ident_a.intent_key,
                                        ident_c.intent_key])))).all())
    check("f7_confirmado_nao_rebaixa_e_terminal_nao_reabre",
          confirmou is True and rebaixa is False and reabre is False
          and estados[ident_c.intent_key] == "CONFIRMED"
          and estados[ident_a.intent_key] == "TERMINAL"
          and await geracao_financeira() == g_confirmado,
          f"{confirmou} {rebaixa} {reabre} {estados}")

    # Reservas somadas sem retirada dupla.
    async with db.get_session() as session:
        soma = float((await session.execute(select(
            func.coalesce(func.sum(EntryIntent.reserved_margin_usd), 0.0))
            .where(EntryIntent.state.in_(list(intents.PENDING_STATES))))).scalar() or 0.0)
    check("f7_sem_retirada_dupla_de_reserva", soma == 0.0, str(soma))

    # ══════════════════════════════════════════════════════════════════════
    #  F5 — causa manual decidida DENTRO da transação do release/retomada
    # ══════════════════════════════════════════════════════════════════════
    async def limpar_incidentes():
        async with db.get_session() as session:
            await session.execute(text("DELETE FROM execution_incidents"))
            await session.commit()

    async def limpar_pausa():
        """Zera o `risk_state` do fixture (setup do teste, não produção)."""
        async with db.get_session() as session:
            await session.execute(text(
                "UPDATE risk_state SET trading_paused = false, "
                "pause_manual = false, pause_reason = NULL, paused_at = NULL "
                "WHERE id = 1"))
            await session.commit()

    async def armar_pausa_p03(motivo="incidente de teste"):
        await limpar_pausa()
        await risk_service.arm_p03_pause(motivo)

    async def estado_risco():
        async with db.get_session() as session:
            linha = (await session.execute(
                select(RiskState).where(RiskState.id == 1))).scalar_one_or_none()
            if linha is None:
                return None
            return {"paused": bool(linha.trading_paused),
                    "manual": bool(linha.pause_manual),
                    "reason": linha.pause_reason or ""}

    async def bloquear_conta():
        async with db.get_session() as session:
            await session.execute(text(
                "UPDATE account_margin_epochs SET manual_validation_blocked = true "
                "WHERE account_scope = :s"), {"s": ESCOPO})
            await session.commit()

    await limpar_incidentes()
    await limpar_acks()
    await desbloquear_conta()
    await armar_pausa_p03()
    await bloquear_conta()
    resultado_release = await risk_service.release_p03_pause(ers._PAUSE_MARKER)
    risco_apos = await estado_risco()
    check("f5_release_retido_pela_causa_manual",
          resultado_release == risk_service.RELEASE_MANUAL_CAUSE
          and risco_apos["paused"] is True
          and risco_apos["reason"].startswith(ers._PAUSE_MARKER),
          f"{resultado_release} {risco_apos}")

    retomada = await risk_service.set_manual_pause(False, "operador tentou")
    risco_retomada = await estado_risco()
    check("f5_retomada_manual_barrada_pela_causa_durave",
          retomada.get("kept_manual_cause") is True
          and retomada.get("resumed") is False
          and risco_retomada["paused"] is True
          and "manual-validation" in risco_retomada["reason"],
          f"{str(retomada)[:200]} {risco_retomada}")

    # Zero incidentes + causa manual: `_maybe_release_quarantine` mantém latch.
    ers._p03_latch_armed = True
    ers._boot_scan_safe = True
    liberou = await ers._maybe_release_quarantine()
    check("f5_quarentena_nao_libera_com_causa_manual",
          liberou is False and ers._p03_latch_armed is True, str(liberou))

    # CORRIDA REAL: outra conexão tenta gravar `blocked=true` enquanto o
    # release decide. A linha da época travada serializa as duas.
    await desbloquear_conta()
    await armar_pausa_p03()
    barreira = {"dentro": asyncio.Event(), "segue": asyncio.Event()}
    causa_real = mps.manual_cause_in_session

    async def causa_que_espera(session, **kwargs):
        veredito = await causa_real(session, **kwargs)
        barreira["dentro"].set()
        await barreira["segue"].wait()
        return veredito

    async def escritor_concorrente():
        await barreira["dentro"].wait()
        tarefa_escrita = asyncio.create_task(bloquear_conta())
        # O escritor tem de FICAR BLOQUEADO na linha da época travada.
        await asyncio.sleep(0.3)
        async with db.get_session() as session:
            esperando = int((await session.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'tuple' "
                "AND NOT granted"))).scalar() or 0)
            esperando += int((await session.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type "
                "= 'Lock' AND query ILIKE '%account_margin_epochs%'"))).scalar() or 0)
        barreira["bloqueado"] = esperando
        barreira["segue"].set()
        await tarefa_escrita

    with patch.object(mps, "manual_cause_in_session", causa_que_espera):
        resultado_corrida, _ = await asyncio.gather(
            risk_service.release_p03_pause(ers._PAUSE_MARKER),
            escritor_concorrente())
    epoca_corrida = await epoca_conta()
    check("f5_escritor_concorrente_fica_bloqueado_na_epoca",
          barreira.get("bloqueado", 0) >= 1, str(barreira.get("bloqueado")))
    check("f5_corrida_termina_consistente",
          (resultado_corrida == risk_service.RELEASE_RELEASED
           and epoca_corrida["blocked"] is True)
          or resultado_corrida == risk_service.RELEASE_MANUAL_CAUSE,
          f"{resultado_corrida} {epoca_corrida}")

    # Caminho POSITIVO: conta validada + zero incidentes ⇒ libera de verdade.
    await desbloquear_conta()
    await armar_pausa_p03()
    ers._p03_latch_armed = True
    liberou_ok = await ers._maybe_release_quarantine()
    risco_liberado = await estado_risco()
    check("f5_release_legitimo_acontece",
          liberou_ok is True and risco_liberado["paused"] is False
          and ers._p03_latch_armed is False,
          f"{liberou_ok} {risco_liberado}")

    # Falha local conhecida DURANTE o release: nada de sucesso com latch limpo.
    await armar_pausa_p03()
    ers._p03_latch_armed = True
    release_real = risk_service.release_p03_pause

    async def release_que_falha_no_meio(marcador):
        resultado = await release_real(marcador)
        mps._advance_local_fence("falha conhecida durante o release")
        return resultado

    with patch.object(risk_service, "release_p03_pause",
                      release_que_falha_no_meio):
        liberou_inseguro = await ers._maybe_release_quarantine()
    check("f5_falha_durante_o_release_rearma_o_latch",
          liberou_inseguro is False and ers._p03_latch_armed is True,
          str(liberou_inseguro))
    mps.reset_local_validation_state()
    await limpar_incidentes()

    # Retomada legítima volta a funcionar (nenhum bloqueio eterno).
    await desbloquear_conta()
    retomada_ok = await risk_service.set_manual_pause(False, "operador")
    risco_final = await estado_risco()
    check("f5_retomada_legitima_nao_fica_bloqueada_para_sempre",
          retomada_ok.get("resumed") is True
          and retomada_ok.get("kept_manual_cause") is False
          and risco_final["paused"] is False,
          f"{str(retomada_ok)[:160]} {risco_final}")

    # ══════════════════════════════════════════════════════════════════════
    #  F6 — recheck com CAS e resolução na MESMA transação
    # ══════════════════════════════════════════════════════════════════════
    async def criar_incidente(symbol, *, chave):
        async with db.get_session() as session:
            session.add(ExecutionIncident(
                incident_key=chave, exchange="binance",
                symbol=mps.canonical_symbol(symbol),
                kind=ers.Kind.UNTRACKED_POSITION,
                state=ers.State.MANUAL_REQUIRED, side="buy",
                created_at=agora(), updated_at=agora()))
            await session.commit()

    async def incidente(chave):
        async with db.get_session() as session:
            linha = (await session.execute(select(ExecutionIncident).where(
                ExecutionIncident.incident_key == chave))).scalar_one()
            return {"state": linha.state, "resolved": linha.resolved_at,
                    "last_error": linha.last_error or ""}

    await limpar_incidentes()
    await limpar_acks()
    await desbloquear_conta()
    EXCHANGE["positions"] = []
    EXCHANGE["orders"] = []
    EXCHANGE["algo"] = []
    await criar_incidente(ALFA, chave="binance:UNTRACKED_POSITION:ALFA5:1")
    ers._untracked_recheck_at.clear()

    # Reconhecimento NOVO criado DURANTE a leitura de flat: o FLAT da
    # observação velha não pode encerrar nada.
    gate_real = ers._fresh_gate

    async def gate_que_cria_ack(inc):
        resultado = await gate_real(inc)
        if not any(True for _ in ()):
            pass
        await criar_ack(BETA)                 # muda o CONJUNTO capturado
        return resultado

    with patch.object(ers, "_fresh_gate", gate_que_cria_ack):
        saida_f6 = await ers.recheck_untracked_manual()
    inc_f6 = await incidente("binance:UNTRACKED_POSITION:ALFA5:1")
    check("f6_reconhecimento_novo_impede_flat_antigo",
          saida_f6["resolved"] == 0
          and inc_f6["resolved"] is None
          and inc_f6["state"] == ers.State.MANUAL_REQUIRED
          and mps.STALE_CONTEXT in str(saida_f6["kept"]),
          f"{saida_f6} {inc_f6}")

    # Posição ABERTA nunca vira FLAT.
    await limpar_acks()
    EXCHANGE["positions"] = [posicao(ALFA)]
    ers._untracked_recheck_at.clear()
    saida_aberta = await ers.recheck_untracked_manual()
    inc_aberta = await incidente("binance:UNTRACKED_POSITION:ALFA5:1")
    check("f6_posicao_aberta_nunca_vira_flat",
          saida_aberta["resolved"] == 0 and inc_aberta["resolved"] is None,
          f"{saida_aberta} {inc_aberta}")

    # Caminho POSITIVO: contexto estável ⇒ FLAT resolvido em UMA transação.
    EXCHANGE["positions"] = []
    ers._untracked_recheck_at.clear()
    saida_ok = await ers.recheck_untracked_manual()
    inc_ok = await incidente("binance:UNTRACKED_POSITION:ALFA5:1")
    check("f6_contexto_estavel_resolve_flat",
          saida_ok["resolved"] == 1 and inc_ok["state"] == ers.State.FLAT
          and inc_ok["resolved"] is not None,
          f"{saida_ok} {inc_ok}")

    # ══════════════════════════════════════════════════════════════════════
    #  Seção 10 — matriz integrada pelo caller REAL `open_shadow_for_recs`
    # ══════════════════════════════════════════════════════════════════════
    from contextlib import ExitStack
    from services import exchange_service
    from services import edge_decay_service
    from services import kill_switch_service

    LIVRO = {"bid": 99.95, "ask": 100.05, "atraso_ms": 0}
    SKIPS: list = []

    def rec_sintetica(symbol, **extra):
        base = {"_just_saved": True, "tier": "A", "symbol": symbol,
                "score": 90, "timeframe": "4h", "playbook": "CHAMPION_LEGACY",
                "entry": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                "leverage": 5, "side": "long", "direction": "long", "signal": {"indicators": {"atr": 2.0}},
                "score_provenance": {},
                "data_freshness": {"candle": {"close_time_ms": ms()}}}
        base.update(extra)
        return base

    def quote_sintetica(**mudancas):
        instante = ms() - LIVRO["atraso_ms"]
        corpo = {"ok": True, "exchange": "binance",
                 "source": "binance_book_ticker", "symbol": "SYM",
                 "message_time_ms": float(instante),
                 "bid": LIVRO["bid"], "ask": LIVRO["ask"],
                 "bid_qty": 500.0, "ask_qty": 500.0,
                 "received_at_ms": float(instante),
                 "exchange_time_ms": float(instante), "latency_ms": 25.0}
        corpo.update(mudancas)
        return corpo

    def depth_sintetico(symbol, **mudancas):
        instante = ms() - LIVRO["atraso_ms"]
        corpo = {"ok": True, "exchange": "binance", "source": "binance_depth",
                 "symbol": symbol, "last_update_id": 42,
                 "message_time_ms": float(instante),
                 "bids": [[str(LIVRO["bid"]), "5000"],
                          [str(round(LIVRO["bid"] - 0.1, 4)), "5000"]],
                 "asks": [[str(LIVRO["ask"]), "5000"],
                          [str(round(LIVRO["ask"] + 0.1, 4)), "5000"]],
                 "received_at_ms": float(instante),
                 "exchange_time_ms": float(instante), "latency_ms": 25.0}
        corpo.update(mudancas)
        return corpo

    async def quote_fn(symbol, timeout_s=None):
        return quote_sintetica(symbol=bss.to_binance(symbol))

    async def depth_fn(symbol, limit=None, timeout_s=None):
        return depth_sintetico(symbol)

    FLAGS_BASE = {
        "DB_ENABLED": True, "SHADOW_ENABLED": False,
        "REGIME_SIZING_ENABLED": False, "P04C_DATA_FRESHNESS_ENABLED": False,
        "FILLER_FORA_ENABLED": False, "NEWS_GATE_ENABLED": False,
        "DAILY_PROFIT_TP_ENABLED": False, "PROXIMITY_GATE_ENABLED": False,
        "STRUCT_CHASE_GATE_ENABLED": False, "ATR_GATE_ENABLED": False,
        "RR_GATE_ENABLED": False, "PROB_TP1_GATE_ENABLED": False,
        "SCORE_ADJUSTERS_ENABLED": False, "SCORE_MIN": 0,
        "QUALITY_EDGE_GATE_ENABLED": False, "LIQUIDITY_GATE_ENABLED": False,
        "SYMBOL_BLACKLIST": set(), "MAKER_ENTRY_ENABLED": False,
        "P04A_ENTRY_REVALIDATION_ENABLED": True,
        "FILL_RR_GATE_ENABLED": False,
        # Throttle de cadência é borda de TEMPO: cada caso da matriz é um
        # ciclo independente, não uma rajada real.
        "ENTRY_COOLDOWN_SECONDS": 0, "ENTRY_MAX_PER_HOUR": 999,
        "MAX_OPEN_PER_DIRECTION": 99,
    }

    async def rodar_ciclo(recs, *, flags=None, extra_patches=()):
        """Chama o caller REAL com as bordas externas falsas."""
        ENVIADOS.clear()
        ASSINADOS.clear()
        SKIPS.clear()
        with ExitStack() as stack:
            for nome, valor in {**FLAGS_BASE, **(flags or {})}.items():
                stack.enter_context(patch.object(sts, nome, valor))
            stack.enter_context(patch.object(edge_decay_service, "is_enabled",
                                             return_value=False))
            stack.enter_context(patch.object(
                sts, "_p04c_live_data_verdict", return_value={"ok": True}))
            stack.enter_context(patch.object(
                sts, "_calibration_contract_verdict", return_value={"ok": True}))
            stack.enter_context(patch.object(sts, "get_exec_allowlist",
                                             return_value=set()))
            stack.enter_context(patch.object(sts, "_is_blocked_time",
                                             return_value=(False, "")))
            stack.enter_context(patch.object(
                sts, "_record_skip",
                lambda rec, stage, reason=None, **kw: SKIPS.append((stage, reason))))
            stack.enter_context(patch.object(
                sts, "_resolve_equity_usd", AsyncMock(return_value=(10_000.0, "live"))))
            stack.enter_context(patch.object(
                sts, "_free_margin_snapshot", AsyncMock(side_effect=lambda: {
                    "available_usd": 8_000.0, "as_of_ms": ms(),
                    "observed_start_ms": ms() - 5, "observed_end_ms": ms(),
                    "quality": "live", "source": "live"})))
            stack.enter_context(patch.object(
                kill_switch_service, "check_can_trade",
                AsyncMock(return_value={"allowed": True, "can_trade": True,
                                        "ok": True, "reason": None})))
            stack.enter_context(patch.object(
                exchange_service, "get_execution_quote", quote_fn, create=True))
            stack.enter_context(patch.object(
                exchange_service, "get_execution_depth", depth_fn, create=True))
            stack.enter_context(patch.object(exchange_service, "ACTIVE_EXCHANGE",
                                             "binance"))
            for item in extra_patches:
                stack.enter_context(item)
            return await sts.open_shadow_for_recs(recs)

    async def criar_snapshot(symbol):
        """Recomendação persistida mínima (o caller real precisa do vínculo)."""
        async with db.get_session() as session:
            linha = RecommendationSnapshot(
                symbol=mps.canonical_symbol(symbol), timeframe="4h", tier="A",
                direction="long", entry=100.0, stop_loss=95.0, tp1=105.0,
                tp2=110.0, score=90.0, risk_reward=2.0, leverage=5,
                risk_pct=1.0, stop_distance_pct=5.0, status="open",
                created_at=agora())
            session.add(linha)
            await session.commit()
            return int(linha.id)

    await limpar_incidentes()
    await limpar_acks()
    await desbloquear_conta()
    await limpar_pausa()
    EXCHANGE["positions"] = []
    async with db.get_session() as session:
        await session.execute(text("DELETE FROM entry_intents"))
        await session.commit()
    await criar_snapshot(BETA)
    await criar_snapshot(ALFA)
    # Latch em memória das seções anteriores: liberado por OWNER (sem clear
    # genérico), como a produção faz.
    for dono in ("manual", "p03"):
        sts.clear_execution_quarantine(owner=dono)
    ers._p03_latch_armed = False

    async def intencao_de(symbol):
        async with db.get_session() as session:
            linha = (await session.execute(
                select(EntryIntent)
                .where(EntryIntent.symbol.like(
                    f"%{mps.symbol_key(symbol).replace('/', '-')}%"))
                .order_by(EntryIntent.created_at.desc())
                .limit(1))).scalar_one_or_none()
            if linha is None:
                return None
            return {"state": linha.state, "reason": linha.reason,
                    "dispatch_ids": list(linha.dispatch_ids or []),
                    "token": linha.margin_generation,
                    "payload": dict(linha.decision_payload or {}),
                    "real_trade_id": linha.real_trade_id}

    contador = {"n": 0}

    async def simbolo_fresco(prefixo="MTX"):
        """Símbolo NOVO por caso: evita o guard real de trade duplicado."""
        contador["n"] += 1
        simbolo = f"{prefixo}{contador['n']}/USDT:USDT"
        await criar_snapshot(simbolo)
        return simbolo

    async def limpar_trades():
        """Zera o portfólio do fixture: os caps agregados REAIS continuam
        valendo dentro de cada caso, mas um caso não herda a carteira do outro.
        """
        async with db.get_session() as session:
            await session.execute(text("DELETE FROM real_trades"))
            await session.commit()

    async def trades_reais():
        async with db.get_session() as session:
            return int((await session.execute(text(
                "SELECT count(*) FROM real_trades"))).scalar() or 0)

    # ── m1: conta saudável sem manual ⇒ MARKET protegida completa ────────
    abertos = await rodar_ciclo([rec_sintetica(BETA)])
    entradas = posts_de_entrada()
    protecoes = posts_de_protecao()
    intencao = await intencao_de(BETA)
    check("m1_entrada_market_protegida_completa",
          abertos == 1 and len(entradas) == 1 and len(protecoes) >= 1
          and entradas[0]["params"]["type"] == "MARKET"
          and entradas[0]["params"]["side"] == "BUY"
          and entradas[0]["params"].get("reduceOnly") is None
          and entradas[0]["params"].get("price") is None
          and entradas[0]["params"].get("stopPrice") is None
          and float(entradas[0]["params"]["quantity"]) > 0,
          f"abertos={abertos} entradas={entradas} protecoes={len(protecoes)} "
          f"skips={SKIPS}")
    coid = entradas[0]["params"]["newClientOrderId"]
    proposta_persistida = (intencao or {}).get("payload", {}).get("proposals", {})
    check("m1_identidade_e_proposta_persistidas",
          intencao is not None and coid in intencao["dispatch_ids"]
          and coid in proposta_persistida
          and proposta_persistida[coid]["hash"]
          and int(proposta_persistida[coid]["token"]) == int(intencao["token"])
          and intencao["state"] == "CONFIRMED"
          and intencao["real_trade_id"] is not None,
          f"{intencao}")
    check("m1_proposta_persistida_bate_com_o_payload",
          intents.proposal_matches_stored(proposta_persistida[coid])["ok"]
          and abs(float(proposta_persistida[coid]["qty"])
                  - float(entradas[0]["params"]["quantity"])) < 1e-9
          and proposta_persistida[coid]["order_type"] == "MARKET"
          and proposta_persistida[coid]["price"] is None,
          str(proposta_persistida[coid])[:300])
    check("m1_protecao_e_redutora_de_verdade",
          all(p["params"].get("closePosition") == "true"
              or p["params"].get("reduceOnly") == "true"
              for p in protecoes),
          str(protecoes)[:300])
    trades_m1 = await trades_reais()

    # ── m2: manual reconhecido em ALFA ⇒ ALFA bloqueado, BETA opera ──────
    ack_matriz = await criar_ack(ALFA)
    EXCHANGE["positions"] = [posicao(ALFA)]
    await criar_snapshot(ALFA)
    abertos_alfa = await rodar_ciclo([rec_sintetica(ALFA)])
    check("m2_simbolo_manual_nao_recebe_entrada",
          abertos_alfa == 0 and posts_de_entrada() == []
          and posts_de_protecao() == [],
          f"{abertos_alfa} {ENVIADOS} {SKIPS}")
    outro = await simbolo_fresco()
    abertos_beta = await rodar_ciclo([rec_sintetica(outro)])
    check("m2_bot_opera_em_outro_simbolo_com_manual_vivo",
          abertos_beta == 1 and len(posts_de_entrada()) == 1,
          f"{abertos_beta} {ENVIADOS} {SKIPS}")
    check("m2_posicao_manual_nao_virou_real_trade",
          await trades_reais() == trades_m1 + 1,
          f"{await trades_reais()} vs {trades_m1}")
    estado_manual = await estado_ack(ack_matriz)
    check("m2_posicao_manual_nunca_foi_gerida",
          estado_manual["state"] == "ACTIVE"
          and not [e for e in ENVIADOS
                   if mps.symbol_key(ALFA).replace("/", "")
                   in str(e["params"].get("symbol", ""))],
          f"{estado_manual} {ENVIADOS}")

    # ── m3: causa manual BLOQUEADA ⇒ nega entrada, preserva proteção ─────
    await bloquear_conta()
    bloqueado = await simbolo_fresco()
    abertos_bloq = await rodar_ciclo([rec_sintetica(bloqueado)])
    check("m3_entrada_nova_negada_com_conta_bloqueada",
          abertos_bloq == 0 and posts_de_entrada() == [],
          f"{abertos_bloq} {ENVIADOS} {SKIPS}")
    ENVIADOS.clear()
    ASSINADOS.clear()
    protecao_bot = await bss.place_protection_orders(
        BETA, "Buy", 1.0, stop_loss=95.0,
        client_order_id_prefix="cw-prot-bot")
    check("m3_protecao_de_posicao_bot_em_outro_simbolo_preservada",
          protecao_bot.get("sl_ok") is True and len(posts_de_protecao()) >= 1,
          f"{str(protecao_bot)[:200]} {ENVIADOS}")
    ENVIADOS.clear()
    ASSINADOS.clear()
    reducao_bot = await bss.place_order(BETA, "SELL", 1.0, order_type="Market",
                                        reduce_only=True,
                                        client_order_id="cw-red-bot")
    check("m3_reducao_bot_em_outro_simbolo_preservada",
          reducao_bot.get("reason_code") not in (mps.GUARD_PROOF_STALE,
                                                 mps.GUARD_ACCOUNT_BLOCKED)
          and len([e for e in ENVIADOS if e["path"] == "/fapi/v1/order"
                   and e["params"].get("reduceOnly")]) == 1,
          f"{str(reducao_bot)[:200]} {ENVIADOS}")
    ENVIADOS.clear()
    ASSINADOS.clear()
    reducao_manual = await bss.place_order(ALFA, "SELL", 1.0,
                                           order_type="Market",
                                           reduce_only=True,
                                           client_order_id="cw-red-manual")
    check("m3_posicao_manual_nao_e_reduzida_pelo_bot",
          reducao_manual.get("ok") is False
          and reducao_manual.get("reason_code") == mps.GUARD_MANUAL_SYMBOL
          and ENVIADOS == [],
          f"{str(reducao_manual)[:200]} {ENVIADOS}")
    await desbloquear_conta()
    await limpar_acks()
    EXCHANGE["positions"] = []

    # ── m4: maker preenchida ⇒ proteção REAL ────────────────────────────
    FLAGS_MAKER = {"MAKER_ENTRY_ENABLED": True,
                   "P04A_ENTRY_REVALIDATION_ENABLED": True,
                   "MAKER_FALLBACK_MARKET": False}
    maker_sym = await simbolo_fresco("MKR")
    abertos_maker = await rodar_ciclo([rec_sintetica(maker_sym)],
                                      flags=FLAGS_MAKER)
    limites = [e for e in posts_de_entrada()
               if e["params"].get("type") == "LIMIT"]
    check("m4_maker_preenchida_instala_protecao",
          abertos_maker == 1 and len(limites) == 1
          and limites[0]["params"].get("timeInForce") == "GTX"
          and len(posts_de_protecao()) >= 1,
          f"{abertos_maker} {ENVIADOS} {SKIPS}")
    intencao_maker = await intencao_de(maker_sym)
    coid_maker = limites[0]["params"]["newClientOrderId"]
    check("m4_proposta_propria_do_maker",
          coid_maker in intencao_maker["payload"]["proposals"]
          and intencao_maker["payload"]["proposals"][coid_maker]["order_type"]
          == "LIMIT"
          and intencao_maker["payload"]["proposals"][coid_maker]["time_in_force"]
          == "GTX",
          str(intencao_maker["payload"]["proposals"])[:300])

    # ── m5: maker SEM fill provado ⇒ `-mfb` com proposta própria ─────────
    RESPOSTAS["/fapi/v1/order"] = lambda params, method: (
        {"code": -5022, "msg": "Post Only order will be rejected"}
        if method == "POST" and params.get("timeInForce") == "GTX"
        else None)
    RESPOSTAS["status"] = lambda params, method: (
        400 if method == "POST" and params.get("timeInForce") == "GTX" else 200)
    mfb_sym = await simbolo_fresco("MFB")
    abertos_mfb = await rodar_ciclo(
        [rec_sintetica(mfb_sym)],
        flags={**FLAGS_MAKER, "MAKER_FALLBACK_MARKET": True,
               "P04B_MAKER_FALLBACK_ENABLED": True})
    RESPOSTAS.clear()
    mercados = [e for e in posts_de_entrada()
                if e["params"].get("type") == "MARKET"]
    intencao_mfb = await intencao_de(mfb_sym)
    propostas_mfb = (intencao_mfb or {}).get("payload", {}).get("proposals", {})
    coids_mfb = sorted(propostas_mfb)
    check("m5_fallback_mfb_tem_dispatch_e_proposta_proprios",
          abertos_mfb == 1 and len(mercados) == 1
          and mercados[0]["params"]["newClientOrderId"].endswith("-mfb")
          and len(coids_mfb) == 2
          and any(c.endswith("-mfb") for c in coids_mfb)
          and all(propostas_mfb[c]["dispatch_id"] == c for c in coids_mfb),
          f"{abertos_mfb} {coids_mfb} {[e['params'] for e in posts_de_entrada()]}")
    filha = [c for c in coids_mfb if c.endswith("-mfb")][0]
    check("m5_prova_maker_nao_vale_como_prova_market",
          propostas_mfb[filha]["order_type"] == "MARKET"
          and propostas_mfb[filha]["evidence"]["evaluator"] == "depth"
          and propostas_mfb[filha]["price"] is None
          and len(posts_de_protecao()) >= 1,
          str(propostas_mfb[filha])[:300])

    # ── m6: maker com fill INCERTO ⇒ nenhum fallback inseguro ────────────
    RESPOSTAS["/fapi/v1/order"] = lambda params, method: (
        {"code": -1007, "msg": "timeout"}
        if method == "POST" and params.get("type") in ("LIMIT", "MARKET")
        else None)
    RESPOSTAS["status"] = lambda params, method: (
        400 if method == "POST" and params.get("type") in ("LIMIT", "MARKET")
        else 200)
    incerto_sym = await simbolo_fresco("UNK")
    abertos_incerto = await rodar_ciclo(
        [rec_sintetica(incerto_sym)],
        flags={**FLAGS_MAKER, "MAKER_FALLBACK_MARKET": True,
               "P04B_MAKER_FALLBACK_ENABLED": True})
    RESPOSTAS.clear()
    mercados_incerto = [e for e in posts_de_entrada()
                        if e["params"].get("type") == "MARKET"]
    intencao_incerta = await intencao_de(incerto_sym)
    async with db.get_session() as session:
        incidentes_abertos = int((await session.execute(select(
            func.count(ExecutionIncident.id))
            .where(ExecutionIncident.resolved_at.is_(None)))).scalar() or 0)
    check("m6_envio_incerto_nao_dispara_fallback",
          abertos_incerto == 0 and mercados_incerto == []
          and intencao_incerta["state"] in ("UNKNOWN", "SENDING")
          and incidentes_abertos >= 1,
          f"{abertos_incerto} {intencao_incerta} incidentes={incidentes_abertos}")
    await limpar_incidentes()
    for dono in ("manual", "p03"):
        sts.clear_execution_quarantine(owner=dono)
    ers._p03_latch_armed = False

    # ── m7: saída real (time-stop) pelo caller do trade manager ─────────
    from services import trade_manager_service as tms
    from models.real_trade import RealTrade
    async with db.get_session() as session:
        trade = (await session.execute(
            select(RealTrade).where(RealTrade.symbol == mps.canonical_symbol(BETA))
            .order_by(RealTrade.id.desc()).limit(1))).scalar_one_or_none()
        if trade is not None:
            trade.opened_at = agora() - timedelta(days=30)
            await session.commit()
            trade_id, trade_symbol = int(trade.id), trade.symbol
    ENVIADOS.clear()
    ASSINADOS.clear()
    EXCHANGE["positions"] = []
    with patch.object(tms, "_resolve_trade_timeframe",
                      AsyncMock(return_value="4h")), \
            patch.object(tms, "_fetch_exchange_position",
                         AsyncMock(return_value=(0.0, "long"))), \
            patch.object(tms, "_cleanup_conditionals_after_flat",
                         AsyncMock(return_value=True)), \
            patch.object(tms, "_close_trade", AsyncMock(return_value=True)):
        async with db.get_session() as session:
            trade_vivo = (await session.execute(select(RealTrade).where(
                RealTrade.id == trade_id))).scalar_one()
            fechou = await tms._check_time_stop(trade_vivo, 1.0)
    saidas = [e for e in ENVIADOS if e["path"] == "/fapi/v1/order"
              and e["params"].get("reduceOnly")]
    check("m7_time_stop_sai_com_reduce_only_de_verdade",
          fechou is True and len(saidas) == 1
          and saidas[0]["params"]["newClientOrderId"] == f"cw-ts-{trade_id}"
          and saidas[0]["params"]["side"] == "SELL",
          f"{fechou} {ENVIADOS}")

    # Mesma saída BLOQUEADA quando o símbolo é manual (ownership no caller).
    ack_saida = await criar_ack(trade_symbol)
    EXCHANGE["positions"] = [posicao(trade_symbol)]
    ENVIADOS.clear()
    ASSINADOS.clear()
    with patch.object(tms, "_resolve_trade_timeframe",
                      AsyncMock(return_value="4h")), \
            patch.object(tms, "_fetch_exchange_position",
                         AsyncMock(return_value=(1.0, "long"))):
        async with db.get_session() as session:
            trade_vivo = (await session.execute(select(RealTrade).where(
                RealTrade.id == trade_id))).scalar_one()
            fechou_manual = await tms._check_time_stop(trade_vivo, 1.0)
    check("m7_saida_em_simbolo_manual_nao_muta_nada",
          fechou_manual is False
          and [e for e in ENVIADOS if e["method"] == "POST"] == [],
          f"{fechou_manual} {ENVIADOS}")
    await limpar_acks()
    EXCHANGE["positions"] = []

    # ── m8: guard de proteção vencendo depois do throttle ⇒ zero mutação ─
    ENVIADOS.clear()
    ASSINADOS.clear()
    chamadas_guard = {"n": 0}

    async def guard_que_vence():
        chamadas_guard["n"] += 1
        return chamadas_guard["n"] < 2

    protecao_vencida = await bss.place_protection_orders(
        BETA, "Buy", 1.0, stop_loss=95.0, mutation_guard=guard_que_vence)
    check("m8_guard_vencido_apos_throttle_nao_muta",
          protecao_vencida.get("sl_ok") is False
          and [e for e in ENVIADOS if e["method"] == "POST"] == []
          and chamadas_guard["n"] >= 2,
          f"{str(protecao_vencida)[:200]} {ENVIADOS} {chamadas_guard}")

    # ── m9: fonte de funding indisponível ⇒ fail-closed preservado ───────
    from services import financial_total_service as fts
    funding_sym = await simbolo_fresco("FND")
    abertos_funding = await rodar_ciclo(
        [rec_sintetica(funding_sym)],
        extra_patches=[
            patch.object(fts, "accounting_total_enabled", return_value=True),
            patch.object(fts, "fresh_total",
                         AsyncMock(side_effect=RuntimeError("fonte caiu"))),
        ])
    check("m9_funding_indisponivel_mantem_fail_closed",
          abertos_funding == 0 and posts_de_entrada() == [],
          f"{abertos_funding} {ENVIADOS} {SKIPS}")

    # ── m10: falha/recuperação/boot ⇒ sem auto-resume falso nem bloqueio
    #        eterno do caminho positivo
    await limpar_trades()
    await mps.register_validation_failure(reason="fonte caiu (m10)", context=None)
    epoca_falha = await epoca_conta()
    bloq_sym = await simbolo_fresco("BOOT")
    abertos_falha = await rodar_ciclo([rec_sintetica(bloq_sym)])
    check("m10_falha_bloqueia_entradas_novas",
          epoca_falha["blocked"] is True and abertos_falha == 0
          and posts_de_entrada() == [],
          f"{epoca_falha} {abertos_falha} {SKIPS}")
    mps.reset_local_validation_state()
    recuperado = await rodar_ciclo([rec_sintetica(bloq_sym)])
    check("m10_boot_zerado_nao_auto_resume",
          recuperado == 0 and posts_de_entrada() == [],
          f"{recuperado} {ENVIADOS} {SKIPS}")
    # Ciclo oficial completo (captura → GET fresco → validação) recupera. O
    # latch/pausa que a falha armou é liberado pelos próprios owners.
    for dono in ("manual", "p03", "legacy"):
        sts.clear_execution_quarantine(owner=dono)
    ers._p03_latch_armed = False
    await limpar_pausa()
    contexto_rec = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
    observacao_rec = await mps.observe_positions()
    veredito_rec = await mps.revalidate_active(observation=observacao_rec,
                                               context=contexto_rec)
    epoca_rec = await epoca_conta()
    pos_sym = await simbolo_fresco("POS")
    abertos_rec = await rodar_ciclo([rec_sintetica(pos_sym)])
    check("m10_ciclo_oficial_recupera_o_caminho_positivo",
          veredito_rec["ok"] is True
          and veredito_rec.get("account_unblocked") is True
          and epoca_rec["blocked"] is False and abertos_rec == 1
          and len(posts_de_entrada()) == 1,
          f"{veredito_rec} {epoca_rec} {abertos_rec} {SKIPS}")

    # ── m11: F4 — carteira de 1,7 s envelhece a evidência ⇒ ZERO POST ────
    f4_sym = await simbolo_fresco("TTL")

    async def carteira_lenta():
        await asyncio.sleep(1.7)
        return {"available_usd": 8_000.0, "as_of_ms": ms(),
                "observed_start_ms": ms() - 5, "observed_end_ms": ms(),
                "quality": "live", "source": "live"}

    abertos_f4 = await rodar_ciclo(
        [rec_sintetica(f4_sym)],
        extra_patches=[patch.object(sts, "_free_margin_snapshot",
                                    carteira_lenta)])
    check("m11_proposta_vencida_nao_envia_ordem",
          abertos_f4 == 0 and posts_de_entrada() == []
          and posts_de_protecao() == []
          and any("STALE" in str(r) or "EXPIRED" in str(r)
                  for _, r in SKIPS),
          f"{abertos_f4} {ENVIADOS} {SKIPS}")
    intencao_f4 = await intencao_de(f4_sym)
    check("m11_recusa_preserva_reserva_sem_fill_fantasma",
          intencao_f4 is not None
          and intencao_f4["state"] in ("TERMINAL", "SENDING", "RESERVED")
          and intencao_f4["real_trade_id"] is None,
          str(intencao_f4))

    print(f"MANUAL_SINGLE_PG_OK: {len(CHECKS)} verificações")
    for item in patches:
        item.stop()
    await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
