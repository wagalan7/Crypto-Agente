"""R11/R12 — estado CONSUMIDO pela simulação real e populações isoladas.

`R11PIPE_TEST_SOCKET` aponta para /tmp/cw-r11pipe-sock.* criado pelo runner.
A simulação é o ENTRYPOINT de pesquisa (`scripts/research_pipeline.py --persist`),
executado como SUBPROCESSO — cada execução é um processo novo, então "retomar
após restart" é retomada de verdade, não variável de módulo sobrevivendo. O
despacho por tipo é exercitado pelo serviço oficial (`strategy_evidence_service`).
Sem TCP/DNS, sem exchange, sem produção.

Defeito reproduzido: `load_state`/`publish_generation`/`advance_hysteresis` só
eram chamados por testes — persistir payload arbitrário não prova retomada — e
o comparador legado ignorava o tipo versionado, aceitando um candidato
PRE_SELECTION porque as chaves eram as mesmas.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

test_socket = os.environ.get("R11PIPE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r11pipe-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://r11pipe@/r11pipedb?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R11/R12")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R11/R12")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R11/R12")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def rodar_pipeline(*, t0: int, symbols: int = 6, seed: int = 7) -> dict:
    """Executa o entrypoint REAL num processo NOVO (restart de verdade)."""
    proc = subprocess.run(
        [sys.executable, "-B", str(BACKEND / "scripts" / "research_pipeline.py"),
         "--symbols", str(symbols), "--seed", str(seed), "--t0", str(t0), "--persist"],
        capture_output=True, text=True, cwd=str(BACKEND),
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPATH": str(BACKEND), "DATABASE_URL": DB_URL})
    if proc.returncode != 0:
        raise AssertionError(f"pipeline falhou: {proc.stderr[-800:]}")
    saida = proc.stdout
    return json.loads(saida[saida.index("{"):])


async def run():
    import db
    from sqlalchemy import func, select, text, update
    from models.policy_simulation_state import PolicySimulationState
    from services import policy_state_service as ps
    from services import preselection_experiment_service as r12
    from services import robust_policy_service as rp
    from services import strategy_evidence_service as evid

    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[PolicySimulationState.__table__])

    T0 = 1_760_000_400_000
    DIA = 86_400_000

    # ── 1. Primeira simulação: estado ausente, histerese começa ────────────
    primeira = rodar_pipeline(t0=T0)["policy_simulation"]
    check("simulacao_executa_pelo_entrypoint", primeira["state"] == "EXECUTED", str(primeira))
    check("primeira_nao_retoma_estado", primeira["resumed_from_state"] is False, str(primeira))
    check("primeira_publica_geracao_um",
          primeira["published"] and primeira["generation"] == 1, str(primeira))
    check("histerese_comeca_em_um_periodo", primeira["periods"] == 1, str(primeira))
    check("ainda_nao_esta_pronta", primeira["ready"] is False, str(primeira))

    # ── 2. Repetição idêntica é idempotente: nem geração, nem período ──────
    repetida = rodar_pipeline(t0=T0)["policy_simulation"]
    check("repeticao_retoma_o_estado", repetida["resumed_from_state"] is True, str(repetida))
    check("repeticao_nao_publica_de_novo",
          repetida["published"] is False
          and repetida["publication_reason_code"] == "PERIOD_UNCHANGED", str(repetida))
    check("repeticao_nao_avanca_periodo",
          repetida["periods"] == 1 and repetida["hysteresis_reason_code"] == "SAME_PERIOD",
          str(repetida))
    async with db.get_session() as session:
        linhas = int((await session.execute(
            select(func.count(PolicySimulationState.id)))).scalar() or 0)
    check("uma_linha_por_identidade", linhas == 1, str(linhas))

    # ── 3. Restart com período NOVO e evidência NOVA: a histerese retoma ───
    # Processo novo, período seguinte: os períodos continuam de onde pararam
    # (2, não 1) — é isso que prova retomada, não payload arbitrário.
    # Semente diferente = outra realização de mercado no MESMO universo: é o
    # que produz evidência nova (tempo passando, sozinho, não produz).
    segunda = rodar_pipeline(t0=T0 + 3 * DIA, seed=11)["policy_simulation"]
    check("restart_retoma_a_histerese", segunda["resumed_from_state"] is True, str(segunda))
    check("periodo_novo_avanca",
          segunda["periods"] == 2
          and segunda["hysteresis_reason_code"] == "PERIOD_WITH_NEW_EVIDENCE",
          str(segunda))
    check("publica_geracao_dois",
          segunda["published"] and segunda["generation"] == 2, str(segunda))
    check("com_periodos_suficientes_fica_pronta",
          segunda["ready"] is True and segunda["required_periods"] == 2, str(segunda))
    check("prontidao_nao_toca_live", segunda["applies_live"] is False, str(segunda))

    # ── 4. Resultado idêntico noutro período NÃO inventa evidência nova ────
    estado_antes = await ps.read_state(
        db.get_session, experiment_key=segunda["identity"]["experiment_key"],
        universe_version=segunda["identity"]["universe_version"],
        population=rp.POPULATION_SHADOW)
    check("estado_lido_com_identidade_conferida",
          estado_antes["available"] and estado_antes["found"]
          and estado_antes["state"]["policy_version"] == rp.POLICY_VERSION,
          str(estado_antes.get("reason_code")))
    payload = estado_antes["state"]["payload"]
    check("payload_carrega_a_histerese",
          payload["hysteresis"]["periods"] == 2
          and payload["hysteresis"]["universe_source"] == segunda["identity"]["universe_version"],
          str(payload))

    # ── 5. Universo diferente tem estado PRÓPRIO (não herda progresso) ─────
    outro = rodar_pipeline(t0=T0, symbols=5)["policy_simulation"]   # outro universo
    check("universo_diferente_nao_herda",
          outro["resumed_from_state"] is False and outro["generation"] == 1, str(outro))
    check("identidade_declara_universo",
          outro["identity"]["universe_version"] != segunda["identity"]["universe_version"],
          str(outro["identity"]))

    # ── 6. Concorrência: duas conexões, uma geração ────────────────────────
    identidade = {"experiment_key": segunda["identity"]["experiment_key"],
                  "universe_version": segunda["identity"]["universe_version"],
                  "population": rp.POPULATION_SHADOW}
    disputa = await asyncio.gather(
        ps.publish_generation(db.get_session, period_key="P-CONC", evidence_key="ev-conc",
                              now_ms=T0 + 9 * DIA, payload={"hysteresis": {}}, **identidade),
        ps.publish_generation(db.get_session, period_key="P-CONC", evidence_key="ev-conc",
                              now_ms=T0 + 9 * DIA, payload={"hysteresis": {}}, **identidade))
    publicadas = [item for item in disputa if item["published"]]
    check("concorrencia_publica_uma_vez", len(publicadas) == 1, str(disputa))
    check("geracao_final_e_tres", publicadas[0]["generation"] == 3, str(publicadas))

    # ── 7. Geração obsoleta não publica por cima ───────────────────────────
    obsoleta = await ps.publish_generation(
        db.get_session, period_key="P-CONC", evidence_key="ev-antiga",
        now_ms=T0 + 9 * DIA, payload={"hysteresis": {}}, **identidade)
    check("geracao_obsoleta_nao_sobrescreve",
          obsoleta["published"] is False and obsoleta["reason_code"] == "PERIOD_UNCHANGED",
          str(obsoleta))

    # ── 8. Erro de leitura NÃO é primeiro estado ───────────────────────────
    async def sessao_quebrada():
        raise RuntimeError("banco fora do ar")

    class FabricaQuebrada:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("banco fora do ar")

        async def __aexit__(self, *exc):
            return False

    leitura_ruim = await ps.read_state(FabricaQuebrada(), **identidade)
    check("erro_de_leitura_nao_e_primeiro_estado",
          leitura_ruim["available"] is False
          and leitura_ruim["reason_code"] == "STATE_READ_ERROR", str(leitura_ruim))
    check("erro_de_leitura_nao_devolve_estado", leitura_ruim["state"] is None,
          str(leitura_ruim))

    # ── 9. Isolamento: a simulação ESCREVE só a tabela dela ────────────────
    # (o schema inteiro existe porque o entrypoint chama `init_db`; o que
    # importa é que nada operacional recebeu linha.)
    operacionais = ["real_trades", "entry_intents", "execution_incidents", "risk_state",
                    "risk_events", "strategy_experiments", "symbol_learned_params",
                    "rotation_universe_state", "recommendation_snapshots",
                    "calibration_versions", "live_test_state", "backtest_trades"]
    async with db.get_session() as session:
        vazias = {}
        for tabela in operacionais:
            vazias[tabela] = int((await session.execute(text(
                f"SELECT count(*) FROM {tabela}"))).scalar() or 0)
        simulacao = int((await session.execute(text(
            "SELECT count(*) FROM policy_simulation_state"))).scalar() or 0)
    check("nada_operacional_recebeu_linha",
          all(valor == 0 for valor in vazias.values()),
          str({k: v for k, v in vazias.items() if v}))
    check("a_simulacao_tem_estado_proprio", simulacao >= 2, str(simulacao))

    # ══════════════════════════════════════════════════════════════════════
    #  R12 — despacho por TIPO no serviço oficial
    # ══════════════════════════════════════════════════════════════════════
    champion = {"SCORE_MIN": 70}
    pre_config = r12.tag_config({"SCORE_MIN": 73})
    pos_config = {"SCORE_MIN": 73}
    linhas_outcome = [{"realized_r": 1.0, "status": "closed_tp2", "features": {"score": 74},
                       "score": 74, "tier": "A",
                       "created_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
                       "resolved_at": datetime(2026, 9, 20, 4, tzinfo=timezone.utc)}]

    comparacao = evid.compare_configs(linhas_outcome, champion, pre_config, ["score"])
    check("comparador_pos_selecao_recusa_pre_selecao",
          comparacao.get("refused") is True
          and comparacao["reason_code"] == r12.TYPE_MISMATCH, str(comparacao)[:200])
    check("recusa_nao_le_outcome",
          comparacao["champion"] is None and comparacao["candidate"] is None
          and comparacao["evaluable"] == 0, str(comparacao)[:200])

    legado = evid.compare_configs(linhas_outcome, champion, pos_config, ["score"])
    check("comparador_legado_preservado",
          legado.get("refused") is None and isinstance(legado["candidate"], dict),
          str(legado)[:200])

    offline = evid.evaluate_candidate_offline(
        linhas_outcome, champion,
        {"knob": "SCORE_MIN", "config": pre_config, "objective": "EV",
         "champion_value": 70, "candidate_value": 73})
    check("avaliacao_offline_recusa_antes_do_loader",
          offline["verdict"] == evid.STATUS_REJECTED
          and offline["reason_code"] == r12.TYPE_MISMATCH
          and offline["outcomes_read"] is False, str(offline)[:200])

    anotacao = evid.build_experiment_annotation(
        linhas_outcome[0], champion, pre_config, experiment_key="exp-pre",
        candidate_hash="h", active=["score"])
    check("anotacao_recusa_tipo_divergente",
          anotacao.get("refused") is True
          and anotacao["reason_code"] == r12.TYPE_MISMATCH, str(anotacao)[:200])

    # Guarda de tipo no comparador continua declarando a não-intercambiabilidade.
    guarda = r12.comparator_guard(expected_type=r12.TYPE_POST_SELECTION,
                                  candidate_config=pre_config)
    check("guarda_declara_nao_intercambiavel",
          guarda["ok"] is False and guarda["interchangeable"] is False, str(guarda))
    check("guarda_aceita_o_proprio_tipo",
          r12.comparator_guard(expected_type=r12.TYPE_PRE_SELECTION,
                               candidate_config=pre_config)["ok"] is True)

    # ── 10. Caminho PERMITIDO da pré-seleção: runner próprio, sem promoção ─
    relatorio = rodar_pipeline(t0=T0 + 12 * DIA, seed=13)
    check("pre_selecao_tem_caminho_proprio",
          relatorio["gate"]["evidence_from_computed_results"] is True, str(relatorio["gate"]))
    check("pre_selecao_nunca_promove",
          relatorio["gate"]["live_approval"] == "UNAVAILABLE"
          and relatorio["promotable"] is False
          and relatorio["walk_forward"]["promotable"] is False, str(relatorio["gate"]))
    check("bloqueios_antigos_preservados",
          r12.legacy_blocks()["p051_analytics_only"] == r12.P051_BLOCK_REASON
          and r12.legacy_blocks()["new_type_bypasses_legacy_blocks"] is False)

    await db._engine.dispose()
    print(f"R11_R12_PIPELINE_PG_OK: {len(CHECKS)} verificações — simulação real pelo "
          "entrypoint, restart por processo novo, despacho por tipo")


if __name__ == "__main__":
    asyncio.run(run())
