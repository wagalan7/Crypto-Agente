"""Lote 01 — P&L líquido, funding, total e transferência, em PostgreSQL real.

`LOTE01_TEST_SOCKET` aponta para /tmp/cw-lote01-sock.* criado pelo runner.
Cluster descartável UTF-8, socket Unix, TCP/DNS bloqueados, driver async real.

Cobre o que os harnesses existentes NÃO cobrem (R05C já prova persistência,
concorrência e conflito do ledger; R05-clock prova a espera pela lock e o corte
de `statement_timestamp()`; margem prova reserva/readmissão/carteira):

1. comissão em OUTRO ativo pelo ciclo REAL de persistência: linha fica FORA do
   `accounting_total` e ENTRA quando a conversão registrada pela corretora é
   confirmada (merge com bloqueio de linha, restart idempotente, CONFLICT);
2. `accounting_total` POSITIVO com funding confirmado, entrada + duas parciais +
   runner, com total exato e sem dupla contagem de fill/funding;
3. transferência reserva → posição → P&L: a parcela não desaparece nem é contada
   duas vezes, com fechamento ENTRE as parcelas e reserva concorrente (duas
   conexões, barreira em `pg_locks`).

Nenhuma chamada real à exchange: o ledger de income/userTrades é sintético.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("LOTE01_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-lote01-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = ("postgresql+asyncpg://lote01@/lote01db?host="
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
            raise AssertionError("TCP proibido no Lote 01")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no Lote 01")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no Lote 01")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "c" * 64


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def agora() -> datetime:
    return datetime.now(timezone.utc)


def ms(valor: datetime) -> int:
    return int(valor.timestamp() * 1000)


async def run():
    from sqlalchemy import select, text
    import db
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot
    from services import execution_accounting_service as ea
    from services import financial_total_service as fts

    await db.init_db()

    abertura = agora() - timedelta(hours=2)
    fechamento = agora() - timedelta(minutes=30)

    async def criar_snapshot():
        async with db.get_session() as session:
            linha = RecommendationSnapshot(
                symbol="BTC/USDT:USDT", timeframe="4h", tier="A",
                direction="long", entry=100.0, stop_loss=95.0, tp1=105.0,
                tp2=110.0, score=90.0, risk_reward=2.0, leverage=5,
                risk_pct=1.0, stop_distance_pct=5.0, status="open",
                created_at=abertura)
            session.add(linha)
            await session.commit()
            return int(linha.id)

    async def criar_trade(snap_id, *, symbol="BTC/USDT:USDT", status="closed"):
        """RealTrade `auto` fechada, com contabilidade no contrato R05C."""
        async with db.get_session() as session:
            trade = RealTrade(
                recommendation_id=snap_id, symbol=symbol, side="long",
                source="auto", status=status, entry_price=100.0, qty=1.0,
                qty_initial=1.0, planned_stop=95.0, leverage=5,
                exchange="binance", exchange_order_id="o7001",
                client_order_id="cw-entry-lote01",
                opened_at=abertura, closed_at=(fechamento if status != "open"
                                               else None),
                execution_accounting=ea.empty_accounting(identity={
                    "exchange": "binance", "symbol": "BTCUSDT", "side": "long",
                    "position_side": "BOTH", "entry_order_id": "o7001",
                    "entry_client_order_id": "cw-entry-lote01",
                    "account_scope": ESCOPO}))
            session.add(trade)
            await session.commit()
            return int(trade.id)

    def fill(exec_id, *, side, price, qty, realized, commission, asset,
             order_id, instante):
        bruto = {"id": exec_id, "orderId": order_id, "symbol": "BTCUSDT",
                 "positionSide": "BOTH", "side": side, "price": price,
                 "qty": qty, "realizedPnl": realized,
                 "commission": commission, "commissionAsset": asset,
                 "time": str(instante)}
        normalizado, motivo = ea.normalize_fill(bruto, exchange="binance")
        assert normalizado is not None, motivo
        return normalizado

    def ordem(order_id, *, side, status="FILLED", reduce_only=False, qty="1"):
        bruto = {"orderId": order_id, "symbol": "BTCUSDT", "side": side,
                 "positionSide": "BOTH", "status": status, "type": "MARKET",
                 "reduceOnly": reduce_only, "executedQty": qty,
                 "clientOrderId": ("cw-entry-lote01" if not reduce_only
                                   else f"cw-exit-{order_id}"),
                 "updateTime": ms(fechamento)}
        # O papel é atribuído pelo coletor real depois de provar a ordem; aqui a
        # fixture reproduz o MESMO rótulo que ele grava.
        return {**ea.normalize_order(bruto),
                "role": "exit" if reduce_only else "entry"}

    #: Entrada + DUAS parciais + runner: quatro fills, uma só comissão em BNB.
    entrada_ms = ms(abertura) + 1_000
    p1_ms = ms(abertura) + 2_000
    p2_ms = ms(abertura) + 3_000
    runner_ms = ms(fechamento) - 1_000

    def conjunto_de_fills():
        return [
            fill("7001", side="BUY", price="100", qty="3", realized="0",
                 commission="0.001", asset="BNB", order_id="o7001",
                 instante=entrada_ms),
            fill("7002", side="SELL", price="105", qty="1", realized="5",
                 commission="0.05", asset="USDT", order_id="o7002",
                 instante=p1_ms),
            fill("7003", side="SELL", price="106", qty="1", realized="6",
                 commission="0.06", asset="USDT", order_id="o7003",
                 instante=p2_ms),
            fill("7004", side="SELL", price="110", qty="1", realized="10",
                 commission="0.07", asset="USDT", order_id="o7004",
                 instante=runner_ms),
        ]

    def conjunto_de_ordens():
        return [ordem("o7001", side="BUY", qty="3"),
                ordem("o7002", side="SELL", reduce_only=True),
                ordem("o7003", side="SELL", reduce_only=True),
                ordem("o7004", side="SELL", reduce_only=True)]

    funding_itens = [
        {"incomeType": "FUNDING_FEE", "tranId": "5001", "income": "-0.20",
         "asset": "USDT", "symbol": "BTCUSDT", "time": str(p1_ms)},
        {"incomeType": "FUNDING_FEE", "tranId": "5002", "income": "0.05",
         "asset": "USDT", "symbol": "BTCUSDT", "time": str(p2_ms)},
    ]

    def observacao(*, com_conversao=None, com_funding=True,
                   fills_extra=(), funding_extra=()):
        """Observação COMPLETA montada pelos helpers reais do serviço."""
        acc = ea.empty_accounting(identity={
            "exchange": "binance", "symbol": "BTCUSDT", "side": "long",
            "position_side": "BOTH", "entry_order_id": "o7001",
            "entry_client_order_id": "cw-entry-lote01",
            "account_scope": ESCOPO})
        funding = []
        if com_funding:
            for bruto in list(funding_itens) + list(funding_extra):
                item, motivo = ea.normalize_funding(bruto)
                assert item is not None, motivo
                funding.append(item)
        acc = ea.merge_accounting(
            acc, fills=list(conjunto_de_fills()) + list(fills_extra),
            orders=conjunto_de_ordens(), funding=funding,
            fee_conversions=([com_conversao] if com_conversao else ()))
        acc["exclusive_exposure"] = True
        acc["execution_proof"] = {"window_key": str(ms(fechamento)),
                                  "complete": True,
                                  "observed_at": fechamento.isoformat()}
        acc["funding_proof"] = {"key": f"{entrada_ms}:{runner_ms}",
                                "complete": bool(com_funding)}
        return ea.finalize_accounting(
            acc, entry_order_ids=["o7001"],
            exit_order_ids=["o7002", "o7003", "o7004"],
            fills_window_complete=True, funding_window_complete=bool(com_funding),
            position_flat=True, planned_stop=95.0)

    def conversao(**mudancas):
        campos = dict(
            account_scope=ESCOPO, exchange="binance",
            fill_key=ea.fill_key("binance", "BTCUSDT", "BOTH", "7001"),
            fee_asset="BNB", fee_qty="0.001", settlement_value="0.42",
            price="420", source=ea.FEE_SOURCE_BROKER,
            price_basis="COMMISSION_LEDGER_SETTLEMENT_VALUE",
            fill_time_ms=entrada_ms, observed_start_ms=ms(abertura),
            observed_end_ms=ms(fechamento))
        campos.update(mudancas)
        return ea.build_fee_conversion(**campos)

    async def ledger_do_trade(trade_id):
        async with db.get_session() as session:
            return (await session.execute(select(
                RealTrade.execution_accounting).where(
                RealTrade.id == trade_id))).scalar_one()

    async def total_da_janela():
        return await fts.fresh_total(
            db.get_session, account_scope=ESCOPO,
            since=abertura - timedelta(hours=1), until=agora())

    # ══════════════════════════════════════════════════════════════════════
    #  1. Comissão em outro ativo pelo ciclo REAL de persistência
    # ══════════════════════════════════════════════════════════════════════
    snap = await criar_snapshot()
    trade_id = await criar_trade(snap)
    obs_sem_conversao = observacao()
    check("l01_net_trade_bloqueado_por_comissao_em_outro_ativo",
          obs_sem_conversao["totals"]["net_trade"] is None
          and obs_sem_conversao["totals"]["net_trade_reason_code"]
          == "FEE_ASSET_CONVERSION_UNAVAILABLE"
          and obs_sem_conversao["totals"]["fee_assets_unconverted"] == ["BNB"],
          str(obs_sem_conversao["totals"])[:260])

    aplicado = await ea.apply_accounting(trade_id, obs_sem_conversao)
    ledger = await ledger_do_trade(trade_id)
    total_bloqueado = await total_da_janela()
    # A comissão não convertida impede o CONFIRMED da própria linha; o total,
    # por consequência, a exclui por `NOT_CONFIRMED` e NÃO fica completo.
    check("l01_linha_fica_fora_do_total_com_comissao_nao_convertida",
          aplicado.get("ok") is True
          and ledger["state"] == ea.STATE_PARTIAL
          and ledger["reason_code"] == "FEE_ASSET_CONVERSION_UNAVAILABLE"
          and ledger["totals"]["fee_assets_unconverted"] == ["BNB"]
          and ledger["totals"]["net_trade"] is None
          and total_bloqueado["state"] != fts.STATE_COMPLETE
          and total_bloqueado["exclusion_reasons"].get(fts.NOT_CONFIRMED) == 1
          and total_bloqueado["total_net_including_funding"] is None,
          f"{aplicado} {total_bloqueado['exclusion_reasons']}")

    # Conversão REGISTRADA pela corretora chega pelo mesmo merge com lock.
    prova = conversao()
    obs_com_conversao = observacao(com_conversao=prova)
    aplicado2 = await ea.apply_accounting(trade_id, obs_com_conversao)
    ledger2 = await ledger_do_trade(trade_id)
    total_liberado = await total_da_janela()
    # gross 21 − taxas USDT 0.18 − conversão 0.42 = 20.40 ; funding −0.15
    check("l01_conversao_confirmada_persiste_e_desbloqueia_o_net_trade",
          aplicado2.get("ok") is True
          and ledger2["fee_conversions"][prova["fill_key"]]["quality"]
          == ea.FEE_QUALITY_CONFIRMED
          and Decimal(ledger2["totals"]["net_trade"]) == Decimal("20.40")
          and ledger2["totals"]["fee_assets_unconverted"] == []
          and ledger2["totals"]["fee_conversion_state"] == ea.FEE_CONVERSION_RESOLVED
          and ledger2["state"] == ea.STATE_CONFIRMED
          and ledger2["funding_state"] == ea.FUNDING_CONFIRMED,
          f"{aplicado2} {str(ledger2['totals'])[:300]}")
    check("l01_total_com_funding_confirmado_fica_positivo_e_exato",
          total_liberado["state"] == fts.STATE_COMPLETE
          and total_liberado["rows_confirmed"] == 1
          and abs(total_liberado["total_net_including_funding"] - 20.25) < 1e-9,
          str(total_liberado)[:300])

    # Restart idempotente: reaplicar a MESMA observação não duplica nem soma.
    aplicado3 = await ea.apply_accounting(trade_id, obs_com_conversao)
    ledger3 = await ledger_do_trade(trade_id)
    total_reaplicado = await total_da_janela()
    check("l01_restart_idempotente_nao_duplica_conversao_nem_valor",
          aplicado3.get("ok") is True
          and len(ledger3["fee_conversions"]) == 1
          and Decimal(ledger3["totals"]["net_trade"]) == Decimal("20.40")
          and ledger3["conflicts"] == []
          and abs(total_reaplicado["total_net_including_funding"] - 20.25) < 1e-9,
          f"{len(ledger3['fee_conversions'])} {str(ledger3['totals'])[:200]}")

    # Evidência DIVERGENTE para o mesmo fill: CONFLICT preservando a original.
    divergente = conversao(settlement_value="9.99", price="9990")
    await ea.apply_accounting(trade_id, observacao(com_conversao=divergente))
    ledger4 = await ledger_do_trade(trade_id)
    total_conflito = await total_da_janela()
    check("l01_evidencia_divergente_vira_conflito_preservando_a_original",
          ledger4["fee_conversions"][prova["fill_key"]]["settlement_value"]
          == "0.42"
          and any(c.get("kind") == "FEE_CONVERSION"
                  for c in ledger4["conflicts"])
          and total_conflito["exclusion_reasons"].get(fts.LEDGER_CONFLICT) == 1
          and total_conflito["state"] != fts.STATE_COMPLETE,
          f"{ledger4['conflicts']} {total_conflito['exclusion_reasons']}")

    # ══════════════════════════════════════════════════════════════════════
    #  2. Duplicatas não somam duas vezes (fill, funding e parcial)
    # ══════════════════════════════════════════════════════════════════════
    snap2 = await criar_snapshot()
    trade2 = await criar_trade(snap2)
    duplicados = observacao(
        com_conversao=conversao(),
        fills_extra=[dict(f) for f in conjunto_de_fills()],
        funding_extra=[dict(funding_itens[0])])
    await ea.apply_accounting(trade2, duplicados)
    ledger_dup = await ledger_do_trade(trade2)
    check("l01_duplicatas_de_fill_e_funding_nao_dobram_o_total",
          Decimal(ledger_dup["totals"]["net_trade"]) == Decimal("20.40")
          and Decimal(ledger_dup["totals"]["funding_net"]) == Decimal("-0.15")
          and len(ledger_dup["fills"]) == 4
          and len(ledger_dup["funding"]) == 2,
          f"{str(ledger_dup['totals'])[:220]} fills={len(ledger_dup['fills'])}")

    # Ledger INCOMPLETO (um trade pendente na janela) não produz total completo.
    snap3 = await criar_snapshot()
    trade3 = await criar_trade(snap3)
    await ea.apply_accounting(trade3, observacao(com_funding=False))
    total_incompleto = await total_da_janela()
    check("l01_ledger_incompleto_na_janela_impede_total_completo",
          total_incompleto["state"] != fts.STATE_COMPLETE
          and total_incompleto["rows_considered"] >= 3,
          str(total_incompleto)[:260])

    # ══════════════════════════════════════════════════════════════════════
    #  3. Transferência reserva → posição → P&L, com duas conexões
    # ══════════════════════════════════════════════════════════════════════
    from services import entry_intent_service as intents
    from services import financial_risk_service as frs

    # Fixture INDEPENDENTE para a admissão: as linhas PARCIAIS/em conflito dos
    # casos acima tornariam o P&L do dia UNKNOWN por contrato (correto), então
    # esta seção começa com o portfólio limpo — limpeza ENTRE casos, nunca no
    # meio de um caso.
    async def limpar_portfolio():
        async with db.get_session() as session:
            await session.execute(text("DELETE FROM entry_intents"))
            await session.execute(text("DELETE FROM real_trades"))
            await session.commit()

    await limpar_portfolio()

    async def liberar_epoca():
        async with db.get_session() as session:
            await session.execute(text(
                "INSERT INTO account_margin_epochs (account_scope, exchange, "
                "market, generation, manual_validation_generation, "
                "manual_validation_blocked, updated_at) VALUES "
                "(:s,'binance','usdm_futures',0,0,false,now()) "
                "ON CONFLICT (account_scope, exchange, market) DO UPDATE SET "
                "manual_validation_blocked=false"), {"s": ESCOPO})
            await session.commit()

    await liberar_epoca()

    def identidade(sufixo):
        return intents.EntryIdentity(
            account_ref=ESCOPO, exchange="binance",
            symbol=f"L01{sufixo}-USDT-USDT", quote="USDT", side="long",
            position_side="BOTH", timeframe="4h", playbook="CHAMPION_LEGACY",
            playbook_version="SCORE_V2", purpose="ENTRY",
            trigger_candle_ms=1_770_000_000_000 + sufixo)

    async def carteira(requerido):
        async with db.get_session() as session:
            geracao = await intents.current_margin_generation(
                session, account_ref=ESCOPO, exchange="binance",
                market="usdm_futures")
        instante = ms(agora())
        return intents.MarginGate(
            available_usd=10_000.0, required_usd=requerido, as_of_ms=instante,
            observed_start_ms=instante - 5, observed_end_ms=instante,
            quality="live", complete=True, account_ref=ESCOPO,
            exchange="binance", market="usdm_futures", generation=geracao)

    ident_a = identidade(1)
    reserva = await intents.reserve(
        db.get_session, ident_a, {"entry": 100.0, "stop_loss": 95.0},
        owner="lote01", margin=await carteira(50.0))
    async with db.get_session() as session:
        visao_reservada = await frs.admission_snapshot(
            session, account_ref=ESCOPO, exchange="binance")
    # A reserva aparece UMA vez na mesma instrução, com a margem reservada
    # (o risco nominal só é gravado quando há orçamento/capacidade na admissão).
    check("l01_reserva_aparece_na_admissao_uma_vez",
          reserva.granted is True
          and visao_reservada.get("quality") == "OK"
          and visao_reservada.get("pending_count") == 1
          and float(visao_reservada.get("pending_margin_usd") or 0) > 0,
          f"{reserva.decision} {str(visao_reservada)[:240]}")

    # A MESMA intenção excluída da soma (readmissão da própria reserva).
    async with db.get_session() as session:
        visao_propria = await frs.admission_snapshot(
            session, account_ref=ESCOPO, exchange="binance",
            exclude_intent_key=ident_a.intent_key)
    check("l01_propria_reserva_nao_conta_duas_vezes_na_readmissao",
          visao_propria.get("pending_count") == 0,
          str(visao_propria)[:240])

    # Transferência: a intenção vira posição (RealTrade aberta) e a reserva sai
    # da soma de pendentes sem que a exposição desapareça.
    snap4 = await criar_snapshot()
    trade_aberto = await criar_trade(snap4, symbol="L01/USDT:USDT",
                                     status="open")
    vinculou = await intents.mark_confirmed(
        db.get_session, ident_a.intent_key, real_trade_id=trade_aberto,
        reason="FILL_CONFIRMED")
    async with db.get_session() as session:
        visao_transferida = await frs.admission_snapshot(
            session, account_ref=ESCOPO, exchange="binance")
    check("l01_transferencia_reserva_para_posicao_sem_sumir_nem_duplicar",
          vinculou is True
          and visao_transferida.get("pending_count") == 0
          and int(visao_transferida.get("open_positions") or 0) >= 1,
          f"{vinculou} {str(visao_transferida)[:240]}")

    # Reserva CONCORRENTE real (duas conexões, serialização pela advisory lock
    # que a própria `reserve` toma). A prova da ESPERA observada em `pg_locks`
    # já é feita por `pg_integration_r05_clock.py`; aqui o que se prova é que
    # duas reservas simultâneas não se perdem nem contam a mesma capacidade
    # duas vezes.
    carteira_a = await carteira(50.0)
    carteira_b = await carteira(50.0)
    primeira, segunda = await asyncio.gather(
        intents.reserve(db.get_session, identidade(2),
                        {"entry": 100.0, "stop_loss": 95.0},
                        owner="lote01-a", margin=carteira_a),
        intents.reserve(db.get_session, identidade(3),
                        {"entry": 100.0, "stop_loss": 95.0},
                        owner="lote01-b", margin=carteira_b))
    async with db.get_session() as session:
        pendentes = int((await session.execute(text(
            "SELECT count(*) FROM entry_intents WHERE account_ref=:s "
            "AND state = ANY(:e)"),
            {"s": ESCOPO, "e": list(intents.PENDING_STATES)})).scalar() or 0)
        visao_concorrente = await frs.admission_snapshot(
            session, account_ref=ESCOPO, exchange="binance")
    # As duas leram a carteira na MESMA geração: a primeira reserva incrementa a
    # época e a segunda é recusada como observação SUPERADA. Capacidade não é
    # gasta duas vezes, e a admissão conta exatamente a que foi concedida.
    concedidas = [r for r in (primeira, segunda) if r.granted]
    recusadas = [r for r in (primeira, segunda) if not r.granted]
    check("l01_concorrencia_nao_gasta_a_mesma_capacidade_duas_vezes",
          len(concedidas) == 1 and len(recusadas) == 1
          and recusadas[0].reason == intents.MARGIN_SUPERSEDED
          and pendentes == 1
          and visao_concorrente.get("pending_count") == 1,
          f"{primeira.decision}/{primeira.reason} "
          f"{segunda.decision}/{segunda.reason} pendentes={pendentes} "
          f"snapshot={visao_concorrente.get('pending_count')}")
    # Fechamento ENTRE parcelas: a linha fechada depois do BEGIN entra na janela
    # da admissão (corte por `statement_timestamp()`), sem sumir das abertas.
    async with db.get_session() as session:
        await session.execute(text(
            "UPDATE real_trades SET status='closed', closed_at=now(), "
            "pnl_usd=-1.5 WHERE id=:i"), {"i": trade_aberto})
        await session.commit()
    async with db.get_session() as session:
        visao_pos_fechamento = await frs.admission_snapshot(
            session, account_ref=ESCOPO, exchange="binance")
    check("l01_fechamento_entre_parcelas_entra_na_janela_sem_sumir",
          visao_pos_fechamento.get("base") is not None,
          str(visao_pos_fechamento)[:240])

    print(f"LOTE01_FINANCEIRO_PG_OK: {len(CHECKS)} verificações")
    await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
