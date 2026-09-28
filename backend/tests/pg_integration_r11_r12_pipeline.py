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

    # ── 7b. G: cálculo feito sobre geração ANTIGA, em período DIFERENTE ────
    # A e B leem a geração N com progresso P. A publica período X (geração N+1,
    # progresso P+1). B, que calculou a partir de N, tenta período Y: precisa
    # ser RECUSADO (ou recalcular), nunca publicar N+2 com o progresso velho.
    leitura_ab = await ps.read_state(db.get_session, **identidade)
    geracao_lida = int(leitura_ab["state"]["generation"])
    progresso_lido = {"symbol": "PORTFOLIO", "action": "HOLD", "periods": 1,
                      "last_period": "P-LIDO", "last_evidence": "ev-lido",
                      "universe_source": identidade["universe_version"]}
    publicou_a = await ps.publish_generation(
        db.get_session, period_key="P-A", evidence_key="ev-a", now_ms=T0 + 10 * DIA,
        payload={"hysteresis": {**progresso_lido, "periods": 2, "last_period": "P-A",
                                "last_evidence": "ev-a"}},
        expected_generation=geracao_lida, **identidade)
    check("a_publica_a_partir_da_geracao_lida",
          publicou_a["published"] and publicou_a["generation"] == geracao_lida + 1,
          str(publicou_a))
    publicou_b = await ps.publish_generation(
        db.get_session, period_key="P-B", evidence_key="ev-b", now_ms=T0 + 11 * DIA,
        payload={"hysteresis": {**progresso_lido, "periods": 2, "last_period": "P-B",
                                "last_evidence": "ev-b"}},
        expected_generation=geracao_lida, **identidade)
    check("b_com_calculo_obsoleto_e_recusado",
          publicou_b["published"] is False
          and publicou_b["reason_code"] == "GENERATION_STALE",
          str(publicou_b))
    depois_de_b = await ps.read_state(db.get_session, **identidade)
    check("avanco_de_a_nao_foi_perdido",
          int(depois_de_b["state"]["generation"]) == geracao_lida + 1
          and depois_de_b["state"]["payload"]["hysteresis"]["last_period"] == "P-A",
          str(depois_de_b["state"]["generation"]))
    # Recalculando a partir da geração NOVA, B publica progresso 3 (não 2).
    atual_b = int(depois_de_b["state"]["generation"])
    recalculado = await ps.publish_generation(
        db.get_session, period_key="P-B", evidence_key="ev-b", now_ms=T0 + 11 * DIA,
        payload={"hysteresis": {**progresso_lido, "periods": 3, "last_period": "P-B",
                                "last_evidence": "ev-b"}},
        expected_generation=atual_b, **identidade)
    check("b_recalculado_publica_progresso_seguinte",
          recalculado["published"] and recalculado["generation"] == atual_b + 1,
          str(recalculado))
    final_g = await ps.read_state(db.get_session, **identidade)
    check("progresso_nao_regrediu",
          final_g["state"]["payload"]["hysteresis"]["periods"] == 3,
          str(final_g["state"]["payload"]["hysteresis"]))

    # ── 7c. Duas conexões concorrentes com períodos diferentes ────────────
    partida = int(final_g["state"]["generation"])
    concorrentes = await asyncio.gather(
        ps.publish_generation(db.get_session, period_key="P-X", evidence_key="ev-x",
                              now_ms=T0 + 12 * DIA,
                              payload={"hysteresis": {**progresso_lido, "periods": 4,
                                                      "last_period": "P-X",
                                                      "last_evidence": "ev-x"}},
                              expected_generation=partida, **identidade),
        ps.publish_generation(db.get_session, period_key="P-Y", evidence_key="ev-y",
                              now_ms=T0 + 12 * DIA,
                              payload={"hysteresis": {**progresso_lido, "periods": 4,
                                                      "last_period": "P-Y",
                                                      "last_evidence": "ev-y"}},
                              expected_generation=partida, **identidade))
    publicadas_xy = [item for item in concorrentes if item["published"]]
    check("periodos_diferentes_da_mesma_geracao_so_um_publica",
          len(publicadas_xy) == 1, str(concorrentes))
    check("o_outro_declara_geracao_obsoleta",
          any(item.get("reason_code") == "GENERATION_STALE" for item in concorrentes),
          str(concorrentes))
    apos_xy = await ps.read_state(db.get_session, **identidade)
    check("geracao_avancou_uma_vez_so",
          int(apos_xy["state"]["generation"]) == partida + 1, str(apos_xy["state"]["generation"]))

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

    # ── 9b. H: caminho PERMITIDO do tipo pré-seleção no CATÁLOGO OFICIAL ──
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select as sql_select
    async with db._engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all, tables=[E.__table__])
    os.environ["P05_ANALYTICS_ENABLED"] = "true"
    os.environ["P05_CHALLENGER_SHADOW_ENABLED"] = "true"
    import importlib
    importlib.reload(evid)

    # Evidência no CONTRATO do gate R12 (nada de número solto: são os campos
    # que `gate_evidence_from_study` produz a partir de replay/estudo reais).
    evidencia_ok = {
        "total_shadow_trades": 400,
        "trades_per_playbook": {"TREND_PULLBACK": 200, "TREND_BREAKOUT": 200},
        "enabled_playbooks": ["TREND_PULLBACK", "TREND_BREAKOUT"],
        "calendar_days": 45.0, "business_days": 32,
        "coverage_pct": 98.0, "net_ev_r": 0.25, "uncertainty_r": 0.02,
        "drawdown_r": 3.0, "stability_ratio": 0.8,
        "operational_failures": 0, "economic_duplicates": 0,
        "unresolved_protection_failures": 0, "essential_gaps": [],
        "fidelity_discrepancy_pct": 0.0,
    }
    corte = datetime(2026, 9, 20, tzinfo=timezone.utc)
    criacao = await evid.create_preselection_experiment(
        champion=champion, config=pre_config, objective=evid.OBJECTIVE_MORE_OPERATIONS,
        evidence=evidencia_ok, cutoff=corte, fingerprint="fp-pre-1")
    check("criacao_oficial_do_tipo_pre_selecao",
          criacao["ok"] and criacao["experiment_type"] == evid.PRE_SELECTION_TYPE,
          str(criacao)[:220])
    check("criacao_nao_promove",
          criacao["promotable"] is False and criacao["live_approval"] == "UNAVAILABLE",
          str(criacao)[:200])
    check("avaliacao_usa_a_populacao_certa",
          criacao["offline"]["population"] == "PRE_SELECTION"
          and criacao["offline"]["outcomes_read"] is False, str(criacao["offline"])[:220])

    async with db.get_session() as session:
        linha_exp = (await session.execute(
            sql_select(E).where(E.experiment_key == criacao["experiment_key"]))).scalar_one()
        exp_id, status_inicial = linha_exp.id, linha_exp.status
        config_congelada = dict(linha_exp.candidate_config)
    check("linha_gravada_no_catalogo_oficial", exp_id > 0 and status_inicial in
          (evid.STATUS_OFFLINE_VALIDATED, evid.STATUS_DRAFT), str(status_inicial))
    check("tipo_viaja_na_config_congelada",
          config_congelada.get("experiment_type") == r12.TYPE_PRE_SELECTION,
          str(config_congelada))

    # Transições: só as permitidas para ESTE tipo; ELIGIBLE fica fechado.
    check("transicao_para_shadow_permitida",
          evid.can_transition_for(config_congelada, evid.STATUS_OFFLINE_VALIDATED,
                                  evid.STATUS_SHADOW))
    check("transicao_para_elegivel_bloqueada",
          not evid.can_transition_for(config_congelada, evid.STATUS_SHADOW,
                                      evid.STATUS_ELIGIBLE))
    sombra = await evid.start_preselection_shadow(exp_id)
    check("shadow_do_tipo_pre_selecao_avanca",
          sombra["ok"] and sombra["status"] == evid.STATUS_SHADOW
          and sombra["simulation_only"] is True, str(sombra)[:200])
    repetida_sombra = await evid.start_preselection_shadow(exp_id)
    check("shadow_e_idempotente", repetida_sombra.get("idempotent") is True,
          str(repetida_sombra)[:160])

    # Recarrega após "restart" (pool derrubado) e continua no mesmo estado.
    await db._engine.dispose()
    async with db.get_session() as session:
        depois_restart = (await session.execute(
            sql_select(E.status, E.shadow_metrics).where(E.id == exp_id))).one()
    check("estado_sobrevive_restart",
          depois_restart[0] == evid.STATUS_SHADOW
          and depois_restart[1]["population"] == "PRE_SELECTION", str(depois_restart))

    # Concorrência indevida: um segundo challenger do MESMO tipo é recusado.
    outra_config = r12.tag_config({"SCORE_MIN": 74})
    criacao2 = await evid.create_preselection_experiment(
        champion=champion, config=outra_config, objective=evid.OBJECTIVE_MORE_OPERATIONS,
        evidence=evidencia_ok, cutoff=corte, fingerprint="fp-pre-2")
    async with db.get_session() as session:
        exp2 = (await session.execute(
            sql_select(E.id).where(E.experiment_key == criacao2["experiment_key"]))).scalar_one()
    segunda_sombra = await evid.start_preselection_shadow(exp2)
    check("exclusividade_do_ciclo_preservada",
          segunda_sombra["ok"] is False
          and segunda_sombra["reason_code"] == "CHALLENGER_ALREADY_ACTIVE",
          str(segunda_sombra)[:200])

    # Config congelada não muda: mesma chave com conteúdo diferente é recusada.
    try:
        await evid.create_preselection_experiment(
            champion=champion, config=pre_config, objective=evid.OBJECTIVE_MORE_OPERATIONS,
            evidence=evidencia_ok, cutoff=corte, fingerprint="fp-OUTRO")
        alterou = True
    except RuntimeError as exc:
        alterou = "FINGERPRINT" not in str(exc)
    check("config_congelada_nao_e_alterada", alterou is False, "fingerprint aceito")

    # O caminho pós-seleção continua fechado para este tipo — e agora aponta o certo.
    errado = await evid.start_shadow(exp_id)
    check("pos_selecao_continua_recusando_o_tipo",
          errado["ok"] is False and errado["reason_code"] == r12.TYPE_MISMATCH
          and errado["dispatch_to"] == "start_preselection_shadow", str(errado)[:220])

    # SHADOW pré-seleção NÃO anota snapshot pós-seleção (populações separadas).
    async with db.get_session() as session:
        contexto = await evid.get_active_shadow_context(session)
    check("shadow_pre_selecao_nao_anota_pos_selecao", contexto is None, str(contexto))

    # Promoção declarada como NÃO IMPLEMENTADA (código por terminar).
    promocao = await evid.promote_preselection(exp_id)
    check("promocao_do_tipo_declarada_nao_implementada",
          promocao["ok"] is False
          and promocao["reason_code"] == evid.PRE_SELECTION_PROMOTION_BLOCK
          and promocao["live_approval"] == "UNAVAILABLE", str(promocao)[:200])

    # Evidência insuficiente NÃO valida o candidato.
    fraca = await evid.create_preselection_experiment(
        champion=champion, config=r12.tag_config({"SCORE_MIN": 75}), objective=evid.OBJECTIVE_MORE_OPERATIONS,
        evidence={"sample": {"resolved": 1}, "source": {"derived_from_computed_results": True}},
        cutoff=corte, fingerprint="fp-pre-3")
    check("evidencia_fraca_nao_valida",
          fraca["offline"]["verdict"] in (evid.STATUS_INSUFFICIENT, evid.STATUS_REJECTED),
          str(fraca["offline"])[:200])

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
