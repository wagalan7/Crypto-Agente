"""R09 pré-seleção — do scanner ao export, pelo caminho REAL.

`R09_PRE_TEST_SOCKET` aponta para /tmp/cw-r09pre-sock.* criado pelo runner.
Usa as funções do scanner de produção (`_preselection_candidate`/`_stage`), o
coletor (`observe_preselection`), o flush REAL do R09 e o exportador R10B nos
escopos novos. Sem TCP/DNS, sem exchange, sem dado de produção.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys
import types

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


def sinal(symbol: str, *, direction="long", trigger=T0, entry=100.0, stop=95.0):
    """Objeto com o MESMO contrato de atributos que o scanner entrega."""
    return types.SimpleNamespace(
        symbol=symbol, timeframe="15m", direction=direction, entry=entry,
        stop_loss=stop, tp1=entry + 5.0, tp2=entry + 10.0,
        indicators={"atr": 2.0},
        data_freshness={"candle": {"close_time_ms": trigger, "source": "binance"}})


async def run():
    from sqlalchemy import func, select
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

    def candidato(symbol, *, aceito, trigger, motivo=None):
        etapas = [rs._stage("CANDIDATE", "PASSED"), rs._stage("PLAYBOOK", "PASSED"),
                  rs._stage("CANDLE", "PASSED")]
        if aceito:
            etapas += [rs._stage("SELECTION", "PASSED"), rs._stage("MTF_REGIME", "PASSED"),
                       rs._stage("GEOMETRY_RR", "PASSED")]
        else:
            etapas += [rs._stage("SELECTION", "REJECTED", motivo or "TIER_BELOW_MINIMUM")]
        return rs._preselection_candidate(sinal(symbol, trigger=trigger), 71.5,
                                          stages=etapas, accepted=aceito)

    linhas = [candidato("AAA/USDT:USDT", aceito=True, trigger=T0),
              candidato("BBB/USDT:USDT", aceito=False, trigger=T0 + BAR,
                        motivo="TIER_BELOW_MINIMUM"),
              candidato("CCC/USDT:USDT", aceito=False, trigger=T0 + 2 * BAR,
                        motivo="REGIME_BLOCK")]
    check("scanner_monta_candidato", all(linha is not None for linha in linhas), str(linhas))

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
    check("payload_pre_selecao_viaja",
          all(isinstance(item, dict) and item.get("schema_version") == pre.PRE_SCHEMA_VERSION
              for item in payloads), str(payloads[:1]))
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
