"""PRE_SELECTION prospectivo: captura e resolver oficiais R09, sem executor.

Decisões são congeladas ANTES dos preços futuros; resultados usam janelas já
coletadas pelo resolver e custos BPS declarados. OFFLINE e TEST_ONLY não são
evidência prospectiva real. Nenhum loop, rede, credencial ou commit próprio.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Mapping

from services.offline_replay_service import CLOSED_STATUSES as _CLOSED_STATUSES

VERSION = "R13_PRESELECTION_PROSPECTIVE_V1"
KEY = "r13_shadow"
REAL = "REAL_PROSPECTIVE"
TEST = "TEST_ONLY"
PENDING = ("PENDING", "GAP_PENDING")
MAX_ROWS = 256
MAX_EVIDENCE_ROWS = 5000
TERMINAL = _CLOSED_STATUSES + ("NOT_FILLED",)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def integer(value):
    return value if type(value) is int and value >= 0 else None


def ms(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return int(value.timestamp() * 1000)


def _now():
    return ms(datetime.now(timezone.utc))


def _annotation(config):
    payload = config.get("r09_pre_selection") if isinstance(config, Mapping) else None
    ann = payload.get(KEY) if isinstance(payload, Mapping) else None
    return ann if isinstance(ann, Mapping) else None


def verify_annotation(ann):
    if not isinstance(ann, Mapping) or ann.get("version") != VERSION:
        return False
    frozen = ann.get("frozen")
    try:
        return isinstance(frozen, Mapping) and digest(frozen) == ann.get("annotation_hash")
    except (TypeError, ValueError):
        return False


def frozen_shadow_context(exp, *, generation, approval_id, started_at_ms):
    """Somente identidade/contexto; não lê outcome nem concede aprovação."""
    from services import strategy_evidence_service as ev
    from services import research_manifest_service as rm
    binding = ev._frozen_study_of(exp)
    if not binding["ok"]:
        return None
    contract = binding["contract"]
    if contract.get("comparison_scope") != "SELECTION_ONLY":
        return None
    verified = rm.verify_manifest(contract.get("research_manifest"))
    if not verified.get("ok") or integer(generation) is None or not approval_id \
            or integer(started_at_ms) is None:
        return None
    if digest(exp.candidate_config) != exp.candidate_hash:
        # O catálogo usa JSON canônico ASCII; fingerprint não é herdado do caller.
        return None
    return {"experiment_id": exp.id, "experiment_key": exp.experiment_key,
            "candidate_hash": exp.candidate_hash, "champion_hash": exp.champion_hash,
            "generation": generation, "approval_id": approval_id,
            "started_at_ms": started_at_ms, "manifest": verified["manifest"],
            "manifest_hash": verified["manifest"]["manifest_hash"],
            "contract_hash": binding["contract_hash"]}


async def get_active_preselection_context(session):
    from sqlalchemy import select
    from models.strategy_experiment import StrategyExperiment as E
    from services import strategy_evidence_service as ev
    if not (ev.P05_ANALYTICS_ENABLED and ev.P05_CHALLENGER_SHADOW_ENABLED):
        return None
    from services import operational_governance_service as governance
    rows = (await session.execute(select(E).where(E.status == ev.STATUS_SHADOW).limit(2))).scalars().all()
    if len(rows) != 1 or not ev.is_pre_selection_experiment(rows[0]):
        return None
    exp = rows[0]
    start = (exp.shadow_metrics or {}).get(KEY)
    if not isinstance(start, Mapping):
        return None
    authority = await governance.assert_authority_in_session(session, exp_id=exp.id,
        purpose="SHADOW", approval_id=start.get("approval_id"),
        expected_generation=start.get("generation"))
    if not authority.get("ok"):
        return None
    context = frozen_shadow_context(exp, generation=authority["generation"],
        approval_id=start.get("approval_id"), started_at_ms=start.get("started_at_ms"))
    if context is None:
        return None
    from services import research_manifest_service as rm
    return context if rm.authorized_comparison(context["manifest"]).get("real_study_allowed") is True else None


async def shadow_authority(session_factory):
    """Autoridade SHADOW do challenger ativo — leitura curta, sem escrita.

    É ela que habilita a decisão candidata OBSERVACIONAL no scanner enquanto o
    seletor operacional continua LEGACY/OFF. Propósito SHADOW nunca autoriza
    reserva, alavancagem ou POST (a fronteira é imposta na governança).
    """
    from services import operational_governance_service as governance
    try:
        async with session_factory() as session:
            context = await get_active_preselection_context(session)
            if context is None:
                await session.rollback()
                return None
            authority = await governance.assert_authority_in_session(
                session, exp_id=context["experiment_id"], purpose="SHADOW",
                approval_id=context["approval_id"],
                expected_generation=context["generation"])
            await session.rollback()
        return authority if authority.get("ok") is True else None
    except Exception:  # noqa: BLE001 — pesquisa nunca derruba o caminho do bot
        return None


def build_preselection_annotation(row, context):
    """Mesma oportunidade e decisão final do champion; candidato não vê outcome."""
    from services import research_manifest_service as rm
    from services import research_selection_service as selection
    cfg = row.get("frozen_config") if isinstance(row, Mapping) else None
    payload = cfg.get("r09_pre_selection") if isinstance(cfg, Mapping) else None
    if not isinstance(payload, Mapping) or not isinstance(context, Mapping):
        return None
    if payload.get(KEY) is not None:
        return copy.deepcopy(payload[KEY])  # primeira anotação nunca é substituída
    manifest = context["manifest"]
    captured = ms(row.get("observed_at"))
    decision = integer(payload.get("decision_ts_ms"))
    if captured is None or decision is None or captured < context["started_at_ms"] or decision > captured:
        return None
    setup = payload.get("setup")
    if not isinstance(setup, Mapping) or payload.get("identity") not in (None, row.get("opportunity_key")):
        return None
    raw = {**setup, "opportunity_key": row["opportunity_key"], "decision_ts_ms": decision,
           "observed_outcome": payload.get("outcome"),
           "observed_decision_scope": payload.get("observed_decision_scope"),
           "score_trace": row.get("score_trace"), "features": payload.get("features"),
           "champion_config": payload.get("config")}
    choices = {side: selection.decide_row(raw, rule=manifest[side]["selection_rule"],
        side_label=side, config=manifest[side]["score_config"], baseline=manifest[side])
        for side in ("baseline", "candidate")}
    feature_evidence = payload.get("feature_evidence")
    if (not isinstance(feature_evidence, Mapping) or feature_evidence.get("quality") != "FRESH"
            or integer(feature_evidence.get("observed_at_ms")) is None
            or feature_evidence["observed_at_ms"] > captured
            or integer(feature_evidence.get("candle_close_ms")) is None
            or feature_evidence["candle_close_ms"] > decision):
        choices["candidate"] = {"state": "UNKNOWN", "reason_code": "POINT_IN_TIME_FEATURES_NOT_CONFIRMED"}
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    quality = (REAL if source.get("decision_source") == "server_scan"
               and rm.authorized_comparison(manifest).get("real_study_allowed") is True else TEST)
    frozen = {k: context[k] for k in ("experiment_id", "experiment_key", "candidate_hash",
        "champion_hash", "generation", "approval_id", "started_at_ms", "manifest_hash", "contract_hash")}
    frozen.update(opportunity_key=row["opportunity_key"], captured_at_ms=captured,
        decision_ts_ms=decision, source_mode=quality, source_schema=payload.get("schema_version"),
        setup=copy.deepcopy(dict(setup)), decisions=choices,
        playbook=manifest["candidate"]["selection_rule"].get("playbook"),
        replay_config=copy.deepcopy(manifest["candidate"]["management_config"]),
        costs_config=copy.deepcopy(manifest["costs"]["config"]),
        # PERSISTIDO antes de qualquer preço futuro: o escopo da decisão
        # observada (do champion) e a decisão candidata OBSERVACIONAL do mesmo
        # instante/estágio, mais a identidade de fórmula/config com que a
        # recomputação offline se reconcilia. Sem isso a comparação de
        # fidelidade não tem com o que comparar — e ausência não é 0%.
        observed_decision_scope=payload.get("observed_decision_scope"),
        shadow_decision=(copy.deepcopy(dict(payload["shadow_decision"]))
                         if isinstance(payload.get("shadow_decision"), Mapping) else None),
        candidate_score_config_hash=manifest["candidate"].get("score_config_hash"),
        candidate_min_score=manifest["candidate"]["selection_rule"].get("min_score"))
    return {"version": VERSION, "frozen": frozen, "annotation_hash": digest(frozen),
            "generation": 0, "candles": [], "resolution": {"status": "PENDING", "net_r": None}}


async def annotate_pending_batch(session, opportunities):
    """Chamado no _admit oficial, somente para oportunidades NOVAS."""
    context = await get_active_preselection_context(session)
    if context is None:
        return 0
    count = 0
    rows = opportunities.values() if isinstance(opportunities, Mapping) else opportunities
    for row in rows:
        if row.get("scope") != "PRE_SELECTION":
            continue
        ann = build_preselection_annotation(row, context)
        if ann is not None:
            cfg = copy.deepcopy(row["frozen_config"])
            cfg["r09_pre_selection"][KEY] = ann
            row["frozen_config"] = cfg
            count += 1
    return count


def resolve_annotation(row, shared):
    """Replay da janela já coletada; mantém custos e contrato congelados."""
    from services import offline_replay_service as replay
    cfg = copy.deepcopy(row.frozen_config)
    ann = _annotation(cfg)
    if not verify_annotation(ann) or ann["resolution"].get("status") not in PENDING:
        return None
    frozen, as_of = ann["frozen"], ms(shared.get("as_of"))
    if as_of is None or as_of < frozen["captured_at_ms"]:
        return None
    try:
        management = frozen["replay_config"]
        config = replay.ReplayConfig(**{k: v for k, v in management.items() if k != "config_hash"})
        costs = replay.CostConfig(**{k: frozen["costs_config"][k] for k in
            ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")})
        setup = frozen["setup"]
        opp = replay.Opportunity(opportunity_id=frozen["opportunity_key"], symbol=setup["symbol"],
            direction=setup["side"], decision_ts_ms=frozen["decision_ts_ms"], entry=setup["entry"],
            stop_loss=setup["stop_loss"], tp1=setup["tp1"], tp2=setup["tp2"], atr=setup.get("atr"))
        first = ((opp.decision_ts_ms + config.bar_ms - 1) // config.bar_ms) * config.bar_ms
        merged = {c["timestamp"]: c for c in ann["candles"]}
        for candle in shared.get("candles") or ():
            stamp = integer(candle.get("timestamp")) if isinstance(candle, Mapping) else None
            if stamp is not None and first <= stamp and stamp + config.bar_ms <= as_of:
                if all(number(candle.get(k)) is not None for k in ("open", "high", "low", "close", "volume")):
                    merged.setdefault(stamp, {"timestamp": stamp, **{k: candle[k] for k in
                        ("open", "high", "low", "close", "volume")}})
        candles = [merged[k] for k in sorted(merged)][:config.max_bars]
        bars = tuple(replay.Candle(timestamp_ms=c["timestamp"], **{k: c[k] for k in
            ("open", "high", "low", "close", "volume")}) for c in candles)
        result = replay.replay_opportunity(opp, bars, config, costs)
        if result.get("status") in TERMINAL and result.get("filled") is True \
                and (number(result.get("net_r")) is None or integer(result.get("result_available_ts_ms")) is None):
            raise ValueError("resolved economics missing")
        status = result.get("status")
        coverage = status if status in TERMINAL else ("GAP_PENDING" if status == "MISSING_OR_UNORDERED_BARS" else "PENDING")
        ann = {**ann, "generation": ann["generation"] + 1, "candles": candles,
               "resolution": {**result, "status": coverage, "replay_status": status,
                    "observed_at_ms": as_of, "cost_source": "DECLARED_BPS_SCENARIO",
                    "source": "OFFICIAL_R09_RESOLVER_FORWARD_WINDOW"}}
    except (KeyError, TypeError, ValueError, OverflowError):
        ann = {**ann, "generation": ann["generation"] + 1,
               "resolution": {"status": "INVALID", "net_r": None,
                              "reason_code": "PROSPECTIVE_REPLAY_INVALID", "observed_at_ms": as_of}}
    cfg["r09_pre_selection"][KEY] = ann
    return cfg


def _matching_filters(exp, start):
    from models.decision_observation import DecisionObservation as O
    ann = O.frozen_config["r09_pre_selection"][KEY]
    f = ann["frozen"]
    return (O.scope == "PRE_SELECTION", O.first_decision_observed_at >= datetime.fromtimestamp(start["started_at_ms"] / 1000, timezone.utc),
            f["experiment_id"].astext == str(exp.id), f["candidate_hash"].astext == exp.candidate_hash,
            f["champion_hash"].astext == exp.champion_hash, f["generation"].astext == str(start["generation"]),
            f["approval_id"].astext == start["approval_id"], f["source_mode"].astext == REAL)


async def resolve_pending(session, windows):
    """Mesmo resolver/transação R09; CAS preserva primeira identidade."""
    from sqlalchemy import select, update
    from models.decision_observation import DecisionObservation as O
    from services import strategy_evidence_service as ev
    if not (ev.P05_ANALYTICS_ENABLED and ev.P05_CHALLENGER_SHADOW_ENABLED):
        return set()
    ann = O.frozen_config["r09_pre_selection"][KEY]
    pending = (O.scope == "PRE_SELECTION", ann["version"].astext == VERSION,
               ann["resolution"]["status"].astext.in_(PENDING))
    if windows:
        rows = (await session.execute(select(O).where(*pending, O.symbol.in_(list(windows)))
            .order_by(O.first_decision_observed_at).limit(MAX_ROWS))).scalars().all()
        for row in rows:
            changed = resolve_annotation(row, windows[row.symbol])
            if changed is not None:
                await session.execute(update(O).where(O.opportunity_key == row.opportunity_key,
                    O.frozen_config == row.frozen_config).values(frozen_config=changed))
    return set((await session.execute(select(O.symbol).where(*pending).distinct())).scalars().all())


#: Escopo gravado pelo scanner quando a SELEÇÃO CANDIDATA rodou em runtime.
RUNTIME_CANDIDATE_SCOPE = "CANDIDATE_SCANNER_SELECTION"
#: Escopo da decisão candidata OBSERVACIONAL (seletor operacional LEGACY/OFF):
#: é ela que quebra a circularidade — fidelidade medida ANTES de promover.
SHADOW_CANDIDATE_SCOPE = "CANDIDATE_SHADOW"
#: Bloqueio ESPECÍFICO (dependência de DADOS, não código por terminar): sem
#: observação da candidata no runtime não existe o que comparar.
FIDELITY_GAP_REASON = "FIDELITY_REQUIRES_CANDIDATE_RUNTIME_OBSERVATIONS"
#: Estados determinados: recusa por score CONHECIDO é discordância legítima;
#: feature/modelo/calibração ausente é UNKNOWN e não entra no denominador.
DETERMINATE = ("SELECTED", "REJECTED")


def _shadow_reconciled(shadow, frozen):
    """Mesma oportunidade, estágio e IDENTIDADE DE REGRA dos dois lados.

    Devolve `None` quando reconcilia; senão o motivo da incomparabilidade. Nada
    aqui "aproxima": hash de fórmula/config diferente é incomparável, não 0%.
    """
    autoridade = shadow.get("authority") if isinstance(shadow.get("authority"), Mapping) else {}
    if autoridade.get("purpose") != "SHADOW":
        return "OBSERVED_PURPOSE_NOT_SHADOW"
    setup = frozen.get("setup") if isinstance(frozen.get("setup"), Mapping) else {}
    if shadow.get("timeframe") != setup.get("timeframe"):
        return "STAGE_TIMEFRAME_MISMATCH"
    for campo_obs, campo_frozen in (("experiment_id", "experiment_id"),
                                    ("generation", "generation"),
                                    ("approval_id", "approval_id"),
                                    ("manifest_hash", "manifest_hash"),
                                    ("score_config_hash", "candidate_score_config_hash")):
        esperado = frozen.get(campo_frozen)
        if esperado is None or autoridade.get(campo_obs) != esperado:
            return "CONFIG_NOT_RECONCILED"
    if number(shadow.get("min_score")) != number(frozen.get("candidate_min_score")):
        return "DENOMINATOR_NOT_RECONCILED"
    return None


def fidelity_measure(valid):
    """Decisão candidata OBSERVADA × recomputação verificável do contrato.

    A comparação é sobre a MESMA oportunidade, a MESMA população e o MESMO
    estágio (o TF daquela linha, antes da escolha do champion), com os hashes de
    fórmula/config/aprovação reconciliados. Publica comparáveis, divergentes,
    UNKNOWN, cobertura e motivos. Sem par comparável, a fidelidade é `None` com
    lacuna declarada — nunca 0%.
    """
    comparaveis = divergencias = indeterminados = observados = 0
    motivos = {}
    def nota(motivo):
        motivos[motivo] = motivos.get(motivo, 0) + 1
    for ann in valid:
        frozen = ann["frozen"]
        offline = frozen["decisions"]["candidate"]
        shadow = frozen.get("shadow_decision")
        escopo = frozen.get("observed_decision_scope")
        if isinstance(shadow, Mapping) and shadow.get("scope") == SHADOW_CANDIDATE_SCOPE:
            observados += 1
            problema = _shadow_reconciled(shadow, frozen)
            if problema is not None:
                indeterminados += 1
                nota(problema)
                continue
            observado_estado = shadow.get("state")
        elif escopo == RUNTIME_CANDIDATE_SCOPE:
            # Candidata decidiu no runtime OPERACIONAL (pós-promoção): o desfecho
            # observado da própria linha é a decisão dela.
            observados += 1
            observado_estado = ("SELECTED" if str(frozen.get("observed_outcome") or "").upper()
                                == "ACCEPTED" else "REJECTED")
        else:
            nota("NO_OBSERVED_CANDIDATE_DECISION")
            continue
        offline_estado = offline.get("state")
        if observado_estado not in DETERMINATE:
            indeterminados += 1
            nota(f"OBSERVED_{observado_estado or 'MISSING'}")
            continue
        if offline_estado not in DETERMINATE:
            indeterminados += 1
            nota(f"RECOMPUTED_{offline_estado or 'MISSING'}")
            continue
        comparaveis += 1
        if observado_estado != offline_estado:
            divergencias += 1
    total = len(valid)
    cobertura = round(comparaveis * 100.0 / total, 6) if total else None
    return {"fidelity_discrepancy_pct": (round(divergencias * 100.0 / comparaveis, 6)
                                         if comparaveis else None),
            "fidelity_comparable": comparaveis,
            "fidelity_divergences": divergencias,
            "fidelity_unknown": indeterminados,
            "fidelity_observed": observados,
            "fidelity_denominator": total,
            "fidelity_coverage_pct": cobertura,
            "fidelity_reasons": motivos,
            "fidelity_gap_reason": None if comparaveis else FIDELITY_GAP_REASON}


#: Proteção do Shadow: SIMULADA pelo replay oficial. Nunca SL/TP real na conta.
PROTECTION_SCOPE = "SHADOW_SIMULATED"
#: Cobertura mínima de observação de proteção para que "zero falhas" signifique
#: algo. Abaixo dela o campo é `None` com motivo — não existe zero por omissão.
MIN_PROTECTION_COVERAGE_PCT = 90.0
#: Decisão HUMANA específica: se a primeira promoção exigir SL REAL, o Shadow
#: não pode provar isso. O gate continua NO_GO e a pergunta fica explícita.
PROTECTION_DECISION_REQUIRED = "BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED"
#: A pergunta que precisa de resposta HUMANA, não de flag/ENV: "proteção
#: SIMULADA do Shadow satisfaz o requisito de proteção da PRIMEIRA promoção, ou
#: ela exige SL REAL colocado na conta?". Enquanto a resposta não existir, esta
#: constante fica `False`, o gate permanece NO_GO e nada é promovido. Mudá-la é
#: uma alteração de código revisada — nunca um ligar/desligar em runtime.
PROTECTION_SCOPE_ACCEPTED_FOR_PROMOTION = False


def _protection_reconciled(protection, frozen, resolution):
    """A trilha é da MESMA oportunidade, gestão e resolução — ou não vale.

    Devolve `None` quando reconcilia; senão o motivo. Nenhum payload que apenas
    *se diz* protegido passa: fonte, versão, produtor e hashes são conferidos.
    """
    from services import offline_replay_service as replay
    if not isinstance(protection, Mapping):
        return "PROTECTION_SOURCE_MISSING"
    if protection.get("source") != replay.PROTECTION_SOURCE \
            or protection.get("version") != replay.PROTECTION_VERSION \
            or protection.get("producer") != replay.PROTECTION_PRODUCER:
        return "PROTECTION_SOURCE_MISSING"
    if protection.get("proves_real_sl") is not False:
        return "PROTECTION_SCOPE_INVALID"
    gestao = frozen.get("replay_config") if isinstance(frozen.get("replay_config"), Mapping) else {}
    custos = frozen.get("costs_config") if isinstance(frozen.get("costs_config"), Mapping) else {}
    if protection.get("config_hash") != gestao.get("config_hash") \
            or protection.get("cost_config_hash") != custos.get("config_hash"):
        return "PROTECTION_CONFIG_NOT_RECONCILED"
    if protection.get("opportunity_id") != frozen.get("opportunity_key"):
        return "PROTECTION_OPPORTUNITY_MISMATCH"
    identidade = protection.get("resolution_identity")
    if not isinstance(identidade, Mapping) \
            or identidade.get("status") != resolution.get("replay_status"):
        return "PROTECTION_RESOLUTION_MISMATCH"
    observacoes = protection.get("observations")
    if not isinstance(observacoes, list) or not observacoes \
            or integer(protection.get("obligation_opened_ts_ms")) is None:
        return "PROTECTION_TRACE_MISSING"
    if not _protection_trace_valid(protection, frozen, resolution):
        return "PROTECTION_TRACE_INVALID"
    return None


def _protection_trace_valid(protection, frozen, resolution):
    """Concilia o conteúdo observado e os resumos, não só os rótulos/hashes.

    Não recalcula economia, não consulta preços e não certifica SL real.
    Registro contraditório/incompleto perde a cobertura; jamais atesta zero.
    """
    from services import offline_replay_service as replay
    setup = frozen.get("setup") if isinstance(frozen.get("setup"), Mapping) else {}
    direction = setup.get("side")
    entry, initial_stop = number(setup.get("entry")), number(setup.get("stop_loss"))
    bar_ms = integer((frozen.get("replay_config") or {}).get("bar_ms"))
    if direction not in ("long", "short") or entry is None or entry <= 0 \
            or initial_stop is None or initial_stop <= 0 or not bar_ms \
            or protection.get("direction") != direction \
            or number(protection.get("entry_reference")) != entry \
            or number(protection.get("initial_stop")) != initial_stop \
            or protection.get("observations_truncated") is not False:
        return False
    identity = protection["resolution_identity"]
    exits = resolution.get("exits")
    if not isinstance(exits, list) or protection.get("status") != identity.get("status") \
            or integer(identity.get("exits")) != len(exits) \
            or integer(identity.get("bars_held")) != integer(resolution.get("bars_held")):
        return False
    for key in ("exit_ts_ms", "result_available_ts_ms"):
        if identity.get(key) != resolution.get(key):
            return False
        if identity.get(key) is not None and integer(identity[key]) is None:
            return False
    observations = protection["observations"]
    opened = protection["obligation_opened_ts_ms"]
    previous_ts, previous_qty = opened, 1.0
    expected_failures = set()
    sign = 1 if direction == "long" else -1
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            return False
        stage, stamp = observation.get("stage"), integer(observation.get("timestamp_ms"))
        qty, stop = number(observation.get("remaining_qty")), number(observation.get("active_stop"))
        if stage not in replay.PROTECTION_STAGES or stamp is None or stamp < previous_ts \
                or integer(observation.get("available_ts_ms")) != stamp + bar_ms \
                or qty is None or not 0 <= qty <= previous_qty + 1e-12 \
                or type(observation.get("tp1_hit")) is not bool:
            return False
        exposure = number(observation.get("exposure_reference"))
        if exposure is None or not math.isclose(exposure, qty * entry, rel_tol=1e-9, abs_tol=1e-9) \
                or observation.get("obligation") is not (qty > 0):
            return False
        finite = stop is not None and stop > 0
        geometry = ("UNKNOWN" if not finite else "LOSS_SIDE" if sign * (stop - entry) < 0
                    else "BREAK_EVEN" if stop == entry else "PROFIT_LOCK")
        valid = finite and (geometry == "LOSS_SIDE" if not observation["tp1_hit"]
                            else geometry in ("LOSS_SIDE", "BREAK_EVEN", "PROFIT_LOCK"))
        if observation.get("stop_finite") is not finite \
                or observation.get("geometry") != geometry \
                or observation.get("geometry_valid") is not valid:
            return False
        if index == 0 and (stage != "ENTRY_FILL" or stamp != opened or qty != 1.0
                           or stop != initial_stop or observation["tp1_hit"] is not False):
            return False
        if qty > 0 and not valid:
            code = replay.PROTECTION_STOP_NOT_FINITE if not finite else replay.PROTECTION_GEOMETRY_INVALID
            expected_failures.add((code, stage, stamp))
        previous_ts, previous_qty = stamp, qty
    remaining = number(protection.get("remaining_at_end"))
    if remaining is None or not math.isclose(remaining, previous_qty, rel_tol=0, abs_tol=1e-12) \
            or protection.get("obligation_open_at_end") is not (remaining > 0):
        return False
    if remaining > 0:
        if protection.get("obligation_closed_ts_ms") is not None:
            return False
    elif protection.get("obligation_closed_ts_ms") != resolution.get("exit_ts_ms"):
        return False
    failures = protection.get("failures")
    if not isinstance(failures, list) or integer(protection.get("pending_failures")) is None:
        return False
    pending, recorded = 0, set()
    for failure in failures:
        if not isinstance(failure, Mapping) or type(failure.get("resolved")) is not bool \
                or failure.get("code") not in (replay.PROTECTION_STOP_NOT_FINITE,
                    replay.PROTECTION_GEOMETRY_INVALID, replay.PROTECTION_OBLIGATION_UNRESOLVED) \
                or failure.get("stage") not in replay.PROTECTION_STAGES \
                or integer(failure.get("timestamp_ms")) is None:
            return False
        if failure["resolved"] is False:
            pending += 1
            recorded.add((failure["code"], failure["stage"], failure["timestamp_ms"]))
    if protection["pending_failures"] != pending or not expected_failures.issubset(recorded):
        return False
    if remaining > 0 and not any(code == replay.PROTECTION_OBLIGATION_UNRESOLVED
                                for code, _, _ in recorded):
        return False
    return True


def protection_measure(annotations):
    """Proteção SIMULADA observada na coorte — jamais SL real, jamais zero vago.

    Conta a obrigação de proteção do fill virtual até a saída/fim de janela, com
    denominador auditável (quantas tinham obrigação aplicável) e cobertura
    exigida. Sem fonte/trilha/obrigação o resultado é `None` com motivo; uma
    saída lucrativa SEM prova de proteção continua sendo falha pendente.
    """
    aplicaveis = observadas = pendentes = 0
    motivos = {}
    def nota(motivo):
        motivos[motivo] = motivos.get(motivo, 0) + 1
    for ann in annotations:
        if not verify_annotation(ann):
            nota("ANNOTATION_INVALID")
            continue
        resolucao = ann.get("resolution") if isinstance(ann.get("resolution"), Mapping) else {}
        if resolucao.get("filled") is not True:
            # Sem fill virtual não existe obrigação de proteção a observar.
            nota("NO_PROTECTION_OBLIGATION")
            continue
        aplicaveis += 1
        problema = _protection_reconciled(resolucao.get("protection"),
                                          ann["frozen"], resolucao)
        if problema is not None:
            nota(problema)
            continue
        observadas += 1
        protecao = resolucao["protection"]
        if integer(protecao.get("pending_failures")) is None \
                or protecao["pending_failures"] > 0 \
                or protecao.get("obligation_open_at_end") is True:
            pendentes += 1
    cobertura = round(observadas * 100.0 / aplicaveis, 6) if aplicaveis else None
    suficiente = bool(aplicaveis) and cobertura is not None \
        and cobertura >= MIN_PROTECTION_COVERAGE_PCT
    return {"protection_scope": PROTECTION_SCOPE,
            "protection_proves_real_sl": False,
            "protection_applicable": aplicaveis,
            "protection_observed": observadas,
            "protection_coverage_pct": cobertura,
            "protection_pending": pendentes if suficiente else None,
            "protection_reasons": motivos,
            "protection_min_coverage_pct": MIN_PROTECTION_COVERAGE_PCT,
            "protection_scope_accepted_for_promotion": PROTECTION_SCOPE_ACCEPTED_FOR_PROMOTION,
            "protection_decision_required": (None if PROTECTION_SCOPE_ACCEPTED_FOR_PROMOTION
                                             else PROTECTION_DECISION_REQUIRED),
            "unresolved_protection_failures": pendentes if suficiente else None,
            "protection_gap_reason": None if suficiente
            else ("NO_PROTECTION_OBLIGATION_OBSERVED" if not aplicaveis
                  else "PROTECTION_OBSERVATION_COVERAGE_INSUFFICIENT")}


def operational_measurements(annotations, valid, excluded, *, now_ms):
    """Falhas operacionais, duplicatas e proteções pendentes MEDIDAS na coorte.

    Fonte: as próprias anotações prospectivas (origem `OFFICIAL_R09_RESOLVER…`),
    no intervalo entre o start do SHADOW e `now_ms`. Nada aqui vira zero por
    conveniência: o que não pôde ser medido continua `None` com motivo.

    • `operational_failures` — resolução INVÁLIDA do replay prospectivo (janela
      corrompida/contrato quebrado). Não há ordem no shadow: falha operacional
      aqui é falha de RESOLUÇÃO, declarada como tal;
    • `economic_duplicates` — oportunidades repetidas DEDUPLICADAS (fato
      contado, não estimado);
    • `unresolved_protection_failures` — saída protegida que terminou sem
      economia conhecível (preenchida sem `net_r`/instante utilizável);
    • `fidelity_discrepancy_pct` — divergência entre a decisão da candidata em
      RUNTIME e a recomputação offline da MESMA oportunidade. Sem observação em
      runtime candidato, fica `None` com o motivo específico.
    """
    if not annotations:
        # Coorte VAZIA: nada foi medido. Zero aqui seria fabricar segurança.
        return {"operational_failures": None, "economic_duplicates": None,
                "resolution_failures": None, "economics_failures": None,
                **protection_measure([]), **fidelity_measure([]),
                "reason_code": "NO_PROSPECTIVE_OBSERVATIONS",
                "measured_until_ms": now_ms,
                "source": "PROSPECTIVE_ANNOTATIONS_OFFICIAL_RESOLVER"}
    # Trilhas SEPARADAS: resolução inválida e economia inconhecível são defeitos
    # diferentes, e a mesma linha pode ter os dois — `elif` apagaria um deles.
    falhas_resolucao = 0
    falhas_economia = 0
    for ann in annotations:
        if not verify_annotation(ann):
            continue
        resolution = ann.get("resolution") if isinstance(ann.get("resolution"), Mapping) else {}
        if resolution.get("status") == "INVALID":
            falhas_resolucao += 1
        if resolution.get("status") in TERMINAL and resolution.get("filled") is True \
                and (number(resolution.get("net_r")) is None
                     or integer(resolution.get("result_available_ts_ms")) is None):
            falhas_economia += 1
    return {"operational_failures": falhas_resolucao + falhas_economia,
            "resolution_failures": falhas_resolucao,
            "economics_failures": falhas_economia,
            "economic_duplicates": int(excluded.get("DUPLICATE_OPPORTUNITY") or 0),
            **protection_measure(annotations), **fidelity_measure(valid),
            "measured_until_ms": now_ms,
            "source": "PROSPECTIVE_ANNOTATIONS_OFFICIAL_RESOLVER"}


def summarize_prospective(annotations, *, started_at_ms, now_ms, enabled_playbooks):
    """Economia derivada de caminhos futuros reais; ausência operacional bloqueia."""
    from services import portfolio_replay_service as portfolio
    from services import walk_forward_service as wf
    from services import preselection_experiment_service as r12
    valid, excluded, keys = [], {}, set()
    for ann in annotations:
        reason = None
        if not verify_annotation(ann):
            reason = "ANNOTATION_INVALID"
        elif ann["frozen"].get("source_mode") != REAL:
            reason = "NOT_REAL_PROSPECTIVE"
        elif ann["frozen"].get("captured_at_ms", -1) < started_at_ms:
            reason = "BEFORE_SHADOW_START"
        elif ann["frozen"]["opportunity_key"] in keys:
            reason = "DUPLICATE_OPPORTUNITY"
        else:
            keys.add(ann["frozen"]["opportunity_key"])
            choices = ann["frozen"]["decisions"]
            result = ann["resolution"]
            if any(choices[s].get("state") == "UNKNOWN" for s in ("baseline", "candidate")):
                reason = "DECISION_UNKNOWN"
            elif result.get("status") not in TERMINAL:
                reason = "OUTCOME_NOT_RESOLVED"
            elif result.get("filled") is True and (number(result.get("net_r")) is None
                    or integer(result.get("result_available_ts_ms")) is None
                    or result["result_available_ts_ms"] > now_ms):
                reason = "OUTCOME_NOT_AVAILABLE"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            valid.append(ann)
    valid.sort(key=lambda a: (a["resolution"].get("result_available_ts_ms") or now_ms, a["frozen"]["opportunity_key"]))
    trades, deltas, periods = [], [], [[], [], [], []]
    for ann in valid:
        frozen, result = ann["frozen"], ann["resolution"]
        net = number(result.get("net_r"))
        if result.get("filled") is not True or net is None:
            continue
        candidate = frozen["decisions"]["candidate"]["state"] == "SELECTED"
        baseline = frozen["decisions"]["baseline"]["state"] == "SELECTED"
        delta = (net if candidate else 0.0) - (net if baseline else 0.0)
        deltas.append(delta)
        index = min(3, max(0, (frozen["captured_at_ms"] - started_at_ms) * 4 // max(1, now_ms - started_at_ms)))
        periods[index].append(delta)
        if candidate:
            trades.append({"admitted": True, "net_r": net, "playbook": frozen["playbook"]})
    coverage = len(valid) * 100.0 / len(annotations) if annotations else None
    ci = wf.block_bootstrap_ci(deltas, seed=7)
    temporal = {"folds": [{"reason_code": "OK", "delta_net_r": sum(p)} for p in periods if p], "ci": ci}
    # Medições REAIS da coorte (origem e intervalo declarados). O que não tem
    # fonte continua ausente — com o motivo específico, nunca zero.
    measured = operational_measurements(annotations, valid, excluded, now_ms=now_ms)
    gaps = ["prospective_sample"] if not valid else []
    if measured["fidelity_gap_reason"] is not None:
        gaps.append(measured["fidelity_gap_reason"])
    if measured.get("protection_gap_reason"):
        gaps.append(measured["protection_gap_reason"])
    # A proteção provada aqui é SIMULADA. Enquanto a decisão humana sobre o
    # ESCOPO de proteção da primeira promoção não existir, isto é lacuna
    # essencial declarada — e lacuna essencial mantém o gate em NO_GO.
    if measured.get("protection_decision_required"):
        gaps.append(measured["protection_decision_required"])
    if measured.get("reason_code"):
        gaps.append(measured["reason_code"])
    evidence = r12.gate_evidence_from_study(replay={"metrics": portfolio.portfolio_metrics([t["net_r"] for t in trades])},
        study=temporal, trades=trades, enabled_playbooks=enabled_playbooks,
        window_start_ms=started_at_ms, window_end_ms=now_ms, coverage_pct=coverage,
        operational_failures=measured["operational_failures"],
        economic_duplicates=measured["economic_duplicates"],
        unresolved_protection_failures=measured["unresolved_protection_failures"],
        essential_gaps=gaps, fidelity_discrepancy_pct=measured["fidelity_discrepancy_pct"])
    gate = r12.go_no_go(evidence)
    verdict = gate.get("verdict") if isinstance(gate, Mapping) else None
    state = ("COLLECTING" if not valid else
             "GATE_PASSED" if verdict == "GO_CANDIDATE" else "INSUFFICIENT_EVIDENCE")
    return {"available": True, "source": REAL, "state": state,
            "reason_code": "OK" if verdict == "GO_CANDIDATE" else "PROSPECTIVE_GATE_NOT_MET",
            "evidence": evidence, "gate": gate, "measurements": measured,
            "data_quality": {"raw": len(annotations), "valid": len(valid), "excluded_by_reason": excluded},
            "fingerprint": digest(annotations), "offline_used": False,
            "promotable": False}


async def load_prospective_evidence(session, exp, *, now_ms=None):
    from sqlalchemy import select
    from models.decision_observation import DecisionObservation as O
    start = (exp.shadow_metrics or {}).get(KEY)
    now_ms = _now() if now_ms is None else integer(now_ms)
    if not isinstance(start, Mapping) or now_ms is None or integer(start.get("started_at_ms")) is None:
        return {"available": False, "reason_code": "PROSPECTIVE_SHADOW_NOT_STARTED", "gate": None}
    frozen = (exp.offline_metrics or {}).get("study", {}).get("contract", {})
    manifest = frozen.get("research_manifest")
    from services import research_manifest_service as rm
    if not rm.authorized_comparison(manifest).get("real_study_allowed"):
        return {"available": False, "reason_code": "TEST_ONLY_NOT_PROSPECTIVE_AUTHORITY", "gate": None}
    ann = O.frozen_config["r09_pre_selection"][KEY]
    rows = (await session.execute(select(ann).where(*_matching_filters(exp, start),
        O.first_decision_observed_at <= datetime.fromtimestamp(now_ms / 1000, timezone.utc))
        .order_by(O.first_decision_observed_at, O.opportunity_key).limit(MAX_EVIDENCE_ROWS + 1))).scalars().all()
    if len(rows) > MAX_EVIDENCE_ROWS:
        return {"available": False, "reason_code": "PROSPECTIVE_SAMPLE_LIMIT_EXCEEDED", "gate": None}
    return summarize_prospective(rows, started_at_ms=start["started_at_ms"], now_ms=now_ms,
        enabled_playbooks=manifest["candidate"]["playbooks"])
