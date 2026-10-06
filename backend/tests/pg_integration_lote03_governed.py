"""Lote 03 — matriz integrada em PostgreSQL DESCARTÁVEL (driver real).

`LOTE03_TEST_SOCKET` aponta para /tmp/cw-lote03-sock.* criado pelo runner.
Socket Unix, TCP/DNS bloqueados e CONTADOS, nenhuma exchange: a borda HTTP
assinada é falsa e cada POST é contado. O opt-in de aprovação TEST_ONLY é
PRIVADO do teste (`_ALLOW_TEST_APPROVALS`), nunca uma ENV — e não representa
aprovação humana de produção.

Provas: paridade OFF/LEGACY pelo caller real; caminho candidato completo
(reserva → proposta → assinatura falsa → proteção oficial) com UM efeito
lógico e contexto persistido; aprovação ausente/revogada/vencida e revogação
durante a espera ⇒ zero POST; duas conexões em aprovação×revogação e dois
promotores; restart e filha `-mfb`; rollback com posição aberta; shadow
sintético não satisfaz o requisito prospectivo; status não refaz trabalho.
"""
import asyncio
import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

test_socket = os.environ.get("LOTE03_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-lote03-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
DB_URL = "postgresql+asyncpg://lote03@/lote03db?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
os.environ["ADMIN_API_TOKEN"] = "token-de-teste-local"
os.environ["P05_ANALYTICS_ENABLED"] = "true"
os.environ["P05_CHALLENGER_SHADOW_ENABLED"] = "true"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket
#: Conexões TCP tentadas (precisam ser ZERO) e resoluções DNS tentadas
#: (bloqueadas e CONTABILIZADAS — fonte declarada no relatório).
TCP_TENTADO: list = []
DNS_TENTADO: list = []


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            TCP_TENTADO.append(address)
            raise AssertionError("TCP proibido no teste do Lote 03")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            TCP_TENTADO.append(address)
            raise AssertionError("TCP proibido no teste do Lote 03")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    import traceback
    quadro = "|".join(f"{q.filename.rsplit('/', 1)[-1]}:{q.lineno}:{q.name}"
                      for q in traceback.extract_stack()[-6:-1])
    DNS_TENTADO.append((args[0] if args else None, quadro))
    raise AssertionError("DNS proibido no teste do Lote 03")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "l03" + "0" * 61

#: Instrumentação LOCAL do harness: guarda o log dos serviços exercitados para
#: que a primeira parada mostre a função/condição real, em vez de um silêncio.
import logging

LOGS: list = []


class _Captura(logging.Handler):
    def emit(self, record):
        try:
            LOGS.append(f"{record.name}:{record.levelname}:{record.getMessage()[:200]}")
        except Exception:  # noqa: BLE001
            pass


_captura = _Captura(level=logging.DEBUG)
for _nome in ("services.shadow_trade_service", "services.entry_intent_service",
              "services.binance_signed_service", "services.live_candidate_adapter_service",
              "services.operational_governance_service"):
    _logger = logging.getLogger(_nome)
    _logger.addHandler(_captura)
    _logger.setLevel(logging.DEBUG)


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        for linha in LOGS[-30:]:
            print(f"    log> {linha}")
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def ms(value=None):
    return int((value or datetime.now(timezone.utc)).timestamp() * 1000)


def clock_ms():
    """Relógio EFETIVO do teste.

    A autoridade TEST_ONLY expira em `now + 1h` do instante do manifesto, então
    o harness congela `time.time` nesse instante. As bordas de mercado falsas
    precisam do MESMO relógio: usar `datetime.now` real faria o preflight ver
    profundidade "no futuro" e recusar a entrada (causa comprovada da segunda
    parada do harness).
    """
    import time as _time
    return int(_time.time() * 1000)


async def run():
    import db
    from sqlalchemy import func, select, text
    from models.decision_observation import (DecisionObservation as O,
                                             DecisionObservationAttempt as A,
                                             RejectedSetupObservation as R)
    from models.entry_intent import EntryIntent
    from models.policy_simulation_state import PolicySimulationState as S
    from models.strategy_experiment import StrategyExperiment as E
    from services import binance_signed_service as bss
    from services import edge_decay_service
    from services import exchange_service
    from services import entry_intent_service as intents
    from services import kill_switch_service
    from services import live_candidate_adapter_service as adapter
    from services import manual_position_service as mps
    from services import operational_governance_service as g
    from services import policy_state_service as ps
    from services import prospective_shadow_service as prospective
    from services import shadow_trade_service as sts
    from services import strategy_evidence_service as se
    from tests.test_lote03_governance import (CHAMPION, approval_payload,
                                              governance_fixture)

    await db.init_db()
    for _ in range(2):              # criação aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[S.__table__, E.__table__, O.__table__,
                                        A.__table__, R.__table__,
                                        EntryIntent.__table__])
    from models.recommendation_snapshot import RecommendationSnapshot

    exp_fixture, report, bundle, now = governance_fixture()
    # População do manifesto é o UNIVERSO (None = escopo inteiro); o símbolo do
    # canário vem dos LIMITES aprovados, que a fixture declara explicitamente.
    SYMBOL = approval_payload(bundle, now)["limits"]["symbols"][0]

    # ── Persistência do experimento e do estudo de calibração ──────────────
    async with db.get_session() as session:
        linha = E(experiment_key=exp_fixture.experiment_key,
                  champion_hash=exp_fixture.champion_hash,
                  candidate_hash=exp_fixture.candidate_hash,
                  status="OFFLINE_VALIDATED", objective="MORE_OPERATIONS",
                  candidate_config=exp_fixture.candidate_config,
                  dataset_fingerprint=exp_fixture.dataset_fingerprint,
                  dataset_cutoff=exp_fixture.dataset_cutoff,
                  offline_metrics={**exp_fixture.offline_metrics,
                                   # Referência do estudo PERSISTIDO (o mesmo
                                   # vínculo que `create_preselection_experiment`
                                   # grava no ciclo oficial).
                                   "study_ref": {
                                       "experiment_key": exp_fixture.experiment_key,
                                       "universe_version": bundle["manifest"]["population"]["universe_version"],
                                       "population": "SHADOW"}},
                  decision={"previous_field": "preserved"})
        session.add(linha)
        await session.flush()
        exp_id = int(linha.id)
        # O relatório de calibração vive no namespace de estado já existente.
        await session.execute(text(
            "INSERT INTO policy_simulation_state (state_key, experiment_key, "
            "policy_version, universe_version, population, generation, payload, "
            "created_at, updated_at) VALUES (:k, :ek, :pv, :uv, :pop, 0, "
            "CAST(:payload AS jsonb), now(), now())"),
            {"k": ps.state_key(experiment_key="r13cal:" + report["study_key"][:32],
                               universe_version=bundle["manifest"]["population"]["universe_version"],
                               population="SHADOW"),
             "ek": "r13cal:" + report["study_key"][:32], "pv": ps.POLICY_VERSION,
             "uv": bundle["manifest"]["population"]["universe_version"],
             "pop": "SHADOW",
             "payload": __import__("json").dumps(report)})
        # Estudo recuperável pelo loader oficial (mesma persistência do ciclo).
        await session.execute(text(
            "INSERT INTO policy_simulation_state (state_key, experiment_key, "
            "policy_version, universe_version, population, generation, payload, "
            "created_at, updated_at) VALUES (:k, :ek, :pv, :uv, :pop, 1, "
            "CAST(:payload AS jsonb), now(), now())"),
            {"k": ps.state_key(experiment_key=exp_fixture.experiment_key,
                               universe_version=bundle["manifest"]["population"]["universe_version"],
                               population="SHADOW"),
             "ek": exp_fixture.experiment_key, "pv": ps.POLICY_VERSION,
             "uv": bundle["manifest"]["population"]["universe_version"],
             "pop": "SHADOW",
             "payload": __import__("json").dumps(
                 {"study": exp_fixture.offline_metrics["study"]})})
        await session.commit()
    check("experimento_e_estudo_persistidos", exp_id > 0, str(exp_id))

    base_patches = [
        patch.object(se, "discover_champion_config", return_value=CHAMPION),
        patch.object(g, "_ALLOW_TEST_APPROVALS", True),
        patch.object(g.time, "time", return_value=now / 1000),
        patch.object(prospective, "_now", return_value=now),
    ]
    for item in base_patches:
        item.start()

    # ── Bundle + aprovações (todas TEST_ONLY, opt-in privado) ─────────────
    registro = await g.register_bundle(db.get_session, exp_id, {
        "confirm": True, "calibration_study_key": report["study_key"],
        "champion_config": CHAMPION}, operator="ADMIN_API_TOKEN")
    # O bundle PERSISTIDO é derivado do experimento REAL do banco (id próprio),
    # então a identidade esperada é recalculada a partir da linha gravada.
    async with db.get_session() as session:
        exp_db = (await session.execute(select(E).where(E.id == exp_id))).scalar_one()
        bundle = g.build_bundle(exp_db, report, CHAMPION, now_ms=now)
    check("bundle_registrado_do_estudo_real", registro.get("ok") is True
          and registro["bundle_hash"] == bundle["bundle_hash"], str(registro))
    repetido = await g.register_bundle(db.get_session, exp_id, {
        "confirm": True, "calibration_study_key": report["study_key"],
        "champion_config": CHAMPION}, operator="ADMIN_API_TOKEN")
    check("registro_de_bundle_e_idempotente", repetido.get("idempotent") is True,
          str(repetido))

    async def aprovar(purpose, *, max_risk_pct=1.5, max_orders=2, etiqueta="",
                      symbols=None):
        """Aprovação TEST_ONLY com limites EXPLÍCITOS.

        O teto aprovado é parte do contrato: 1,5% admite o trade sintético de
        1,0% de risco (controle positivo) e 0,5% o recusa na autorização final
        (controle negativo). Nenhum dos dois amplia sizing.
        """
        payload = approval_payload(bundle, now, purpose)
        payload["limits"]["symbols"] = list(symbols or [SYMBOL])
        payload["limits"]["max_risk_pct"] = max_risk_pct
        payload["limits"]["max_orders"] = max_orders
        payload["validity"]["id"] = f"TEST_ONLY:approval-{purpose}{etiqueta}"
        return await g.register_approval(db.get_session, exp_id, payload,
                                         operator="ADMIN_API_TOKEN", test_only=True)

    shadow_ap = await aprovar("SHADOW")
    check("aprovacao_shadow_persistida", shadow_ap.get("ok") is True, str(shadow_ap))

    # ── Start SHADOW exige a aprovação EXATA (id + geração) ───────────────
    sem_aprovacao = await se.start_preselection_shadow(exp_id)
    check("start_sem_aprovacao_e_bloqueado", sem_aprovacao.get("ok") is False
          and sem_aprovacao.get("reason_code") in ("HUMAN_APPROVAL_MISSING_OR_REVOKED",
                                                   "OPERATIONAL_GENERATION_STALE"),
          str(sem_aprovacao))
    geracao = (await g.get_status(db.get_session))["generation"]
    errado = await se.start_preselection_shadow(exp_id, approval_id="f" * 64,
                                               expected_generation=geracao)
    check("start_com_aprovacao_inexistente_bloqueia", errado.get("ok") is False,
          str(errado))
    iniciado = await se.start_preselection_shadow(
        exp_id, approval_id=shadow_ap["approval_id"], expected_generation=geracao)
    check("start_shadow_congela_a_autoridade", iniciado.get("ok") is True
          and iniciado["approval_id"] == shadow_ap["approval_id"]
          and iniciado["generation"] == geracao, str(iniciado))

    # O fast-path SHADOW também consulta a autoridade real. Idempotência não
    # autoriza trocar a aprovação, dispensá-la ou usar uma geração vencida.
    async with db.get_session() as session:
        antes_repeat = (await session.execute(select(E.shadow_metrics).where(
            E.id == exp_id))).scalar_one()
    repeticao_shadow = await se.start_preselection_shadow(
        exp_id, approval_id=shadow_ap["approval_id"], expected_generation=geracao)
    check("repeticao_shadow_valida_e_idempotente",
          repeticao_shadow.get("ok") is True
          and repeticao_shadow.get("idempotent") is True, str(repeticao_shadow))
    for nome, aid, gen in (("sem_aprovacao", None, geracao),
                            ("aprovacao_inexistente", "f" * 64, geracao),
                            ("geracao_vencida", shadow_ap["approval_id"], geracao + 1)):
        negativa = await se.start_preselection_shadow(
            exp_id, approval_id=aid, expected_generation=gen)
        check("repeticao_shadow_recusa_" + nome, negativa.get("ok") is False
              and negativa.get("blocked") is True, str(negativa))
    async with db.get_session() as session:
        depois_repeat = (await session.execute(select(E.shadow_metrics).where(
            E.id == exp_id))).scalar_one()
    check("repeticoes_preservam_inicio_e_identidade_da_coorte",
          antes_repeat == depois_repeat)

    # ── Evidência prospectiva: coorte vazia/sintética NÃO passa o gate ─────
    # Manifesto TEST_ONLY NUNCA é autoridade prospectiva — e o estudo offline
    # não é reaproveitado como se fossem trades novos.
    avaliado = await se.evaluate_preselection_shadow(exp_id)
    check("evaluate_pre_recusa_autoridade_test_only",
          avaliado.get("ok") is False
          and avaliado.get("reason_code") == "TEST_ONLY_NOT_PROSPECTIVE_AUTHORITY"
          and avaliado.get("offline_used") is False, str(avaliado)[:260])

    # Opt-in de ENGENHARIA (declarado): a MESMA função real de resumo, sobre uma
    # coorte prospectiva VAZIA. Prova a ligação; não prova autorização.
    async def prospectiva_real_vazia(session, exp, *, now_ms=None):
        return prospective.summarize_prospective([], started_at_ms=iniciado["prospective_started_at_ms"],
            now_ms=now, enabled_playbooks=[bundle["manifest"]["candidate"]["selection_rule"]["playbook"]])

    with patch.object(prospective, "load_prospective_evidence", prospectiva_real_vazia):
        avaliado = await se.evaluate_preselection_shadow(exp_id)
    check("evaluate_pre_usa_coorte_prospectiva_e_nao_o_estudo_offline",
          avaliado.get("ok") is True and avaliado.get("offline_used") is False
          and avaliado.get("prospective_source") == prospective.REAL
          and avaliado.get("gate_verdict") != "GO_CANDIDATE", str(avaliado)[:300])
    async with db.get_session() as session:
        exp_row = (await session.execute(select(E).where(E.id == exp_id))).scalar_one()
        medidas = (exp_row.shadow_metrics or {}).get("prospective") or {}
    check("metricas_ausentes_ficam_declaradas_e_nao_zero",
          medidas.get("offline_used") is False
          and medidas.get("gate_verdict") == "NO_GO", str(medidas)[:220])

    # ── 2b. Cadeia prospectiva OFICIAL: decisão observacional (B) ─────────
    # O ciclo OFICIAL do scanner observa a candidata com aprovação SHADOW e a
    # coleta ligada, enquanto o seletor operacional segue LEGACY. A linha é
    # publicada pelo acervo oficial, commitada e lida de volta por conexão NOVA.
    from services import decision_observation_service as obs
    from services import preselection_observation_service as pre
    from services import recommendation_service as scanner
    # A autoridade SHADOW vem do BANCO pelo validador oficial de governança —
    # sem promoção, sem CANARY, com o seletor operacional em LEGACY.
    async with db.get_session() as session:
        shadow_auth_db = await g.assert_authority_in_session(
            session, exp_id=exp_id, purpose="SHADOW",
            approval_id=shadow_ap["approval_id"],
            expected_generation=iniciado["generation"])
        await session.rollback()
    check("autoridade_shadow_vem_do_banco_sem_promocao",
          shadow_auth_db.get("ok") is True
          and shadow_auth_db.get("purpose") == "SHADOW"
          and shadow_auth_db["approval"]["approval_id"] == shadow_ap["approval_id"]
          and os.environ.get("R13_OPERATIONAL_SELECTOR") is None,
          str(shadow_auth_db)[:200])
    # O carregador AUTOMÁTICO do scanner exige manifesto de pesquisa APROVADO
    # (não TEST_ONLY): aqui ele recusa. Fronteira declarada — e é por isso que a
    # autoridade acima é obtida pelo validador, não fabricada.
    check("carregador_automatico_recusa_manifesto_test_only",
          await adapter.load_shadow_authority(db.get_session) is None)
    from tests.test_lote03_candidate_adapter import FEATURES as FEATURES_BASE
    FEATURES_SHADOW = {**FEATURES_BASE, "structure_quality": 0.4,
                       "trigger_body_ratio": 0.45, "volume_ratio": 1.2}
    grupo_shadow = adapter.shadow_group_decision(shadow_auth_db, [
        {"features": {**FEATURES_SHADOW}, "symbol": SYMBOL, "side": "long",
         "timeframe": "4h", "entry": 100.0, "stop_loss": 95.0, "tp1": 105.0,
         "tp2": 110.0},
        {"features": {}, "symbol": SYMBOL, "side": "long", "timeframe": "1h",
         "entry": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0}],
        now_ms=now)
    bloco_shadow = adapter.shadow_decision_for_timeframe(
        grupo_shadow.get("group"), "4h") if grupo_shadow.get("ok") else None
    from tests.test_lote02_preselection_capture import sinal as sinal_real
    linha_pre = scanner._preselection_candidate(
        sinal_real(symbol=SYMBOL, timeframe="4h", conf=78), 78.0,
        stages=[{"stage": "CANDIDATE", "verdict": "PASSED", "reason_code": None}],
        accepted=True, features={"atr": 2.0, "score": 78.0},
        shadow=bloco_shadow)
    with patch.dict(os.environ, {pre.MODE_ENV: pre.MODE_OBSERVE,
                                 "R13_OPERATIONAL_SELECTOR": "LEGACY"}):
        publicado = obs.observe_preselection([linha_pre])
        await obs.flush_pending()
    async with db.get_session() as session:
        gravadas = (await session.execute(select(O.frozen_config).where(
            O.scope == "PRE_SELECTION"))).scalars().all()
    payloads_pre = [(c or {}).get("r09_pre_selection") or {} for c in gravadas]
    com_shadow = [p for p in payloads_pre if p.get("shadow_decision")]
    check("decisao_observacional_persiste_pela_cadeia_oficial",
          publicado.get("accepted") == 1 and len(com_shadow) == 1
          and com_shadow[0]["shadow_decision"]["scope"] == pre.SHADOW_DECISION_SCOPE
          and com_shadow[0]["shadow_decision"]["authority"]["purpose"] == "SHADOW"
          and com_shadow[0]["observed_decision_scope"] == "FINAL_SCANNER_SELECTION",
          str(com_shadow)[:260] if com_shadow else str(payloads_pre)[:260])
    check("autoridade_test_only_nao_cria_anotacao_prospectiva",
          all(prospective.KEY not in p for p in payloads_pre),
          str([list(p) for p in payloads_pre])[:200])

    # ── 2c. Cadeia prospectiva OFICIAL: proteção simulada (C) ─────────────
    # Anotação construída pelo congelador REAL, persistida no PG e resolvida
    # pelo resolver OFICIAL (`resolve_pending`). A trilha de proteção tem de
    # sobreviver ao JSON e à leitura por conexão nova — e continuar TEST_ONLY.
    from tests.test_lote03_prospective_shadow import engineering_fixture
    from services import research_dataset_service as ds
    _exp_eng, ctx_eng, row_eng, prices_eng = engineering_fixture()
    ann_eng = prospective.build_preselection_annotation(row_eng, ctx_eng)
    chave_eng = row_eng["opportunity_key"]
    cfg_eng = copy.deepcopy(row_eng["frozen_config"])
    cfg_eng["r09_pre_selection"][prospective.KEY] = ann_eng
    async with db.get_session() as session:
        session.add(O(scope="PRE_SELECTION", opportunity_key=chave_eng,
                      identity_source="PRE_SELECTION_SETUP", symbol="BTCUSDT",
                      frozen_setup=row_eng["frozen_setup"], frozen_config=cfg_eng,
                      first_decision="ACCEPTED",
                      first_seen_at=row_eng["observed_at"],
                      last_seen_at=row_eng["observed_at"],
                      first_decision_observed_at=row_eng["observed_at"]))
        await session.commit()
    velas_eng = [{"timestamp": c["timestamp_ms"], **{k: c[k] for k in
                 ("open", "high", "low", "close", "volume")}}
                 for c in prices_eng["windows"][chave_eng]]
    relogio_eng = max(c["timestamp"] for c in velas_eng) + 300_000
    async with db.get_session() as session:
        await prospective.resolve_pending(session, {"BTCUSDT": {
            "candles": velas_eng, "as_of": ds.ms_datetime(relogio_eng)}})
        await session.commit()
    await db._engine.dispose()                     # leitura por conexão NOVA
    async with db.get_session() as session:
        lida = (await session.execute(select(O.frozen_config).where(
            O.opportunity_key == chave_eng))).scalar_one()
    ann_lida = lida["r09_pre_selection"][prospective.KEY]
    medida_protecao = prospective.protection_measure([ann_lida])
    check("protecao_simulada_sobrevive_ao_resolver_oficial_e_ao_restart",
          prospective.verify_annotation(ann_lida)
          and ann_lida["resolution"]["status"] in prospective.TERMINAL
          and ann_lida["resolution"]["protection"]["source"] == "SHADOW_SIMULATED"
          and ann_lida["resolution"]["protection"]["proves_real_sl"] is False
          and medida_protecao["protection_applicable"] == 1
          and medida_protecao["protection_observed"] == 1,
          str(medida_protecao)[:240])
    resumo_eng = prospective.summarize_prospective(
        [ann_lida], started_at_ms=ctx_eng["started_at_ms"],
        now_ms=ctx_eng["manifest"]["split"]["as_of_ms"],
        enabled_playbooks=[bundle["manifest"]["candidate"]["selection_rule"]["playbook"]])
    check("protecao_simulada_nunca_vira_sl_real_nem_promove",
          resumo_eng["measurements"]["protection_scope"] == "SHADOW_SIMULATED"
          and prospective.PROTECTION_DECISION_REQUIRED
          in resumo_eng["evidence"]["essential_gaps"]
          and resumo_eng["gate"]["verdict"] == "NO_GO"
          and resumo_eng["promotable"] is False
          and ann_lida["frozen"]["source_mode"] == prospective.TEST,
          str(resumo_eng["gate"]["reason_codes"])[:200])

    # Prova adversarial PERSISTIDA: o hash do congelado segue válido, mas o
    # conteúdo da trilha é contraditório. O consumidor deve perder cobertura,
    # não atestar zero pelo contador declarado.
    corrompida = copy.deepcopy(lida)
    ann_corrompida = corrompida["r09_pre_selection"][prospective.KEY]
    trilha = ann_corrompida["resolution"]["protection"]
    primeira = trilha["observations"][0]
    primeira.update(active_stop=None, stop_finite=False,
                    geometry="UNKNOWN", geometry_valid=False)
    from services import offline_replay_service as replay
    trilha["failures"].append({"code": replay.PROTECTION_STOP_NOT_FINITE, "stage": primeira["stage"],
                               "timestamp_ms": primeira["timestamp_ms"], "resolved": False})
    trilha["pending_failures"] = 0
    from sqlalchemy import update
    async with db.get_session() as session:
        await session.execute(update(O).where(O.opportunity_key == chave_eng)
                              .values(frozen_config=corrompida))
        await session.commit()
    await db._engine.dispose()
    async with db.get_session() as session:
        prova_invalida = (await session.execute(select(O.frozen_config).where(
            O.opportunity_key == chave_eng))).scalar_one()
    ann_invalida = prova_invalida["r09_pre_selection"][prospective.KEY]
    medida_invalida = prospective.protection_measure([ann_invalida])
    check("trilha_contraditoria_persistida_nao_certifica_zero",
          prospective.verify_annotation(ann_invalida)
          and medida_invalida["protection_observed"] == 0
          and medida_invalida["unresolved_protection_failures"] is None,
          str(medida_invalida))
    async with db.get_session() as session:
        await session.execute(update(O).where(O.opportunity_key == chave_eng)
                              .values(frozen_config=lida))
        await session.commit()

    # ── 2d. Funding indisponível: economia fica DESCONHECIDA, não zero ────
    sem_funding = copy.deepcopy(ann_lida)
    sem_funding["frozen"] = {**sem_funding["frozen"], "costs_config": {
        **sem_funding["frozen"]["costs_config"], "funding_bps_per_bar": None}}
    sem_funding["annotation_hash"] = prospective.digest(sem_funding["frozen"])
    sem_funding["resolution"] = {"status": "PENDING", "net_r": None}
    linha_sf = copy.deepcopy(row_eng)
    linha_sf["frozen_config"]["r09_pre_selection"][prospective.KEY] = sem_funding
    refeita = prospective.resolve_annotation(SimpleNamespace(**linha_sf), {
        "candles": velas_eng, "as_of": ds.ms_datetime(relogio_eng)})
    ann_sf = refeita["r09_pre_selection"][prospective.KEY]
    medidas_sf = prospective.operational_measurements([ann_sf], [], {}, now_ms=now)
    # Custo de funding ausente ⇒ economia INCONHECÍVEL: o resolver oficial recusa
    # a resolução (INVALID, motivo declarado) em vez de fabricar R=0, a falha vai
    # para a trilha de RESOLUÇÃO e a proteção continua `None` (sem obrigação
    # observada não existe zero).
    check("funding_indisponivel_nao_vira_zero_e_bloqueia",
          ann_sf["resolution"]["status"] == "INVALID"
          and ann_sf["resolution"]["reason_code"] == "PROSPECTIVE_REPLAY_INVALID"
          and ann_sf["resolution"]["net_r"] is None
          and medidas_sf["resolution_failures"] == 1
          and medidas_sf["operational_failures"] >= 1
          and medidas_sf["unresolved_protection_failures"] is None,
          str(ann_sf["resolution"])[:200] + str(medidas_sf)[:200])


    promocao_ap = await aprovar("PROMOTION")
    check("aprovacao_promotion_e_separada_da_shadow",
          promocao_ap.get("ok") is True
          and promocao_ap["approval_id"] != shadow_ap["approval_id"],
          str(promocao_ap))
    geracao = (await g.get_status(db.get_session))["generation"]
    bloqueada = await se.promote_preselection(
        exp_id, approval_id=promocao_ap["approval_id"],
        expected_generation=geracao, operator="ADMIN_API_TOKEN")
    check("promocao_sem_prova_prospectiva_e_bloqueada",
          bloqueada.get("ok") is False
          and bloqueada.get("reason_code") == "PROSPECTIVE_GO_NO_GO_NOT_PASSED"
          and bloqueada.get("live_approval") == "UNAVAILABLE", str(bloqueada))

    # Opt-in de ENGENHARIA, declarado: evidência prospectiva TEST_ONLY aprovada.
    # Isto prova o CAMINHO, não a autorização operacional.
    async def prospectiva_go(session, exp, *, now_ms=None):
        return {"available": True, "source": prospective.REAL, "state": "GATE_PASSED",
                "gate": {"verdict": "GO_CANDIDATE"}, "offline_used": False,
                "fingerprint": "a" * 64, "test_only_engineering_opt_in": True}

    with patch.object(prospective, "load_prospective_evidence", prospectiva_go):
        promovido = await se.promote_preselection(
            exp_id, approval_id=promocao_ap["approval_id"],
            expected_generation=geracao, operator="ADMIN_API_TOKEN")
    check("promocao_governada_publica_referencia_sem_ligar_seletor",
          promovido.get("ok") is True and promovido.get("status") == "ELIGIBLE"
          and promovido.get("selector_env_unchanged") is True
          and promovido.get("live_approval") == "CANARY_APPROVAL_STILL_REQUIRED"
          and os.environ.get("R13_OPERATIONAL_SELECTOR") is None, str(promovido))
    canario_ap = await aprovar("CANARY")
    check("aprovacao_canary_exige_registro_proprio", canario_ap.get("ok") is True,
          str(canario_ap))

    # ── Autoridade CANDIDATE só com seletor explícito ──────────────────────
    sem_seletor = await adapter.load_operational_view(db.get_session)
    check("eligible_sem_seletor_nao_autoriza",
          sem_seletor.get("mode") == "LEGACY" and sem_seletor.get("ok") is True,
          str(sem_seletor))
    selector_env = {"R13_OPERATIONAL_SELECTOR": "CANDIDATE",
                    "R13_OPERATIONAL_EXPERIMENT_ID": str(exp_id)}
    with patch.dict(os.environ, selector_env):
        visao = await adapter.load_operational_view(db.get_session)
    check("autoridade_candidata_carregada_do_banco",
          adapter.candidate_mode_active(visao)
          and visao["approval"]["approval_id"] == canario_ap["approval_id"]
          and visao["bundle"]["bundle_hash"] == bundle["bundle_hash"], str(visao)[:200])

    # ══════════════════════════════════════════════════════════════════════
    #  Borda HTTP assinada FALSA + caller real do executor
    # ══════════════════════════════════════════════════════════════════════
    ENVIADOS: list = []
    ASSINADOS: list = []
    EXCHANGE = {"positions": [], "orders": [], "algo": []}

    #: Rejeição GTX DEFINITIVA (-5022) na primeira LIMIT post-only: é o único
    #: estado que prova que a maker NÃO foi aceita e habilita a filha `-mfb`.
    GTX_REJEITA = {"ativo": False, "feito": False}

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
        if (path == "/fapi/v1/order" and GTX_REJEITA["ativo"]
                and not GTX_REJEITA["feito"]
                and str(params.get("timeInForce") or "") == "GTX"):
            GTX_REJEITA["feito"] = True
            return SimpleNamespace(status_code=400, headers={}, json=lambda: {
                "code": -5022, "msg": "Post only order will be rejected"})
        corpo: object = {}
        if path == "/fapi/v1/order":
            corpo = {"orderId": 7, "status": "FILLED", "executedQty": "1",
                     "avgPrice": "100", "cumQuote": "100"}
        elif path == "/fapi/v1/algoOrder":
            corpo = {"algoId": "A7", "status": "NEW"}
        elif path == "/fapi/v1/leverage":
            corpo = {"leverage": 5, "symbol": "X"}
        elif path == "/fapi/v2/positionRisk":
            corpo = [{"symbol": "X", "positionAmt": "0", "entryPrice": "0",
                      "markPrice": "0", "unRealizedProfit": "0",
                      "leverage": "5", "updateTime": 1}]
        return SimpleNamespace(status_code=200, headers={}, json=lambda: corpo)

    def posts_de_entrada():
        return [e for e in ENVIADOS if e["method"] == "POST"
                and e["path"] == "/fapi/v1/order"
                and not e["params"].get("reduceOnly")
                and not e["params"].get("closePosition")]

    def posts_de_protecao():
        return [e for e in ENVIADOS if e["method"] == "POST"
                and (e["path"] == "/fapi/v1/algoOrder"
                     or (e["path"] == "/fapi/v1/order"
                         and (e["params"].get("reduceOnly")
                              or e["params"].get("closePosition"))))]

    transport_patches = [
        patch.object(bss, "is_configured", return_value=True),
        patch.object(bss, "_build_signed_url", assinar),
        patch.object(bss, "_get_client", return_value=SimpleNamespace(request=request)),
        patch.object(bss, "_ban_until_ms", 0),
        patch.object(bss, "_throttle_until_ms", 0),
        patch.object(bss, "get_positions",
                     AsyncMock(side_effect=lambda symbol=None, force=False: {
                         "ok": True, "positions": list(EXCHANGE["positions"])})),
        patch.object(bss, "get_open_orders",
                     AsyncMock(side_effect=lambda symbol=None: {
                         "ok": True, "orders": list(EXCHANGE["orders"])})),
        patch.object(bss, "get_open_algo_orders",
                     AsyncMock(side_effect=lambda symbol=None: {
                         "ok": True, "orders": list(EXCHANGE["algo"])})),
        patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda s, q: float(q))),
        patch.object(bss, "_round_price", AsyncMock(side_effect=lambda s, p: float(p))),
        patch.object(bss, "_get_symbol_filters", AsyncMock(return_value={
            "step": 0.001, "min_qty": 0.001, "max_qty": 10_000.0,
            "market_step": 0.001, "market_min_qty": 0.001,
            "market_max_qty": 10_000.0, "min_notional": 5.0, "tick": 0.01})),
        patch.object(bss, "accounting_scope", lambda: ESCOPO),
        patch.object(mps, "current_account_scope", return_value=ESCOPO),
    ]
    for item in transport_patches:
        item.start()

    LIVRO = {"bid": 99.95, "ask": 100.05}
    SKIPS: list = []

    async def quote_fn(symbol, timeout_s=None):
        instante = clock_ms()
        return {"ok": True, "exchange": "binance", "source": "binance_book_ticker",
                "symbol": bss.to_binance(symbol), "message_time_ms": float(instante),
                "bid": LIVRO["bid"], "ask": LIVRO["ask"], "bid_qty": 500.0,
                "ask_qty": 500.0, "received_at_ms": float(instante),
                "exchange_time_ms": float(instante), "latency_ms": 25.0}

    #: Ponto REAL de injeção da revogação: a profundidade é buscada DEPOIS da
    #: reserva e ANTES do POST (preflight do transporte). Revogar aqui prova que
    #: a autoridade antiga não revive na autorização final.
    REVOGAR_NO_DEPTH = {"ativo": False, "feito": False, "approval_id": None}

    async def depth_fn(symbol, limit=None, timeout_s=None):
        if REVOGAR_NO_DEPTH["ativo"] and not REVOGAR_NO_DEPTH["feito"]:
            REVOGAR_NO_DEPTH["feito"] = True
            estado_meio = await g.get_status(db.get_session)
            await g.revoke_approval(db.get_session, exp_id,
                                    REVOGAR_NO_DEPTH["approval_id"],
                                    estado_meio["generation"], "ADMIN_API_TOKEN")
        instante = clock_ms()
        return {"ok": True, "exchange": "binance", "source": "binance_depth",
                "symbol": symbol, "last_update_id": 42,
                "message_time_ms": float(instante),
                "bids": [[str(LIVRO["bid"]), "5000"], ["99.85", "5000"]],
                "asks": [[str(LIVRO["ask"]), "5000"], ["100.15", "5000"]],
                "received_at_ms": float(instante),
                "exchange_time_ms": float(instante), "latency_ms": 25.0}

    FLAGS = {"DB_ENABLED": True, "SHADOW_ENABLED": False,
             "REGIME_SIZING_ENABLED": False, "P04C_DATA_FRESHNESS_ENABLED": False,
             "FILLER_FORA_ENABLED": False, "NEWS_GATE_ENABLED": False,
             "DAILY_PROFIT_TP_ENABLED": False, "PROXIMITY_GATE_ENABLED": False,
             "STRUCT_CHASE_GATE_ENABLED": False, "ATR_GATE_ENABLED": False,
             "RR_GATE_ENABLED": False, "PROB_TP1_GATE_ENABLED": False,
             "SCORE_ADJUSTERS_ENABLED": False, "SCORE_MIN": 0,
             "QUALITY_EDGE_GATE_ENABLED": False, "LIQUIDITY_GATE_ENABLED": False,
             "SYMBOL_BLACKLIST": set(), "MAKER_ENTRY_ENABLED": False,
             "P04A_ENTRY_REVALIDATION_ENABLED": True, "FILL_RR_GATE_ENABLED": False,
             "ENTRY_COOLDOWN_SECONDS": 0, "ENTRY_MAX_PER_HOUR": 999,
             "MAX_OPEN_PER_DIRECTION": 99}

    async def rodar_ciclo(recs, *, env=None, extra=(), limpar=True):
        if limpar:
            ENVIADOS.clear()
            ASSINADOS.clear()
            SKIPS.clear()
            LOGS.clear()
        with ExitStack() as stack:
            for nome, valor in FLAGS.items():
                stack.enter_context(patch.object(sts, nome, valor))
            stack.enter_context(patch.dict(os.environ, env or {}))
            # O relógio do P03 segue o MESMO instante congelado das bordas de
            # mercado: misturar `datetime.now` real com `time.time` congelado
            # fazia a carteira falsa parecer stale (segunda causa comprovada).
            stack.enter_context(patch.object(
                intents, "_now",
                lambda: datetime.fromtimestamp(clock_ms() / 1000, timezone.utc)))
            stack.enter_context(patch.object(edge_decay_service, "is_enabled",
                                             return_value=False))
            stack.enter_context(patch.object(sts, "_p04c_live_data_verdict",
                                             return_value={"ok": True}))
            stack.enter_context(patch.object(sts, "_calibration_contract_verdict",
                                             return_value={"ok": True}))
            stack.enter_context(patch.object(sts, "get_exec_allowlist",
                                             return_value=set()))
            stack.enter_context(patch.object(sts, "_is_blocked_time",
                                             return_value=(False, "")))
            stack.enter_context(patch.object(
                sts, "_record_skip",
                lambda rec, stage, reason=None, **kw: SKIPS.append((stage, reason))))
            stack.enter_context(patch.object(
                sts, "_observe_decision",
                lambda rec, state, reason=None, **kw: SKIPS.append((state, reason))))
            stack.enter_context(patch.object(
                sts, "_resolve_equity_usd", AsyncMock(return_value=(10_000.0, "live"))))
            stack.enter_context(patch.object(
                sts, "_free_margin_snapshot", AsyncMock(side_effect=lambda: {
                    "available_usd": 8_000.0, "as_of_ms": clock_ms(),
                    "observed_start_ms": clock_ms() - 5, "observed_end_ms": clock_ms(),
                    "quality": "live", "source": "live"})))
            stack.enter_context(patch.object(
                kill_switch_service, "check_can_trade",
                AsyncMock(return_value={"allowed": True, "can_trade": True,
                                        "ok": True, "reason": None})))
            stack.enter_context(patch.object(exchange_service,
                                             "get_execution_quote", quote_fn, create=True))
            stack.enter_context(patch.object(exchange_service,
                                             "get_execution_depth", depth_fn, create=True))
            stack.enter_context(patch.object(exchange_service, "ACTIVE_EXCHANGE",
                                             "binance"))
            # Preço de marca é FRONTEIRA DE MERCADO (OKX público): sem este mock
            # o caller tenta resolver www.okx.com — bloqueado e contado pelo
            # guard, mas é ruído externo, não parte da lógica provada aqui.
            stack.enter_context(patch.object(
                sts, "_get_mark_price", AsyncMock(return_value=100.0)))
            for item in extra:
                stack.enter_context(item)
            return await sts.open_shadow_for_recs(recs)

    async def rodar_ciclo_sem_limpar(recs, *, env=None, extra=()):
        """Variante para DOIS consumidores concorrentes: o contador é comum."""
        return await rodar_ciclo(recs, env=env, extra=extra, limpar=False)

    async def criar_snapshot(symbol):
        """Recomendação persistida mínima (o caller real precisa do vínculo)."""
        from models.recommendation_snapshot import RecommendationSnapshot
        async with db.get_session() as session:
            linha = RecommendationSnapshot(
                symbol=mps.canonical_symbol(symbol), timeframe="4h", tier="A",
                direction="long", entry=100.0, stop_loss=95.0, tp1=105.0,
                tp2=110.0, score=90.0, risk_reward=2.0, leverage=5,
                risk_pct=1.0, stop_distance_pct=5.0, status="open",
                created_at=datetime.now(timezone.utc))
            session.add(linha)
            await session.flush()
            identificador = int(linha.id)
            await session.commit()
        return identificador

    def rec_base(symbol, snap_id, **extra):
        base = {"_just_saved": True, "tier": "A", "symbol": symbol, "score": 90,
                "timeframe": "4h", "playbook": "CHAMPION_LEGACY", "entry": 100.0,
                "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0, "leverage": 5,
                "side": "long", "direction": "long", "snapshot_id": snap_id,
                "signal": {"indicators": {"atr": 2.0}, "tp1": 105.0},
                "score_provenance": {},
                "data_freshness": {"candle": {"close_time_ms": clock_ms()}}}
        base.update(extra)
        return base

    async def desbloquear_conta():
        """Época de validação manual da conta: sem ela o P03 recusa toda entrada
        com `MANUAL_ACCOUNT_VALIDATION_BLOCKED` (causa comprovada da parada
        anterior do harness — fixture, não defeito de produção)."""
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

    async def limpar_intents():
        async with db.get_session() as session:
            await session.execute(text("DELETE FROM entry_intents"))
            await session.execute(text("DELETE FROM real_trades"))
            await session.commit()

    # ── 1. Paridade OFF × LEGACY pelo caller REAL ─────────────────────────
    from models.account_margin_epoch import AccountMarginEpoch
    async with db._engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all,
                            tables=[AccountMarginEpoch.__table__])
    await desbloquear_conta()
    snap_legacy = await criar_snapshot(SYMBOL)
    await limpar_intents()
    legacy = await rodar_ciclo([rec_base(SYMBOL, snap_legacy)],
                               env={"R13_OPERATIONAL_SELECTOR": "LEGACY"})
    legacy_posts = [dict(e) for e in posts_de_entrada()]
    legacy_protecao = len(posts_de_protecao())
    async with db.get_session() as session:
        legacy_payload = (await session.execute(select(
            EntryIntent.decision_payload))).scalars().all()
    await limpar_intents()
    snap_off = await criar_snapshot(SYMBOL)
    off = await rodar_ciclo([rec_base(SYMBOL, snap_off)],
                            env={"R13_OPERATIONAL_SELECTOR": "OFF"})
    off_posts = [dict(e) for e in posts_de_entrada()]
    check("paridade_off_legacy_mesmas_decisoes_e_chamadas",
          legacy == off == 1 and len(legacy_posts) == len(off_posts) == 1
          and legacy_posts[0]["params"].get("quantity") == off_posts[0]["params"].get("quantity")
          and legacy_protecao == len(posts_de_protecao()),
          f"{legacy}/{off} {legacy_posts} {off_posts}")
    check("legado_nao_grava_campo_candidato",
          all("operational_selection" not in (p or {}) for p in legacy_payload),
          str(legacy_payload)[:200])

    # ── 2. Caminho CANDIDATO completo (um efeito lógico) ──────────────────
    async def contexto_candidato(symbol, snap_id):
        with patch.dict(os.environ, selector_env):
            visao_atual = await adapter.load_operational_view(db.get_session)
        assert adapter.candidate_mode_active(visao_atual), visao_atual
        from tests.test_lote03_candidate_adapter import FEATURES
        # O artefato REAL desta fixture só suporta a faixa 70–80 (n=313): as
        # features são escolhidas para cair nela. Faixa não suportada devolve
        # CANDIDATE_CALIBRATION_UNAVAILABLE — e isso é o contrato, não um ajuste.
        features = {**FEATURES, "structure_quality": 0.4,
                    "trigger_body_ratio": 0.45, "volume_ratio": 1.2}
        decisao = adapter.candidate_decision(
            visao_atual, features=features, symbol=symbol, side="long",
            timeframe="4h", entry=100.0, stop_loss=95.0, tp1=105.0, tp2=110.0,
            now_ms=now)
        assert decisao.get("ok") is True, decisao
        return decisao["context"]

    await limpar_intents()
    snap_cand = await criar_snapshot(SYMBOL)
    contexto = await contexto_candidato(SYMBOL, snap_cand)
    rec_cand = rec_base(SYMBOL, snap_cand, operational_selection=contexto)
    abertos = await rodar_ciclo([rec_cand], env=selector_env)
    entradas = posts_de_entrada()
    async with db.get_session() as session:
        linhas = (await session.execute(select(EntryIntent))).scalars().all()
    persistido = [(l.decision_payload or {}).get("operational_selection") for l in linhas]
    check("candidata_percorre_executor_oficial_com_um_post",
          abertos == 1 and len(entradas) == 1 and len(posts_de_protecao()) >= 1,
          f"{abertos} entradas={len(entradas)} protecao={len(posts_de_protecao())} {SKIPS}")
    check("contexto_governado_persistido_antes_do_post",
          len(linhas) == 1 and persistido[0] is not None
          and adapter.same_identity(persistido[0], contexto)
          and persistido[0]["authority"]["approval"]["approval_id"] == canario_ap["approval_id"],
          str(persistido)[:220])
    async with db.get_session() as session:
        propostas = (await session.execute(select(
            EntryIntent.decision_payload))).scalars().all()
    proposta_ctx = [((p or {}).get("proposals") or {}) for p in propostas]
    check("proposta_congelada_carrega_a_mesma_identidade",
          any(adapter.same_identity(
              (v or {}).get("operational_selection") or {}, contexto)
              for grupo in proposta_ctx for v in grupo.values()),
          str(proposta_ctx)[:220])

    # ── 3. Aprovação revogada ⇒ zero POST de entrada ──────────────────────
    await limpar_intents()
    estado = await g.get_status(db.get_session)
    revogado = await g.revoke_approval(db.get_session, exp_id,
                                       canario_ap["approval_id"],
                                       estado["generation"], "ADMIN_API_TOKEN")
    check("revogacao_persiste_e_bloqueia_entradas",
          revogado.get("ok") is True and revogado.get("preserved_positions_and_protection") is True,
          str(revogado))
    snap_rev = await criar_snapshot(SYMBOL)
    rec_rev = rec_base(SYMBOL, snap_rev, operational_selection=contexto)
    sem_post = await rodar_ciclo([rec_rev], env=selector_env)
    check("autoridade_revogada_nao_envia_entrada",
          sem_post == 0 and posts_de_entrada() == [], f"{sem_post} {SKIPS}")
    # MESMO payload da aprovação revogada ⇒ mesmo `approval_id`: reenviar não
    # revive a autoridade (um payload diferente seria outra aprovação, não uma
    # reativação).
    reativar = await aprovar("CANARY")
    check("aprovacao_revogada_nao_revive_com_o_mesmo_payload",
          reativar.get("ok") is False
          and reativar.get("reason_code") == "APPROVAL_ALREADY_REVOKED",
          str(reativar))

    # ── 4. Revogação DURANTE a espera: autoridade antiga não revive ───────
    await limpar_intents()
    # Payload DIFERENTE (outra referência de validade) ⇒ outra aprovação, não
    # reativação da revogada.
    nova_canary = await aprovar("CANARY", etiqueta="-2")
    check("nova_aprovacao_canary_registrada",
          nova_canary.get("ok") is True
          and nova_canary["approval_id"] != canario_ap["approval_id"],
          str(nova_canary))
    estado = await g.get_status(db.get_session)
    promocao2 = await aprovar("PROMOTION")
    with patch.object(prospective, "load_prospective_evidence", prospectiva_go):
        await se.promote_preselection(exp_id, approval_id=promocao2["approval_id"],
                                      expected_generation=(await g.get_status(
                                          db.get_session))["generation"],
                                      operator="ADMIN_API_TOKEN")
    contexto2 = await contexto_candidato(SYMBOL, snap_cand)
    snap_mid = await criar_snapshot(SYMBOL)

    REVOGAR_NO_DEPTH.update(
        ativo=True, feito=False,
        approval_id=contexto2["authority"]["approval"]["approval_id"])
    meio = await rodar_ciclo(
        [rec_base(SYMBOL, snap_mid, operational_selection=contexto2)],
        env=selector_env)
    REVOGAR_NO_DEPTH["ativo"] = False
    check("revogacao_durante_a_espera_zera_o_post",
          meio == 0 and posts_de_entrada() == []
          and REVOGAR_NO_DEPTH["feito"] is True, f"{meio} {SKIPS}")

    # ── 5. Duas conexões: aprovação × revogação e dois promotores ─────────
    await limpar_intents()
    estado = await g.get_status(db.get_session)
    aprovacao_concorrente, revogacao_concorrente = await asyncio.gather(
        aprovar("SHADOW"),
        g.revoke_approval(db.get_session, exp_id, shadow_ap["approval_id"],
                          estado["generation"], "ADMIN_API_TOKEN"))
    vencedores = [r for r in (aprovacao_concorrente, revogacao_concorrente)
                  if r.get("ok") is True and not r.get("idempotent")]
    check("aprovar_x_revogar_concorrente_serializa_sem_perder_historico",
          len(vencedores) >= 1, f"{aprovacao_concorrente} / {revogacao_concorrente}")
    async with db.get_session() as session:
        historico = ((await session.execute(select(S.payload).where(
            S.state_key == g.STATE_KEY))).scalar_one() or {}).get("history") or []
    check("historico_de_governanca_e_durave_e_ordenado",
          len(historico) >= 6
          and [h["generation"] for h in historico] == sorted(h["generation"] for h in historico),
          str(len(historico)))
    estado = await g.get_status(db.get_session)
    promocao3 = await aprovar("PROMOTION")
    with patch.object(prospective, "load_prospective_evidence", prospectiva_go):
        geracao_atual = (await g.get_status(db.get_session))["generation"]
        primeiro, segundo = await asyncio.gather(
            se.promote_preselection(exp_id, approval_id=promocao3["approval_id"],
                                    expected_generation=geracao_atual,
                                    operator="ADMIN_API_TOKEN"),
            se.promote_preselection(exp_id, approval_id=promocao3["approval_id"],
                                    expected_generation=geracao_atual,
                                    operator="ADMIN_API_TOKEN"))
    efetivos = [r for r in (primeiro, segundo)
                if r.get("ok") is True and not r.get("idempotent")]
    check("dois_promotores_um_efeito_logico",
          len(efetivos) == 1, f"{primeiro} / {segundo}")

    # ── 5b. Filha `-mfb`: proposta e autorização FINAL próprias ───────────
    # GTX rejeitada de forma DEFINITIVA → fallback MARKET com COID filho. A
    # filha não herda a autorização da mãe: ela congela proposta própria e passa
    # pela MESMA autorização final antes de assinar.
    await limpar_intents()
    canary_mfb = await aprovar("CANARY", etiqueta="-mfb")
    check("aprovacao_canary_para_a_filha_registrada",
          canary_mfb.get("ok") is True, str(canary_mfb))
    snap_mfb = await criar_snapshot(SYMBOL)
    contexto_mfb = await contexto_candidato(SYMBOL, snap_mfb)
    GTX_REJEITA.update(ativo=True, feito=False)
    maker_patches = [patch.object(sts, "MAKER_ENTRY_ENABLED", True),
                     patch.object(sts, "MAKER_FALLBACK_MARKET", True),
                     patch.object(sts, "P04B_MAKER_FALLBACK_ENABLED", True),
                     patch.object(sts, "P04B_MARKET_REVALIDATION_ENABLED", True)]
    com_filha = await rodar_ciclo(
        [rec_base(SYMBOL, snap_mfb, operational_selection=contexto_mfb)],
        env=selector_env, extra=maker_patches)
    GTX_REJEITA["ativo"] = False
    entradas_mfb = posts_de_entrada()
    coids = [e["params"].get("newClientOrderId") or e["params"].get("clientOrderId")
             for e in entradas_mfb]
    filhas = [c for c in coids if str(c or "").endswith("-mfb")]
    async with db.get_session() as session:
        payloads_mfb = (await session.execute(select(
            EntryIntent.decision_payload))).scalars().all()
    propostas_mfb = {}
    for p in payloads_mfb:
        propostas_mfb.update((p or {}).get("proposals") or {})
    check("filha_mfb_tem_proposta_e_autorizacao_propria",
          com_filha == 1 and GTX_REJEITA["feito"] is True and len(filhas) == 1
          and len(propostas_mfb) >= 2
          and all(adapter.same_identity(
              (v or {}).get("operational_selection") or {}, contexto_mfb)
              for v in propostas_mfb.values()),
          f"abertos={com_filha} coids={coids} propostas={list(propostas_mfb)}")

    # ── 5c. Último slot: o teto de ordens aprovado é fronteira dura ────────
    #: Exclusividade do propósito: só UMA aprovação CANARY ativa. Trocar o teto
    #: exige a sequência GOVERNADA inteira — revogar, promover de novo com
    #: PROMOTION própria e só então aprovar o novo CANARY. Nada é reaproveitado.
    CANARY_ATIVA = {"id": canary_mfb["approval_id"]}

    async def renovar_canary(etiqueta, **limites):
        estado_t = await g.get_status(db.get_session)
        await g.revoke_approval(db.get_session, exp_id, CANARY_ATIVA["id"],
                                estado_t["generation"], "ADMIN_API_TOKEN")
        prom = await aprovar("PROMOTION", etiqueta=etiqueta)
        with patch.object(prospective, "load_prospective_evidence", prospectiva_go):
            await se.promote_preselection(
                exp_id, approval_id=prom["approval_id"],
                expected_generation=(await g.get_status(db.get_session))["generation"],
                operator="ADMIN_API_TOKEN")
        nova = await aprovar("CANARY", etiqueta=etiqueta, **limites)
        if nova.get("ok"):
            CANARY_ATIVA["id"] = nova["approval_id"]
        return nova

    # Dois símbolos APROVADOS e um único slot: a segunda entrada é de OUTRO
    # símbolo (dentro da população aprovada), senão o guard de duplicata do
    # executor bloquearia antes de o teto de ordens ser avaliado.
    SEGUNDO = ("ADA" + SYMBOL[len(SYMBOL.split("/")[0]):]) if "/" in SYMBOL else "ADAUSDT"
    await limpar_intents()
    canary_slot = await renovar_canary("-slot", max_orders=1,
                                       symbols=[SYMBOL, SEGUNDO])
    check("aprovacao_canary_com_um_slot", canary_slot.get("ok") is True,
          str(canary_slot))
    snap_a = await criar_snapshot(SYMBOL)
    contexto_slot = await contexto_candidato(SYMBOL, snap_a)
    primeira_entrada = await rodar_ciclo(
        [rec_base(SYMBOL, snap_a, operational_selection=contexto_slot)],
        env=selector_env)
    posts_primeira = len(posts_de_entrada())
    snap_b = await criar_snapshot(SEGUNDO)
    contexto_segundo = await contexto_candidato(SEGUNDO, snap_b)
    segunda_entrada = await rodar_ciclo(
        [rec_base(SEGUNDO, snap_b, operational_selection=contexto_segundo)],
        env=selector_env)
    check("ultimo_slot_aprovado_bloqueia_a_proxima_sem_post",
          primeira_entrada == 1 and posts_primeira == 1
          and segunda_entrada == 0 and posts_de_entrada() == []
          # O motivo EXATO vem do caller real (log do p03-intent); o `_record_skip`
          # recebe só a classe BLOCKED_CAPACITY.
          and any("CANARY_ORDER_LIMIT" in linha for linha in LOGS)
          and ("entry-intent", "BLOCKED_CAPACITY") in SKIPS,
          f"{primeira_entrada}/{segunda_entrada} {SKIPS} {LOGS[-3:]}")

    # ── 5d. Posição MANUAL preservada (nunca fechada pelo ciclo) ──────────
    MANUAL = "ETHUSDT"
    EXCHANGE["positions"] = [{"symbol": bss.to_binance(MANUAL), "positionAmt": "2",
                              "entryPrice": "2000", "markPrice": "2000",
                              "unRealizedProfit": "0", "leverage": "3",
                              "updateTime": 1}]
    antes_manual = [dict(p) for p in EXCHANGE["positions"]]
    await limpar_intents()
    snap_man = await criar_snapshot(SYMBOL)
    contexto_man = await contexto_candidato(SYMBOL, snap_man)
    await rodar_ciclo([rec_base(SYMBOL, snap_man,
                                operational_selection=contexto_man)],
                      env=selector_env)
    reduz_manual = [e for e in ENVIADOS if bss.to_binance(MANUAL) in
                    str(e["params"].get("symbol") or "")
                    and (e["params"].get("reduceOnly")
                         or e["params"].get("closePosition"))]
    check("posicao_manual_nao_e_tocada_pelo_ciclo",
          EXCHANGE["positions"] == antes_manual and reduz_manual == [],
          f"{EXCHANGE['positions']} {reduz_manual}")
    EXCHANGE["positions"] = []

    # ── 5e. Dois consumidores da MESMA decisão: nunca duplicam a entrada ──
    # Controle POSITIVO (sequencial e determinístico): o primeiro consumidor
    # envia UM POST; o segundo, com a MESMA decisão, não reenvia — o despacho
    # já está registrado. Depois, os dois concorrentes de verdade: o contador é
    # comum e no máximo um POST pode existir.
    await limpar_intents()
    snap_dois = await criar_snapshot(SYMBOL)
    contexto_dois = await contexto_candidato(SYMBOL, snap_dois)
    rec_dois = rec_base(SYMBOL, snap_dois, operational_selection=contexto_dois)
    consumidor_a = await rodar_ciclo([dict(rec_dois)], env=selector_env)
    posts_a = len(posts_de_entrada())
    consumidor_b = await rodar_ciclo([dict(rec_dois)], env=selector_env)
    posts_b = len(posts_de_entrada())
    await limpar_intents()
    snap_par = await criar_snapshot(SYMBOL)
    contexto_par = await contexto_candidato(SYMBOL, snap_par)
    rec_par = rec_base(SYMBOL, snap_par, operational_selection=contexto_par)
    ENVIADOS.clear()
    ASSINADOS.clear()
    SKIPS.clear()
    LOGS.clear()
    par_a, par_b = await asyncio.gather(
        rodar_ciclo_sem_limpar([dict(rec_par)], env=selector_env),
        rodar_ciclo_sem_limpar([dict(rec_par)], env=selector_env))
    posts_par = len(posts_de_entrada())
    async with db.get_session() as session:
        intents_par = (await session.execute(
            select(func.count(EntryIntent.intent_key)))).scalar_one()
    check("dois_consumidores_nunca_duplicam_a_entrada",
          consumidor_a == 1 and posts_a == 1
          and consumidor_b == 0 and posts_b == 0
          and posts_par <= 1 and (par_a + par_b) == posts_par
          and int(intents_par) == 1,
          f"seq={consumidor_a}/{consumidor_b} posts={posts_a}/{posts_b} "
          f"par={par_a}/{par_b} posts_par={posts_par} intents={intents_par}")

    # ── 5f. Restart: autoridade e intenção vêm do BANCO, não da memória ───
    await db._engine.dispose()
    g._PENDING_FENCES.clear()
    g._FAILED_FENCES.clear()
    with patch.dict(os.environ, selector_env):
        visao_pos_restart = await adapter.load_operational_view(db.get_session)
    async with db.get_session() as session:
        sobreviventes = (await session.execute(
            select(func.count(EntryIntent.intent_key)))).scalar_one()
    check("restart_recarrega_autoridade_e_intencao_do_banco",
          adapter.candidate_mode_active(visao_pos_restart)
          and visao_pos_restart["approval"]["approval_id"] == canary_slot["approval_id"]
          and int(sobreviventes) == 1,
          f"{str(visao_pos_restart)[:160]} intents={sobreviventes}")

    # ── 5g. Expiração e drift da aprovação: zero POST ─────────────────────
    await limpar_intents()
    snap_exp = await criar_snapshot(SYMBOL)
    contexto_exp = await contexto_candidato(SYMBOL, snap_exp)
    expirado = copy.deepcopy(contexto_exp)
    expirado["authority"]["approval"]["limits"]["expires_at_ms"] = now - 1
    pos_expiracao = await rodar_ciclo(
        [rec_base(SYMBOL, snap_exp, operational_selection=expirado)],
        env=selector_env)
    check("aprovacao_expirada_ou_adulterada_nao_envia_entrada",
          pos_expiracao == 0 and posts_de_entrada() == [], f"{pos_expiracao} {SKIPS}")
    await limpar_intents()
    snap_drift = await criar_snapshot(SYMBOL)
    driftado = copy.deepcopy(contexto_exp)
    driftado["authority"]["bundle"]["bundle_hash"] = "d" * 64
    pos_drift = await rodar_ciclo(
        [rec_base(SYMBOL, snap_drift, operational_selection=driftado)],
        env=selector_env)
    check("drift_de_bundle_nao_envia_entrada",
          pos_drift == 0 and posts_de_entrada() == [], f"{pos_drift} {SKIPS}")

    # ── 5k. Ordem GLOBAL do lock: duas conexões REAIS com barreira ────────
    # Ponto REAL: a transação financeira (advisory 917283) segura a LINHA do
    # singleton de governança (o mesmo `FOR UPDATE` que `_state` toma). O
    # escritor de governança (advisory 505202609 → mesma linha) precisa ESPERAR
    # e concluir — se o leitor de autoridade tomasse 505202609 dentro da
    # transação financeira, isto viraria deadlock em vez de espera.
    import time as _tempo
    barreira_financeira = asyncio.Event()
    liberar_financeira = asyncio.Event()
    ordem_observada: list = []

    async def transacao_financeira():
        async with db.get_session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                  {"k": intents.RISK_LOCK_KEY})
            await session.execute(text(
                "SELECT 1 FROM policy_simulation_state WHERE state_key = :k "
                "FOR UPDATE"), {"k": g.STATE_KEY})
            ordem_observada.append("financeira_com_linha")
            barreira_financeira.set()
            await asyncio.wait_for(liberar_financeira.wait(), timeout=10)
            await session.rollback()
            ordem_observada.append("financeira_liberou")

    async def escritor_de_governanca():
        await asyncio.wait_for(barreira_financeira.wait(), timeout=10)
        inicio = _tempo.monotonic()
        tarefa = asyncio.ensure_future(aprovar("SHADOW", etiqueta="-ordem"))
        await asyncio.sleep(0.25)
        esperando = not tarefa.done()
        liberar_financeira.set()
        return await asyncio.wait_for(tarefa, timeout=15), esperando, \
            _tempo.monotonic() - inicio

    _, (aprov_ordem, esperou_na_linha, espera_s) = await asyncio.gather(
        transacao_financeira(), escritor_de_governanca())
    check("ordem_global_do_lock_serializa_sem_deadlock",
          esperou_na_linha is True and aprov_ordem.get("ok") is True
          and espera_s >= 0.2
          and ordem_observada == ["financeira_com_linha", "financeira_liberou"],
          f"esperou={esperou_na_linha} {espera_s:.2f}s {aprov_ordem} {ordem_observada}")

    # ── 5l. Concorrência repetida 2× DEPOIS da última mudança ─────────────
    repeticoes = []
    for rodada in range(2):
        estado_rep = await g.get_status(db.get_session)
        aprovado_rep, revogado_rep = await asyncio.gather(
            aprovar("SHADOW", etiqueta=f"-rep{rodada}"),
            g.revoke_approval(db.get_session, exp_id, aprov_ordem["approval_id"],
                              estado_rep["generation"], "ADMIN_API_TOKEN"))
        efetivos_rep = [r for r in (aprovado_rep, revogado_rep)
                        if r.get("ok") is True and not r.get("idempotent")]
        repeticoes.append(len(efetivos_rep))
    async with db.get_session() as session:
        hist_rep = ((await session.execute(select(S.payload).where(
            S.state_key == g.STATE_KEY))).scalar_one() or {}).get("history") or []
    geracoes = [h["generation"] for h in hist_rep]
    check("concorrencia_repetida_2x_mantem_um_efeito_e_historico_ordenado",
          all(n >= 1 for n in repeticoes) and len(repeticoes) == 2
          and geracoes == sorted(geracoes) and len(geracoes) == len(set(geracoes)),
          f"{repeticoes} geracoes={geracoes[-6:]}")

    # ── 6. Rollback preserva posição/proteção e bloqueia nova candidata ───
    EXCHANGE["positions"] = [{"symbol": bss.to_binance(SYMBOL), "positionAmt": "1",
                              "entryPrice": "100", "markPrice": "100",
                              "unRealizedProfit": "0", "leverage": "5",
                              "updateTime": 1}]
    estado = await g.get_status(db.get_session)
    desfeito = await g.rollback(db.get_session, estado["generation"],
                                "ADMIN_API_TOKEN", "ROLLBACK_TESTE",
                                block_entries=True)
    check("rollback_preserva_posicoes_e_protecao",
          desfeito.get("ok") is True
          and desfeito.get("preserved_positions_and_protection") is True
          and desfeito.get("selector_env_unchanged") is True, str(desfeito))
    await limpar_intents()
    snap_roll = await criar_snapshot(SYMBOL)
    pos_rollback = await rodar_ciclo(
        [rec_base(SYMBOL, snap_roll, operational_selection=contexto2)],
        env=selector_env)
    check("apos_rollback_nenhuma_entrada_candidata",
          pos_rollback == 0 and posts_de_entrada() == [], f"{pos_rollback} {SKIPS}")
    async with db.get_session() as session:
        trades = int((await session.execute(text(
            "SELECT count(*) FROM real_trades"))).scalar() or 0)
    check("rollback_nao_cria_nem_altera_trade", trades == 0, str(trades))
    EXCHANGE["positions"] = []

    # ── 7. Status é leitura: não refaz replay/fitting/aprovação ───────────
    with patch.object(prospective, "summarize_prospective",
                      side_effect=AssertionError("status não resume coorte")), \
            patch.object(g, "register_approval",
                         side_effect=AssertionError("status não aprova")):
        status = await g.get_status(db.get_session)
    check("status_nao_refaz_trabalho_nem_aprova",
          status.get("ok") is True and status.get("no_orders_executed") is True
          and status.get("block_entries") is True, str(status))

    # Nenhuma conexão TCP foi tentada; as resoluções DNS que apareceram vêm de
    # thread de pool da borda pública de mercado (OKX) e TODAS foram bloqueadas
    # e contabilizadas — nenhum byte saiu, e a lógica provada não depende delas.
    hosts = sorted({str(item[0]) for item in DNS_TENTADO})
    check("nenhuma_conexao_tcp_tentada", TCP_TENTADO == [], str(TCP_TENTADO[:3]))
    check("dns_bloqueado_e_contabilizado",
          all("okx" in host for host in hosts),
          f"tentativas={len(DNS_TENTADO)} hosts={hosts}")
    print(f"  · DNS bloqueado/contado: {len(DNS_TENTADO)} tentativa(s) {hosts}")
    for item in transport_patches + base_patches:
        item.stop()
    print(f"LOTE03_GOVERNED_PG_OK: {len(CHECKS)} verificações")
    await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
