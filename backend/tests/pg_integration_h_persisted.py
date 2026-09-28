"""H — o entrypoint em modo PERSISTIDO: coleta → banco → export → replay.

`H_PERSIST_TEST_SOCKET` aponta para /tmp/cw-hpersist-sock.* criado pelo runner.
Roda `scripts/research_pipeline.py --persist` como SUBPROCESSO, com o coletor
R09 LIGADO, e confere que as linhas observadas voltam do banco pelo exportador
R10B REAL e alimentam a entrada do replay — não um flush seguido de amnésia.
Sem TCP/DNS, sem exchange, sem produção.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

test_socket = os.environ.get("H_PERSIST_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-hpersist-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://hpersist@/hpersistdb?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste H-persistido")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste H-persistido")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste H-persistido")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def rodar(*, coletor: str, t0: int, symbols: int = 6, seed: int = 7) -> tuple:
    ambiente = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(BACKEND), "DATABASE_URL": DB_URL,
                "R09_PRESELECTION_MODE": coletor}
    proc = subprocess.run(
        [sys.executable, "-B", str(BACKEND / "scripts" / "research_pipeline.py"),
         "--symbols", str(symbols), "--seed", str(seed), "--t0", str(t0), "--persist"],
        capture_output=True, text=True, cwd=str(BACKEND), env=ambiente)
    if proc.returncode != 0:
        raise AssertionError(f"pipeline falhou: {proc.stderr[-800:]}")
    saida = proc.stdout
    return json.loads(saida[saida.index("{"):]), proc


async def run():
    import db
    from sqlalchemy import func, select, text
    from models.decision_observation import (DecisionObservation as O,
                                             DecisionObservationAttempt as A,
                                             RejectedSetupObservation as R)

    T0 = 1_760_000_400_000

    # ── 1. Coletor LIGADO: observa, grava e o export devolve as MESMAS linhas ─
    relatorio, proc = rodar(coletor="observe", t0=T0)
    check("coleta_ligada_observou",
          relatorio["observation"]["enabled"] is True
          and relatorio["observation"]["accepted"] > 0, str(relatorio["observation"]))
    check("persistiu_no_banco", relatorio["persistence"]["state"] == "FLUSHED",
          str(relatorio["persistence"]))
    export = relatorio["persistence"]["export"]
    check("export_real_leu_o_banco",
          export["state"] == "EXPORTED" and export["rows"] > 0, str(export))
    check("export_declara_a_coorte_certa",
          export["scope"] == "R09_PRE_SELECTION_ACCEPTED", str(export))
    check("replay_usa_a_entrada_exportada",
          relatorio["replay_input"]["source"] == "R10B_EXPORT"
          and relatorio["replay_input"]["rows"] == export["rows"],
          str(relatorio["replay_input"]))
    check("replay_admitiu_a_partir_do_banco", relatorio["replay"]["admitted"] > 0,
          str(relatorio["replay"]["admitted"]))

    async with db.get_session() as session:
        gravadas = int((await session.execute(
            select(func.count(O.opportunity_key)))).scalar() or 0)
        escopos = {linha for (linha,) in (await session.execute(select(O.scope))).all()}
    check("linhas_gravadas_no_escopo_novo",
          gravadas >= export["rows"] and escopos == {"PRE_SELECTION"},
          f"{gravadas} / {escopos}")

    # ── 2. A simulação R11 rodou sobre ESSES resultados ────────────────────
    simulacao = relatorio["policy_simulation"]
    check("simulacao_executou_com_persistencia",
          simulacao["state"] == "EXECUTED" and simulacao["persisted"] is True,
          str(simulacao))
    check("simulacao_nao_toca_live", simulacao["applies_live"] is False, str(simulacao))

    # ── 2b. Cronologia D/E no caminho REAL e persistido ───────────────────
    for trade in relatorio["replay"]["capital_timeline"]:
        check(f"disponibilidade_depois_da_saida_{trade['opportunity_id'][:6]}",
              trade["result_available_ts_ms"] > trade["exit_ts_ms"], str(trade))
    wf_report = relatorio["walk_forward"]
    check("estudo_declara_etapas_executadas",
          wf_report["stages"]["candidate_selection"] == "EXECUTED_ON_TRAIN"
          and "train" not in set(wf_report["stages"].values()), str(wf_report["stages"]))
    check("estudo_declara_labels_retidos",
          isinstance(wf_report["train_labels_withheld"], int), str(wf_report))

    # ── 2c. F: escopo da comparação e hipótese autorizada ─────────────────
    comparacao = relatorio["comparison"]
    check("comparacao_declara_escopo_de_gestao",
          comparacao["scope"] == "MANAGEMENT_ONLY"
          and "Score V3" in comparacao["does_not_prove"], str(comparacao["scope"]))
    check("hipotese_autorizada_declarada_bloqueada",
          comparacao["authorized_hypothesis"]["state"] == "BLOCKED_MISSING_DECISION",
          str(comparacao["authorized_hypothesis"]["reason_code"]))
    check("identidade_registrada_antes_do_resultado",
          comparacao["identity_registered_before_results"] is True
          and comparacao["bundle_hash"], str(comparacao)[:160])

    # ── 3. Capital liquidado na saída também no caminho persistido ─────────
    linha_do_tempo = relatorio["replay"]["capital_timeline"]
    check("capital_de_entrada_declarado",
          all(item["capital_at_entry_usd"] is not None for item in linha_do_tempo),
          str(linha_do_tempo[:1]))
    check("capital_de_saida_declarado",
          all(item["capital_after_usd"] is not None for item in linha_do_tempo),
          str(linha_do_tempo[:1]))
    metricas = relatorio["replay"]["metrics"]
    check("caixa_concilia",
          abs((metricas["capital_start_usd"] + metricas["realized_pnl_usd"])
              - metricas["capital_end_usd"]) < 1e-9, str(metricas))

    # ── 4. Coletor DESLIGADO: nada novo é observado e o export fica vazio ──
    async with db.get_session() as session:
        antes = int((await session.execute(select(func.count(O.opportunity_key)))).scalar() or 0)
    desligado, _ = rodar(coletor="inactive", t0=T0, symbols=4)
    check("coleta_desligada_nao_observa",
          desligado["observation"]["enabled"] is False
          and desligado["observation"]["accepted"] == 0, str(desligado["observation"]))
    check("sem_coleta_o_acervo_nao_cresce",
          desligado["persistence"]["export"]["rows"] == export["rows"],
          f"{desligado['persistence']['export']} vs {export}")
    check("entrada_continua_vindo_do_acervo",
          desligado["replay_input"]["source"] == "R10B_EXPORT"
          and desligado["replay_input"]["rows"] == export["rows"],
          str(desligado["replay_input"]))
    async with db.get_session() as session:
        depois = int((await session.execute(select(func.count(O.opportunity_key)))).scalar() or 0)
    check("coleta_desligada_nao_grava", depois == antes, f"{antes} → {depois}")

    # ── 5. Nada de execução: sem ordem, sem promoção, sem LIVE ─────────────
    check("entrypoint_nao_promove",
          relatorio["promotable"] is False and relatorio["live_equivalent"] is False
          and relatorio["gate"]["live_approval"] == "UNAVAILABLE", str(relatorio["gate"]))
    check("adaptador_operacional_ainda_nao_existe",
          relatorio["live_adapter"] == "LIVE_ADAPTER_NOT_IMPLEMENTED")
    blob = (proc.stdout + proc.stderr).lower()
    for termo in ("place_order", "kill-switch", "telegram", "binance.com"):
        check(f"sem_{termo.replace('.', '_').replace('-', '_')}", termo not in blob)

    # ── 6. Isolamento: risco/universo/learned cache continuam vazios ───────
    async with db.get_session() as session:
        intocadas = {}
        for tabela in ("real_trades", "entry_intents", "execution_incidents",
                       "risk_state", "symbol_learned_params", "rotation_universe_state"):
            intocadas[tabela] = int((await session.execute(text(
                f"SELECT count(*) FROM {tabela}"))).scalar() or 0)
    check("nada_operacional_foi_escrito", all(v == 0 for v in intocadas.values()),
          str({k: v for k, v in intocadas.items() if v}))

    await db._engine.dispose()
    print(f"H_PERSISTED_PG_OK: {len(CHECKS)} verificações — entrypoint real, "
          "export real alimentando o replay, sem exchange")


if __name__ == "__main__":
    asyncio.run(run())
