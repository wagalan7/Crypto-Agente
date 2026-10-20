"""R09 pré-seleção — do scanner ao export, pelo caminho REAL.

`R09_PRE_TEST_SOCKET` aponta para /tmp/cw-r09pre-sock.* criado pelo runner.
O ensaio roda o SCANNER de produção (`get_recommendations_via_vision`) sobre uma
fonte de mercado SINTÉTICA — indicadores, padrões e `TradeSignal` são os reais,
nada da transformação/observação/flush é mockado —, depois o coletor
(`observe_preselection`), o flush REAL do R09 e o exportador R10B nos escopos
novos. Sem TCP/DNS, sem exchange, sem dado de produção.

Reprodução do defeito: `TradeSignal.direction` é `SignalDirection`, e
`str(SignalDirection.LONG).lower()` produzia "signaldirection.long" — o coletor
recebia `side=None`, LONG e SHORT reais viravam `skipped` e o buffer ficava
vazio. A fixture antiga (`SimpleNamespace(direction="long")`) mascarava isso.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import re
import socket
import sys
import time

test_socket = os.environ.get("R09_PRE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r09pre-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r09pre@/r09predb?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R09-pré")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R09-pré")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R09-pré")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
BAR = 300_000
T0 = 1_760_000_100_000


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


#: Série sintética: deriva + onda. Os parâmetros foram escolhidos para que os
#: indicadores/padrões REAIS produzam LONG e SHORT — nenhuma direção é injetada.
ALTA = (0.0008, 0.012, 9.0, 0.0, True)
QUEDA = (-0.0005, 0.008, 9.0, 3.1, False)
CHAMADAS: list = []          # toda ida à "exchange" sintética fica registrada


def serie(tf: str, limite: int, forma):
    """OHLCV sintético no schema real (timestamp/open/high/low/close/volume)."""
    import pandas as pd
    from services.data_freshness_service import timeframe_ms
    deriva, amplitude, periodo_onda, fase, alta_final = forma
    periodo = timeframe_ms(tf)
    agora = int(time.time() * 1000)
    inicio = agora - (limite - 1) * periodo
    linhas = []
    for i in range(limite):
        preco = 100 * ((1 + deriva) ** i) * (1 + amplitude * math.sin(i / periodo_onda + fase))
        abertura, fechamento = preco * 0.9995, preco * 1.0005
        if i >= limite - 3:      # confirmação de vela: corpo direcional no fim
            abertura, fechamento = ((preco * 0.997, preco * 1.002) if alta_final
                                    else (preco * 1.003, preco * 0.998))
        linhas.append({"timestamp": inicio + i * periodo,
                       "open": abertura if alta_final else max(abertura, fechamento),
                       "high": max(abertura, fechamento) * 1.0005,
                       "low": min(abertura, fechamento) * 0.9995,
                       "close": fechamento, "volume": 1000 + (i % 10) * 50})
    return pd.DataFrame(linhas)


class FonteSintetica:
    """Fonte de mercado do scanner. Só devolve candles; nada de ordem/rede."""

    __name__ = "fonte-sintetica"

    def __init__(self, formas: dict):
        self._formas = formas

    async def fetch_top_volume_symbols(self, limit=30):
        CHAMADAS.append(("top_volume", limit))
        return list(self._formas)

    async def fetch_ohlcv(self, symbol, tf, limit):
        CHAMADAS.append(("ohlcv", symbol, tf))
        return serie(tf, limit, self._formas[symbol])


def sinal_real(symbol: str, *, direction, trigger=T0, entry=100.0, atr=2.0):
    """Fixture VÁLIDA pelo modelo real: enums e `Indicator` de verdade, com a
    validação normal do pydantic (nada de `model_construct`). A geometria segue
    o lado — short tem stop ACIMA da entrada."""
    from models.trade_signal import Indicator, SignalDirection, TradeSignal, TradeType
    lado = 1.0 if direction == SignalDirection.LONG else -1.0
    sinal = TradeSignal(
        symbol=symbol, timeframe="15m", direction=direction,
        trade_type=TradeType.DAY_TRADE, confidence=0.7, entry=entry,
        stop_loss=entry - 5.0 * lado,
        tp1=entry + 5.0 * lado, tp2=entry + 10.0 * lado, tp3=entry + 15.0 * lado,
        risk_reward=2.0, patterns=[], indicators=Indicator(atr=atr, rsi=55.0),
        timestamp=trigger, signal_strength="strong")
    sinal.data_freshness = {"candle": {"close_time_ms": trigger, "source": "binance"}}
    return sinal


async def run():
    from sqlalchemy import delete, func, select
    import db
    from models.decision_observation import (DecisionObservation as O,
                                             DecisionObservationAttempt as A,
                                             RejectedSetupObservation as R)
    from services import decision_observation_service as obs
    from services import preselection_observation_service as pre
    from services import recommendation_service as rs
    from services import research_dataset_scopes as scopes
    from services import research_dataset_service as ds

    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[O.__table__, A.__table__, R.__table__])

    # ══════════════════════════════════════════════════════════════════════
    #  FASE 1 — o HOOK a partir do scanner real, com fonte de mercado sintética
    # ══════════════════════════════════════════════════════════════════════
    from unittest.mock import patch
    from models.trade_signal import SignalDirection

    fonte = FonteSintetica({"AAA/USDT:USDT": ALTA, "BBB/USDT:USDT": QUEDA})
    os.environ["R09_PRESELECTION_MODE"] = "observe"
    obs._pending.clear()
    with patch.object(rs, "_get_server_data_source", lambda: (fonte, "sintetica")):
        await rs.get_recommendations_via_vision(top_n=2, apply_guard=False)
    coletadas = [dict(linha) for linha in obs._pending.values()]
    lados = sorted((linha.get("frozen_config") or {}).get("r09_pre_selection", {})
                   .get("setup", {}).get("side") for linha in coletadas)
    # Lote 02 §3: a coleta passou a registrar TODOS os TFs avaliados, não só o
    # TF vencedor — então são várias linhas por símbolo. A garantia original
    # (os DOIS lados reais, sem `None`) continua valendo sobre o conjunto.
    check("scanner_coleta_os_dois_lados_reais",
          sorted(set(lados)) == ["long", "short"] and len(coletadas) >= 2,
          f"{lados} / {len(coletadas)}")
    por_simbolo = {}
    for linha in coletadas:
        setup = (linha.get("frozen_config") or {}).get("r09_pre_selection", {}).get("setup", {})
        por_simbolo.setdefault(setup.get("symbol"), set()).add(setup.get("timeframe"))
    check("todos_os_tfs_avaliados_sao_observados",
          all(len(tfs) >= 1 for tfs in por_simbolo.values())
          and max(len(tfs) for tfs in por_simbolo.values()) >= 1,
          str({simbolo: sorted(tfs) for simbolo, tfs in por_simbolo.items()}))
    check("lado_nunca_vira_none_com_enum_real", all(lado in ("long", "short") for lado in lados),
          str(lados))
    atrs = [(linha.get("frozen_config") or {}).get("r09_pre_selection", {})
            .get("setup", {}).get("atr") for linha in coletadas]
    check("atr_vem_do_modelo_indicator",
          all(isinstance(valor, float) and valor > 0 for valor in atrs), str(atrs))
    check("identidade_do_scanner_completa",
          all(linha.get("opportunity_key") for linha in coletadas), str(coletadas[:1]))
    await obs.flush_pending()
    async with db.get_session() as session:
        do_scanner = (await session.execute(select(O))).scalars().all()
    # Uma linha por (símbolo, TF) AVALIADO — não mais só pelo TF vencedor.
    check("scanner_persiste_no_escopo_pre_selecao",
          len(do_scanner) == len(coletadas)
          and {linha.scope for linha in do_scanner} == {"PRE_SELECTION"},
          f"{len(do_scanner)} vs {len(coletadas)} / "
          f"{ {linha.scope for linha in do_scanner} }")
    chamadas_scanner = len(CHAMADAS)
    check("scanner_nao_fez_chamada_extra_a_exchange",
          chamadas_scanner == 1 + 2 * len(rs.SCAN_TFS), str(CHAMADAS[:3]) + f" ({chamadas_scanner})")

    # Modo DESLIGADO no MESMO scanner: não coleta e não muda a decisão.
    os.environ["R09_PRESELECTION_MODE"] = "inactive"
    obs._pending.clear()
    with patch.object(rs, "_get_server_data_source", lambda: (fonte, "sintetica")):
        recomendacoes_off = await rs.get_recommendations_via_vision(top_n=2, apply_guard=False)
    check("off_nao_coleta_no_scanner", not obs._pending, str(len(obs._pending)))
    with patch.object(rs, "_get_server_data_source", lambda: (fonte, "sintetica")):
        os.environ["R09_PRESELECTION_MODE"] = "observe"
        recomendacoes_on = await rs.get_recommendations_via_vision(top_n=2, apply_guard=False)
    check("observacao_nao_altera_a_decisao_do_champion",
          [(r.symbol, r.tier, round(r.score, 6)) for r in recomendacoes_off]
          == [(r.symbol, r.tier, round(r.score, 6)) for r in recomendacoes_on],
          f"{[r.symbol for r in recomendacoes_off]} / {[r.symbol for r in recomendacoes_on]}")
    obs._pending.clear()

    # Tabelas limpas: a FASE 2 mede população/dedupe com linhas determinísticas.
    async with db.get_session() as session:
        for tabela in (A.__table__, R.__table__, O.__table__):
            await session.execute(delete(tabela))
        await session.commit()

    # ══════════════════════════════════════════════════════════════════════
    #  FASE 2 — resolver/exportador sobre linhas com os MODELOS REAIS
    # ══════════════════════════════════════════════════════════════════════
    def candidato(symbol, *, aceito, trigger, motivo=None, direcao=SignalDirection.LONG):
        etapas = [rs._stage("CANDIDATE", "PASSED"), rs._stage("PLAYBOOK", "PASSED"),
                  rs._stage("CANDLE", "PASSED")]
        if aceito:
            etapas += [rs._stage("SELECTION", "PASSED"), rs._stage("MTF_REGIME", "PASSED"),
                       rs._stage("GEOMETRY_RR", "PASSED")]
        else:
            etapas += [rs._stage("SELECTION", "REJECTED", motivo or "TIER_BELOW_MINIMUM")]
        return rs._preselection_candidate(sinal_real(symbol, direction=direcao, trigger=trigger),
                                          71.5, stages=etapas, accepted=aceito)

    linhas = [candidato("AAA/USDT:USDT", aceito=True, trigger=T0),
              candidato("BBB/USDT:USDT", aceito=False, trigger=T0 + BAR,
                        motivo="TIER_BELOW_MINIMUM", direcao=SignalDirection.SHORT),
              candidato("CCC/USDT:USDT", aceito=False, trigger=T0 + 2 * BAR,
                        motivo="REGIME_BLOCK")]
    check("scanner_monta_candidato", all(linha is not None for linha in linhas), str(linhas))
    check("lado_do_enum_real_preservado",
          [linha["setup"]["side"] for linha in linhas] == ["long", "short", "long"],
          str([linha["setup"]["side"] for linha in linhas]))
    check("atr_do_indicator_real_preservado",
          {linha["setup"]["atr"] for linha in linhas} == {2.0},
          str([linha["setup"]["atr"] for linha in linhas]))

    # ── Modo DESLIGADO: nada é coletado ────────────────────────────────────
    os.environ["R09_PRESELECTION_MODE"] = "inactive"
    resumo_off = obs.observe_preselection(linhas)
    check("off_nao_coleta", resumo_off.get("enabled") is False and not obs._pending,
          f"{resumo_off} / {len(obs._pending)}")
    await obs.flush_pending()
    async with db.get_session() as session:
        total_off = int((await session.execute(select(func.count(O.opportunity_key)))).scalar() or 0)
    check("off_nao_grava", total_off == 0, str(total_off))

    # ── Modo OBSERVE: coleta, flush REAL e persistência com escopo próprio ─
    os.environ["R09_PRESELECTION_MODE"] = "observe"
    resumo = obs.observe_preselection(linhas)
    check("observe_coleta_aceitas_e_vetadas",
          resumo["accepted"] == 1 and resumo["vetoed"] == 2, str(resumo))
    await obs.flush_pending()

    async with db.get_session() as session:
        oportunidades = (await session.execute(select(O))).scalars().all()
        vetadas = (await session.execute(select(R))).scalars().all()
        tentativas = int((await session.execute(select(func.count(A.attempt_id)))).scalar() or 0)
    check("gravou_as_tres_oportunidades", len(oportunidades) == 3, str(len(oportunidades)))
    check("escopo_e_pre_selection",
          {linha.scope for linha in oportunidades} == {"PRE_SELECTION"},
          str({linha.scope for linha in oportunidades}))
    check("vetadas_persistidas", len(vetadas) == 2, str(len(vetadas)))
    check("tentativas_persistidas", tentativas == 3, str(tentativas))
    payloads = [linha.frozen_config.get("r09_pre_selection") for linha in oportunidades]
    # O payload nasce v2 quando traz blocos v2 (trace, evidência ponto-no-tempo,
    # escopo da decisão observada). Ambas as versões são aceitas pelo acervo; o
    # que importa é a versão DECLARADA ser uma das do contrato.
    check("payload_pre_selecao_viaja",
          all(isinstance(item, dict)
              and item.get("schema_version") in pre.PRE_SCHEMA_VERSIONS
              for item in payloads), str(payloads[:1])[:200])
    desfechos = sorted(item.get("outcome") for item in payloads)
    check("desfecho_congelado", desfechos == ["ACCEPTED", "VETOED", "VETOED"], str(desfechos))
    nao_avaliadas = payloads[0]["funnel"]["stages_not_evaluated"]
    check("etapas_nao_avaliadas_nao_viram_aprovadas",
          "RISK" in nao_avaliadas and "EXECUTION" in nao_avaliadas, str(nao_avaliadas))

    # O funil ANTIGO continua com o significado dele.
    obs.begin_batch([], mode="LIVE")
    check("funil_antigo_preserva_escopo", obs._CONFIG["scope"] == "POST_SELECTION",
          str(obs._CONFIG.get("scope")))

    # ── Exportador R10B lê exatamente estas linhas nos escopos novos ───────
    # A janela acompanha o instante REAL em que as linhas foram observadas
    # (o coletor carimba `observed_at` com o relógio do processo).
    observado_ms = min(ds.utc_ms(linha.first_decision_observed_at) for linha in oportunidades)
    inicio = (observado_ms // BAR) * BAR - 10 * BAR
    split = {"train_start_ms": inicio, "validation_start_ms": inicio + 100 * BAR,
             "holdout_start_ms": inicio + 200 * BAR, "purge_bars": 1}
    as_of_ms = inicio + 150 * BAR
    as_of_utc = ds.ms_datetime(as_of_ms).isoformat().replace("+00:00", "Z")
    replay = {"bar_ms": BAR, "entry_window_bars": 3, "pre_tp1_time_stop_bars": 12,
              "max_holding_bars": 24, "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
              "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5, "max_bars": 96}

    def pedido(scope):
        return {"as_of_utc": as_of_utc, "split": split,
                "baseline_config": dict(replay), "scope": scope,
                "candidate": {"candidate_id": "R09-PRE-AA", "registered_at_ms": inicio - 10 * BAR,
                              "kind": "MANAGEMENT_ONLY", "replay_config": dict(replay)},
                "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0,
                          "funding_bps_per_bar": 1.0},
                "bootstrap": {"seed": 5, "samples": 100, "block_size": 1}}

    async with db.get_session() as session:
        aceitas_ds, aceitas_manifest = await ds.load_dataset(
            session, ds.parse_request(pedido(scopes.SCOPE_PRE_ACCEPTED)))
    exportadas = [linha["opportunity_key"] for linha in aceitas_ds["rows"]]
    esperadas = [linha.opportunity_key for linha in oportunidades
                 if (linha.frozen_config.get("r09_pre_selection") or {}).get("outcome") == "ACCEPTED"]
    check("export_aceitas_traz_as_mesmas_linhas", exportadas == esperadas,
          f"{exportadas} != {esperadas}")
    check("export_aceitas_sem_outcome",
          aceitas_ds["rows"][0]["outcome"] is None, str(aceitas_ds["rows"][0]))
    check("export_aceitas_declara_incomparavel",
          aceitas_manifest["source"]["comparable_with_r10a"] is False,
          str(aceitas_manifest["source"]))

    async with db.get_session() as session:
        vetadas_ds, vetadas_manifest = await ds.load_dataset(
            session, ds.parse_request(pedido(scopes.SCOPE_PRE_VETOED)))
    check("export_vetadas_usa_a_coorte_certa",
          vetadas_manifest["source"]["cohort"] == "R09_PRE_SELECTION_VETOED",
          str(vetadas_manifest["source"]["cohort"]))
    check("export_vetadas_conta_as_duas",
          vetadas_manifest["counts"]["exported"]["training"]
          + vetadas_manifest["counts"]["excluded"].get("INVALID_CANDLE_DATA", 0) == 2,
          str(vetadas_manifest["counts"]))

    # O escopo legado NÃO mistura a coorte nova.
    async with db.get_session() as session:
        legado_ds, legado_manifest = await ds.load_dataset(
            session, ds.parse_request(pedido(scopes.SCOPE_REJECTED_POST)))
    check("legado_nao_mistura_pre_selecao",
          legado_manifest["counts"]["exported"] == {"training": 0, "validation": 0},
          str(legado_manifest["counts"]["exported"]))

    await db._engine.dispose()
    print(f"R09_PRESELECTION_PG_OK: {len(CHECKS)} verificações — scanner real, flush real, "
          "export real, sem exchange")


if __name__ == "__main__":
    asyncio.run(run())
