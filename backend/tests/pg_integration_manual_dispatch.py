"""Autoridade local e identidade do payload no envio final, em PostgreSQL real.

`MANUALDISPATCH_TEST_SOCKET` aponta para /tmp/cw-mdisp-sock.* criado pelo runner.
Converte em testes permanentes as reproduções da revisão de `001a6685`
(`referencias/probe_dispatch_pg.py` e `probe_binding.py`).

Reais: reserva, `mark_sending`, `register_dispatch`, admissão financeira,
proposta persistida, `check_ownership_in_session`, `authorize_dispatch`,
`_intent_final_authorization`, exame síncrono e `_signed_request` (assinatura
inclusive). Sintéticos: HTTP, conta/símbolo opacos, carteira e relógio.

RED medido na baseline `001a6685` antes da correção:
- B01: falha manual pública registrada em fence/pending DURANTE o
  `await session.rollback()` do `authorize_dispatch` ⇒ ainda 1 POST de entrada;
- C01: proposta persistida de DELTA aceitou `params.symbol=OTHERUSDT` ⇒ 1 POST.
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import sys
import time
from unittest.mock import patch

test_socket = os.environ.get("MANUALDISPATCH_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-mdisp-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = ("postgresql+asyncpg://mdisp@/mdispdb?host="
                              + test_socket)
for _nome in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BYBIT_API_KEY",
              "BYBIT_API_SECRET"):
    os.environ.pop(_nome, None)
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no envio final")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no envio final")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no envio final")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "d" * 64
DONO = "pacote-unico"


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


async def run():
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession
    import db
    from models.entry_intent import EntryIntent
    from models.recommendation_snapshot import RecommendationSnapshot  # noqa: F401
    from services import binance_signed_service as bss
    from services import entry_intent_service as intents
    from services import manual_position_service as mps
    from services import shadow_trade_service as sts
    from tests.test_manual_bot_single_closure import (ClienteHTTPFalso,
                                                      evidencia_quote,
                                                      respostas_padrao)

    await db.init_db()
    contador = {"n": 0}

    def identidade(simbolo="DELTA/USDT:USDT"):
        contador["n"] += 1
        return intents.EntryIdentity(
            account_ref=ESCOPO, exchange="binance", symbol=simbolo,
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            purpose="ENTRY", trigger_candle_ms=1_770_000_000_000 + contador["n"])

    async def liberar_conta():
        async with db.get_session() as session:
            await session.execute(text(
                "INSERT INTO account_margin_epochs (account_scope, exchange, "
                "market, generation, manual_validation_generation, "
                "manual_validation_blocked, updated_at) VALUES "
                "(:s,'binance','usdm_futures',0,0,false,now()) "
                "ON CONFLICT (account_scope, exchange, market) DO UPDATE SET "
                "manual_validation_blocked=false"), {"s": ESCOPO})
            await session.execute(text(
                "DELETE FROM manual_position_acks WHERE account_scope=:s"),
                {"s": ESCOPO})
            await session.commit()
        mps.reset_local_validation_state()

    async def carteira(requerido=20.0):
        async with db.get_session() as session:
            geracao = await intents.current_margin_generation(
                session, account_ref=ESCOPO, exchange="binance",
                market="usdm_futures")
        instante = ms()
        return intents.MarginGate(
            available_usd=10_000.0, required_usd=requerido, as_of_ms=instante,
            observed_start_ms=instante - 5, observed_end_ms=instante,
            quality="live", complete=True, account_ref=ESCOPO,
            exchange="binance", market="usdm_futures", generation=geracao)

    def campos_da_proposta(ident, *, tipo="LIMIT", preco=99.9, qty=1.0,
                           referencia=None):
        agora_ms = time.time() * 1000.0
        evidencia = evidencia_quote(agora_ms=agora_ms, preco=preco)
        if tipo == "MARKET":
            evidencia = {**evidencia, "order_type": "MARKET",
                         "time_in_force": None}
        return {
            "account_ref": ESCOPO, "exchange": "binance",
            "market": "usdm_futures", "intent_key": ident.intent_key,
            "symbol": ident.symbol, "side": "BUY", "position_side": "BOTH",
            "order_type": tipo,
            "time_in_force": "GTX" if tipo == "LIMIT" else None,
            "reduce_only": False, "dispatch_id": ident.client_order_id,
            "qty": qty, "price": preco if tipo == "LIMIT" else None,
            "stop": 99.0,
            "reference_price": referencia if tipo == "MARKET" else None,
            "leverage": 5, "evidence": evidencia,
            "limits": evidencia["limits"]}

    async def preparar(ident, *, tipo="LIMIT", preco=99.9, qty=1.0):
        """Fixture INDEPENDENTE e saudável até a proposta persistida."""
        await liberar_conta()
        reserva = await intents.reserve(
            db.get_session, ident, {"entry": 100.0, "stop_loss": 99.0},
            owner=DONO, margin=await carteira())
        if not reserva.granted:
            raise AssertionError(f"reserva não concedida: {reserva}")
        if not await intents.mark_sending(db.get_session, ident.intent_key,
                                          owner=DONO):
            raise AssertionError("mark_sending falhou")
        if not await intents.register_dispatch(
                db.get_session, ident.intent_key, owner=DONO,
                dispatch_id=ident.client_order_id):
            raise AssertionError("register_dispatch falhou")
        admitida = await intents.admit_final_risk(
            db.get_session, ident.intent_key, owner=DONO, risk_usd=1.0,
            margin=await carteira(),
            proposal=campos_da_proposta(ident, tipo=tipo, preco=preco,
                                        qty=qty, referencia=100.0))
        if not (admitida.granted and admitida.proposal):
            raise AssertionError(f"admissão sem proposta: {admitida}")
        return {"granted": True, "intent_key": ident.intent_key,
                "margin_generation": admitida.generation,
                "last_dispatch_id": ident.client_order_id,
                "identity": ident, "proposal": admitida.proposal}

    def payload_de(ident, *, tipo="LIMIT", preco=99.9, qty=1.0, **extra):
        base = {"symbol": bss.to_binance(ident.symbol), "side": "BUY",
                "type": tipo, "quantity": qty,
                "newClientOrderId": ident.client_order_id}
        if tipo == "LIMIT":
            base.update(price=preco, timeInForce="GTX")
        else:
            base.update(newOrderRespType="RESULT")
        base.update(extra)
        return base

    async def enviar(intent, params, *, acao="maker_entry", cliente=None):
        cliente = cliente or ClienteHTTPFalso(respostas_padrao())
        with patch.object(bss, "_build_signed_url", cliente.assinar), \
                patch.object(bss, "_get_client", return_value=cliente):
            resultado = await bss._signed_request(
                "POST", "/fapi/v1/order", dict(params),
                mutation={"symbol": intent["identity"].symbol, "action": acao,
                          "final_check": sts._intent_final_authorization(intent)})
        return resultado, cliente

    base_patches = [
        patch.object(mps, "current_account_scope", return_value=ESCOPO),
        patch.object(bss, "accounting_scope", lambda: ESCOPO),
        patch.object(sts, "_INTENT_OWNER", DONO),
        patch.object(bss, "is_configured", return_value=True),
        patch.object(bss, "_ban_until_ms", 0),
        patch.object(bss, "_throttle_until_ms", 0),
    ]
    for item in base_patches:
        item.start()

    async def estado_intencao(chave):
        async with db.get_session() as session:
            linha = (await session.execute(select(EntryIntent).where(
                EntryIntent.intent_key == chave))).scalar_one()
            return {"state": linha.state, "token": linha.margin_generation,
                    "dispatches": list(linha.dispatch_ids or [])}

    # ══════════════════════════════════════════════════════════════════════
    #  B06 — controle POSITIVO com as funções reais
    # ══════════════════════════════════════════════════════════════════════
    ident = identidade()
    intent = await preparar(ident)
    check("b06_precondicao_saudavel",
          mps.pending_validation_failure() is None
          and mps.local_validation_fence() == 0
          and mps.local_symbol_block(ident.symbol) is None,
          "fixture com autoridade local limpa")
    resultado, cliente = await enviar(intent, payload_de(ident))
    check("b06_controle_saudavel_envia_um_post",
          resultado.get("ok") is True
          and len(cliente.posts_de_entrada()) == 1
          and cliente.posts_de_entrada()[0]["params"]["timeInForce"] == "GTX",
          f"{str(resultado)[:200]} {cliente.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  C01 — proposta DELTA, params OTHER ⇒ ZERO POST por identidade
    # ══════════════════════════════════════════════════════════════════════
    ident_c1 = identidade()
    intent_c1 = await preparar(ident_c1)
    resultado_c1, cliente_c1 = await enviar(
        intent_c1, payload_de(ident_c1, symbol="OTHERUSDT"))
    check("c01_simbolo_divergente_nao_envia",
          resultado_c1.get("ok") is False
          and resultado_c1.get("_request_sent") is False
          and cliente_c1.posts_de_entrada() == []
          and "PROPOSAL" in str(resultado_c1.get("reason_code")),
          f"{str(resultado_c1)[:220]} {cliente_c1.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  C02 — quote diferente e positionSide incompatível
    # ══════════════════════════════════════════════════════════════════════
    ident_c2 = identidade()
    intent_c2 = await preparar(ident_c2)
    resultado_quote, cliente_quote = await enviar(
        intent_c2, payload_de(ident_c2, symbol="DELTAUSDC"))
    check("c02_quote_diferente_nao_envia",
          resultado_quote.get("ok") is False
          and cliente_quote.posts_de_entrada() == [],
          f"{str(resultado_quote)[:200]} {cliente_quote.chamadas}")
    resultado_ps, cliente_ps = await enviar(
        intent_c2, payload_de(ident_c2, positionSide="SHORT"))
    check("c02_position_side_incompativel_nao_envia",
          resultado_ps.get("ok") is False
          and cliente_ps.posts_de_entrada() == [],
          f"{str(resultado_ps)[:200]} {cliente_ps.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  C03 — campos indevidos e geometria adulterada
    # ══════════════════════════════════════════════════════════════════════
    ident_c3 = identidade()
    intent_c3 = await preparar(ident_c3)
    indevidos = [
        ("close_position_true", {"closePosition": "true"}),
        ("close_position_false", {"closePosition": "false"}),
        ("close_position_bool", {"closePosition": False}),
        ("close_position_zero", {"closePosition": 0}),
        ("reduce_only_true", {"reduceOnly": "true"}),
        ("reduce_only_false", {"reduceOnly": "false"}),
        ("reduce_only_bool", {"reduceOnly": False}),
        ("stop_price", {"stopPrice": 99.0}),
        ("coid", {"newClientOrderId": "cw-outro-coid"}),
        ("side", {"side": "SELL"}),
        ("qty_maior", {"quantity": 1.5}),
        ("qty_menor", {"quantity": 0.5}),
        ("preco", {"price": 100.5}),
        ("tipo", {"type": "MARKET"}),
        ("tif", {"timeInForce": "GTC"}),
    ]
    for rotulo, mudanca in indevidos:
        resultado_i, cliente_i = await enviar(
            intent_c3, payload_de(ident_c3, **mudanca))
        check(f"c03_{rotulo}_nao_envia",
              resultado_i.get("ok") is False
              and resultado_i.get("_request_sent") is False
              and cliente_i.posts_de_entrada() == []
              and "PROPOSAL" in str(resultado_i.get("reason_code")),
              f"{rotulo}: {str(resultado_i)[:180]} {cliente_i.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  C04 — equivalentes wire legítimos e MARKET/LIMIT saudáveis
    # ══════════════════════════════════════════════════════════════════════
    ident_c4 = identidade()
    intent_c4 = await preparar(ident_c4)
    resultado_ccxt, cliente_ccxt = await enviar(
        intent_c4, payload_de(ident_c4, symbol=ident_c4.symbol))
    check("c04_simbolo_ccxt_equivalente_e_aceito",
          resultado_ccxt.get("ok") is True
          and len(cliente_ccxt.posts_de_entrada()) == 1,
          f"{str(resultado_ccxt)[:200]} {cliente_ccxt.chamadas}")

    ident_c4b = identidade()
    intent_c4b = await preparar(ident_c4b, tipo="MARKET")
    resultado_market, cliente_market = await enviar(
        intent_c4b, payload_de(ident_c4b, tipo="MARKET"))
    check("c04_market_saudavel_e_aceito",
          resultado_market.get("ok") is True
          and len(cliente_market.posts_de_entrada()) == 1
          and cliente_market.posts_de_entrada()[0]["params"].get("price") is None,
          f"{str(resultado_market)[:200]} {cliente_market.chamadas}")

    ident_c4c = identidade()
    intent_c4c = await preparar(ident_c4c, tipo="MARKET")
    resultado_market_preco, cliente_mp = await enviar(
        intent_c4c, payload_de(ident_c4c, tipo="MARKET", price=100.0))
    check("c04_market_com_preco_artificial_nao_envia",
          resultado_market_preco.get("ok") is False
          and cliente_mp.posts_de_entrada() == [],
          f"{str(resultado_market_preco)[:200]} {cliente_mp.chamadas}")

    # positionSide omitido por um builder saudável (one-way/BOTH) é aceito.
    ident_c4d = identidade()
    intent_c4d = await preparar(ident_c4d)
    resultado_omitido, cliente_omitido = await enviar(
        intent_c4d, payload_de(ident_c4d))
    check("c04_position_side_omitido_e_saudavel",
          resultado_omitido.get("ok") is True
          and "positionSide" not in cliente_omitido.posts_de_entrada()[0]["params"],
          f"{str(resultado_omitido)[:200]}")

    # ══════════════════════════════════════════════════════════════════════
    #  B01 — falha pública durante o rollback final ⇒ ZERO POST
    # ══════════════════════════════════════════════════════════════════════
    ident_b1 = identidade()
    intent_b1 = await preparar(ident_b1)
    ownership_real = mps.check_ownership_in_session
    rollback_real = AsyncSession.rollback
    injecao = {"ligada": False, "tarefas": [], "ownership": None,
               "checkpoint": None}

    async def ownership_observado(session, **kwargs):
        veredito = await ownership_real(session, **kwargs)
        if kwargs.get("action") == "dispatch":
            injecao["ownership"] = veredito
            if veredito.get("allowed"):
                session.info["marca_pos_ownership"] = True
        return veredito

    async def rollback_com_barreira(session):
        if injecao["ligada"] and session.info.pop("marca_pos_ownership", False):
            injecao["ligada"] = False
            # Método PÚBLICO real: registra fence/pending SINCRONAMENTE e a
            # persistência fica esperando a mutex local real (detida abaixo).
            tarefa = asyncio.create_task(mps.register_validation_failure(
                reason="FALHA_DURANTE_ROLLBACK_FINAL"))
            injecao["tarefas"].append(tarefa)
            await asyncio.sleep(0)
            injecao["checkpoint"] = {
                "ownership": (injecao["ownership"] or {}).get("allowed"),
                "fence": mps.local_validation_fence(),
                "pending": mps.pending_validation_failure() is not None}
        await rollback_real(session)

    with patch.object(mps, "check_ownership_in_session", ownership_observado), \
            patch.object(AsyncSession, "rollback", rollback_com_barreira):
        async with mps.manual_writer_lock():
            injecao["ligada"] = True
            resultado_b1, cliente_b1 = await enviar(intent_b1,
                                                    payload_de(ident_b1))
            guarda_fresco = await mps.ownership_guard(
                ident_b1.symbol, action="place_order", require_fresh_proof=True)
        await asyncio.gather(*injecao["tarefas"])
    check("b01_falha_durante_rollback_tem_precondicao_e_zero_post",
          (injecao["checkpoint"] or {}).get("ownership") is True
          and injecao["checkpoint"]["fence"] >= 1
          and injecao["checkpoint"]["pending"] is True
          and resultado_b1.get("ok") is False
          and resultado_b1.get("_request_sent") is False
          and cliente_b1.posts_de_entrada() == []
          and guarda_fresco["allowed"] is False,
          f"{injecao['checkpoint']} {str(resultado_b1)[:200]} "
          f"{cliente_b1.chamadas} {guarda_fresco}")

    # ══════════════════════════════════════════════════════════════════════
    #  B02 — falha durante o __aexit__/cleanup da sessão ⇒ ZERO POST
    # ══════════════════════════════════════════════════════════════════════
    ident_b2 = identidade()
    intent_b2 = await preparar(ident_b2)
    close_real = AsyncSession.close
    injecao_b2 = {"ligada": False, "tarefas": []}

    async def close_com_barreira(session):
        if injecao_b2["ligada"] and session.info.pop("marca_pos_ownership", False):
            injecao_b2["ligada"] = False
            injecao_b2["tarefas"].append(asyncio.create_task(
                mps.register_validation_failure(reason="FALHA_DURANTE_CLEANUP")))
            await asyncio.sleep(0)
        await close_real(session)

    with patch.object(mps, "check_ownership_in_session", ownership_observado), \
            patch.object(AsyncSession, "close", close_com_barreira):
        async with mps.manual_writer_lock():
            injecao_b2["ligada"] = True
            resultado_b2, cliente_b2 = await enviar(intent_b2,
                                                    payload_de(ident_b2))
        await asyncio.gather(*injecao_b2["tarefas"])
    check("b02_falha_no_cleanup_da_sessao_nao_envia",
          resultado_b2.get("ok") is False
          and resultado_b2.get("_request_sent") is False
          and cliente_b2.posts_de_entrada() == [],
          f"{str(resultado_b2)[:200]} {cliente_b2.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  B03 — falha DEPOIS da autorização, antes do exame síncrono
    # ══════════════════════════════════════════════════════════════════════
    ident_b3 = identidade()
    intent_b3 = await preparar(ident_b3)
    autorizacao_real = intents.authorize_dispatch

    async def autoriza_e_falha(*args, **kwargs):
        veredito = await autorizacao_real(*args, **kwargs)
        if veredito.get("ok"):
            mps._advance_local_fence("FALHA_APOS_AUTORIZACAO")
        return veredito

    with patch.object(intents, "authorize_dispatch", autoriza_e_falha):
        resultado_b3, cliente_b3 = await enviar(intent_b3, payload_de(ident_b3))
    check("b03_falha_apos_autorizacao_nao_envia",
          resultado_b3.get("ok") is False
          and resultado_b3.get("_request_sent") is False
          and cliente_b3.posts_de_entrada() == [],
          f"{str(resultado_b3)[:200]} {cliente_b3.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  B04 — retry/throttle não ressuscitam a autorização antiga
    # ══════════════════════════════════════════════════════════════════════
    resultado_retry, cliente_retry = await enviar(intent_b3,
                                                  payload_de(ident_b3))
    estado_b3 = await estado_intencao(ident_b3.intent_key)
    check("b04_retry_com_falha_pendente_continua_negado",
          resultado_retry.get("ok") is False
          and cliente_retry.posts_de_entrada() == []
          and mps.pending_validation_failure() is not None
          and estado_b3["state"] == "SENDING",
          f"{str(resultado_retry)[:200]} {cliente_retry.chamadas} {estado_b3}")

    # ══════════════════════════════════════════════════════════════════════
    #  B05 — persistir/limpar pending não revive o token antigo;
    #         ciclo seguro + NOVA autorização funciona
    # ══════════════════════════════════════════════════════════════════════
    flush = await mps.flush_pending_validation_failure()
    fence_apos_flush = mps.local_validation_fence()
    resultado_antigo, cliente_antigo = await enviar(intent_b3,
                                                    payload_de(ident_b3))
    check("b05_token_antigo_nao_ressuscita_apos_flush",
          flush["ok"] is True and mps.pending_validation_failure() is None
          and resultado_antigo.get("ok") is False
          and cliente_antigo.posts_de_entrada() == [],
          f"{flush} fence={fence_apos_flush} {str(resultado_antigo)[:180]} "
          f"{cliente_antigo.chamadas}")

    ident_b5 = identidade()
    intent_b5 = await preparar(ident_b5)
    resultado_novo, cliente_novo = await enviar(intent_b5, payload_de(ident_b5))
    check("b05_nova_autorizacao_com_ciclo_seguro_envia",
          resultado_novo.get("ok") is True
          and len(cliente_novo.posts_de_entrada()) == 1,
          f"{str(resultado_novo)[:200]} {cliente_novo.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  C06 — proteção/redução BOT em outro símbolo × símbolo manual
    # ══════════════════════════════════════════════════════════════════════
    await liberar_conta()
    cliente_prot = ClienteHTTPFalso(respostas_padrao())
    with patch.object(bss, "_build_signed_url", cliente_prot.assinar), \
            patch.object(bss, "_get_client", return_value=cliente_prot), \
            patch.object(bss, "_round_price",
                         side_effect=lambda s, p: float(p)):
        protecao = await bss.place_protection_orders(
            "GAMA/USDT:USDT", "Buy", 1.0, stop_loss=95.0,
            client_order_id_prefix="cw-prot-outro")
    check("c06_protecao_bot_em_outro_simbolo_funciona",
          protecao.get("sl_ok") is True
          and len(cliente_prot.posts_de_protecao()) >= 1,
          f"{str(protecao)[:200]} {cliente_prot.chamadas}")

    # ══════════════════════════════════════════════════════════════════════
    #  Idempotência: reexecutar o MESMO despacho não duplica intenção/dispatch
    #  nem incremento econômico.
    # ══════════════════════════════════════════════════════════════════════
    ident_idem = identidade()
    intent_idem = await preparar(ident_idem)
    async with db.get_session() as session:
        geracao_antes = int((await session.execute(text(
            "SELECT generation FROM account_margin_epochs WHERE account_scope=:s"),
            {"s": ESCOPO})).scalar() or 0)
    primeiro, cliente_i1 = await enviar(intent_idem, payload_de(ident_idem))
    estado_i1 = await estado_intencao(ident_idem.intent_key)
    # Mesmo id efetivo registrado de novo (retry do MESMO envio).
    reregistro = await intents.register_dispatch(
        db.get_session, ident_idem.intent_key, owner=DONO,
        dispatch_id=ident_idem.client_order_id)
    segundo, cliente_i2 = await enviar(intent_idem, payload_de(ident_idem))
    estado_i2 = await estado_intencao(ident_idem.intent_key)
    async with db.get_session() as session:
        geracao_depois = int((await session.execute(text(
            "SELECT generation FROM account_margin_epochs WHERE account_scope=:s"),
            {"s": ESCOPO})).scalar() or 0)
        intencoes = int((await session.execute(text(
            "SELECT count(*) FROM entry_intents WHERE intent_key=:k"),
            {"k": ident_idem.intent_key})).scalar() or 0)
    check("idempotente_reexecucao_nao_duplica_despacho_nem_economia",
          primeiro.get("ok") is True and segundo.get("ok") is True
          and reregistro is True
          and estado_i1["dispatches"] == estado_i2["dispatches"]
          and len(estado_i2["dispatches"]) == 1
          and intencoes == 1 and geracao_depois == geracao_antes
          and len(cliente_i1.posts_de_entrada()) == 1
          and len(cliente_i2.posts_de_entrada()) == 1,
          f"{estado_i1} {estado_i2} intencoes={intencoes} "
          f"geracao={geracao_antes}→{geracao_depois}")

    print(f"MANUAL_DISPATCH_PG_OK: {len(CHECKS)} verificações")
    for item in base_patches:
        item.stop()
    await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
